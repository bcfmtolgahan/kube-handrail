#!/usr/bin/env python3
"""End-to-end smoke test for the Milestone 0 slice through the gateway.

Gets a client-credentials token, lists tools, scales the sandbox Deployment
1 -> 2 -> 1 through plan/apply and checks that a plan cannot be reused.

Configuration (flags override environment variables):
  GATEWAY_URL, TOKEN_URL, CLIENT_ID, CLIENT_SECRET, SCOPE (optional,
  space-separated),
  NAMESPACE (default kube-handrail-sandbox), NAME (default web).
The client secret is read from the environment only.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

EXPECTED_TOOLS = {
    "k8sr___get_workload_status",
    "k8sr___plan_scale_deployment",
    "k8sw___apply_scale_deployment",
}
READY_TIMEOUT_SECONDS = 180
CONFLICT_RETRIES = 3


class SmokeTestError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway-url", default=env("GATEWAY_URL"))
    parser.add_argument("--token-url", default=env("TOKEN_URL"))
    parser.add_argument("--client-id", default=env("CLIENT_ID"))
    parser.add_argument("--scope", default=env("SCOPE"))
    parser.add_argument("--namespace", default=env("NAMESPACE", "kube-handrail-sandbox"))
    parser.add_argument("--name", default=env("NAME", "web"))
    args = parser.parse_args()
    args.client_secret = env("CLIENT_SECRET")
    missing = [
        flag
        for flag, value in [
            ("GATEWAY_URL", args.gateway_url),
            ("TOKEN_URL", args.token_url),
            ("CLIENT_ID", args.client_id),
            ("CLIENT_SECRET", args.client_secret),
        ]
        if not value
    ]
    if missing:
        parser.error(f"missing configuration: {', '.join(missing)}")
    return args


def fetch_token(token_url: str, client_id: str, client_secret: str, scope: str | None) -> str:
    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope
    response = httpx.post(token_url, data=form, auth=(client_id, client_secret), timeout=30)
    if response.status_code != 200:
        # The body may echo request details; only the status is reported.
        raise SmokeTestError(f"token request failed with HTTP {response.status_code}")
    return response.json()["access_token"]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestError(message)
    print(f"ok: {message}")


class Gateway:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return the handler envelope."""
        result = await self.session.call_tool(tool, arguments)
        texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        if result.isError or not texts:
            raise SmokeTestError(f"{tool}: gateway returned an error result: {texts[:1]}")
        try:
            envelope = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise SmokeTestError(f"{tool}: response is not a JSON envelope") from exc
        if not isinstance(envelope, dict) or "ok" not in envelope:
            raise SmokeTestError(f"{tool}: response is not a JSON envelope")
        return envelope

    async def call_ok(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = await self.call(tool, arguments)
        if not envelope["ok"]:
            raise SmokeTestError(f"{tool}: {envelope['error']}")
        return envelope["data"]


async def status(gw: Gateway, namespace: str, name: str) -> dict[str, Any]:
    return await gw.call_ok("k8sr___get_workload_status", {"namespace": namespace, "name": name})


async def wait_ready(gw: Gateway, namespace: str, name: str, replicas: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        counts = (await status(gw, namespace, name))["replicas"]
        if counts["desired"] == replicas and counts["ready"] == replicas:
            print(f"ok: {namespace}/{name} has {replicas} ready replica(s)")
            return
        await asyncio.sleep(5)
    raise SmokeTestError(f"{namespace}/{name} not ready at {replicas} replicas in time")


async def plan(gw: Gateway, args: dict[str, Any]) -> str:
    data = await gw.call_ok("k8sr___plan_scale_deployment", args)
    plan_id = data["plan_id"]
    print(f"ok: planned {args} as {plan_id}")
    return plan_id


async def plan_and_apply(gw: Gateway, args: dict[str, Any]) -> str:
    """Plan and apply, re-planning when the Deployment changed in between."""
    for _ in range(CONFLICT_RETRIES):
        plan_id = await plan(gw, args)
        envelope = await gw.call("k8sw___apply_scale_deployment", {"plan_id": plan_id, **args})
        if envelope["ok"]:
            print(f"ok: applied {plan_id}")
            return plan_id
        if envelope["error"]["code"] != "conflict":
            raise SmokeTestError(f"apply failed: {envelope['error']}")
        print("Deployment changed since planning; planning again")
        await asyncio.sleep(5)
    raise SmokeTestError("apply kept returning conflict")


async def run(args: argparse.Namespace, token: str) -> None:
    http_client = create_mcp_http_client(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30, read=300)
    )
    async with http_client, streamable_http_client(args.gateway_url, http_client=http_client) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            gw = Gateway(session)

            tools = {tool.name for tool in (await session.list_tools()).tools}
            check(tools == EXPECTED_TOOLS, f"tools/list returns exactly {sorted(EXPECTED_TOOLS)}")

            target = {"namespace": args.namespace, "name": args.name}
            initial = await status(gw, **target)
            check(initial["gitops_managed"] is False, "target is not GitOps-managed")
            check(initial["replicas"]["desired"] == 1, "target starts at 1 replica")

            scale_up = {**target, "replicas": 2}
            plan_id = await plan_and_apply(gw, scale_up)
            reused = await gw.call("k8sw___apply_scale_deployment", {"plan_id": plan_id, **scale_up})
            check(
                not reused["ok"] and reused["error"]["code"] == "plan_used",
                "second apply with the same plan_id is rejected as plan_used",
            )
            await wait_ready(gw, args.namespace, args.name, 2)

            await plan_and_apply(gw, {**target, "replicas": 1})
            await wait_ready(gw, args.namespace, args.name, 1)


def main() -> int:
    args = parse_args()
    failures: list[BaseException] = []
    try:
        token = fetch_token(args.token_url, args.client_id, args.client_secret, args.scope)
        asyncio.run(run(args, token))
    except* SmokeTestError as group:
        # The MCP client runs in a task group, so failures may arrive wrapped.
        failures.extend(group.exceptions)
    for exc in failures:
        print(f"FAIL: {exc}", file=sys.stderr)
    if failures:
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
