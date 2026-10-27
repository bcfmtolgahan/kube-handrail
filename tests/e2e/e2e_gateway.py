#!/usr/bin/env python3
"""End-to-end guard tests against a deployed gateway and test cluster.

Covers authentication, argument validation, namespace and replica limits,
GitOps and HPA checks, single-use and expiring plans, conflicts, concurrent
applies and the kill switch. Run it after the smoke test, against a disposable
cluster only.

Environment: GATEWAY_URL, TOKEN_URL, CLIENT_ID, CLIENT_SECRET, KILL_SWITCH_PARAM,
KUBECONFIG, and the usual AWS variables. Pass --with-expiry to also wait out a
plan TTL (PLAN_TTL_SECONDS, default 300).

The test applies tests/e2e/fixtures/argo-managed.yaml and creates and deletes
a temporary HPA, both in kube-handrail-sandbox. It briefly labels `web`, sets the
E2E_TOUCH environment variable on it and scales it by hand, and leaves it at
1 replica without the labels or the variable.
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

NAMESPACE = "kube-handrail-sandbox"
WEB = {"namespace": NAMESPACE, "name": "web"}
PLAN, APPLY = "k8sr___plan_scale_deployment", "k8sw___apply_scale_deployment"
FIXTURE = Path(__file__).parent / "fixtures" / "argo-managed.yaml"
UNKNOWN_PLAN = "00000000-0000-4000-8000-000000000000"

results: list[bool] = []


def report(name: str, ok: bool, detail: Any = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail not in ('', None) else ''}", flush=True)


def kubectl(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], check=True, capture_output=True, text=True, input=stdin).stdout


def set_kill_switch(value: str) -> None:
    param = os.environ["KILL_SWITCH_PARAM"]
    subprocess.run(["aws", "ssm", "put-parameter", "--overwrite", "--name", param, "--value", value], check=True, capture_output=True)


def fetch_token() -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(f"{os.environ['CLIENT_ID']}:{os.environ['CLIENT_SECRET']}".encode()).decode()
    request = urllib.request.Request(
        os.environ["TOKEN_URL"],
        data=body,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    return json.load(urllib.request.urlopen(request))["access_token"]


def list_tools_status(token: str | None) -> int:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return urllib.request.urlopen(urllib.request.Request(os.environ["GATEWAY_URL"], data=body, headers=headers)).status
    except urllib.error.HTTPError as error:
        return error.code


def tampered(token: str) -> str:
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["client_id"] = "someone-else"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{forged}.{signature}"


def auth_tests(token: str) -> None:
    report("no token is rejected", list_tools_status(None) == 401)
    report("malformed token is rejected", list_tools_status("abc.def.ghi") == 401)
    # The gateway answers 403 (insufficient_scope) for a well-formed token with a bad signature.
    status = list_tools_status(tampered(token))
    report("token with tampered claims is rejected", status in (401, 403), status)
    report("valid token is accepted", list_tools_status(token) == 200)


class Gateway:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.session.call_tool(tool, arguments)
        text = result.content[0].text if result.content else ""
        try:
            return json.loads(text)
        except ValueError:
            # Schema violations are rejected by the gateway before the Lambda runs.
            return {"ok": False, "error": {"code": "gateway_rejected", "message": text[:200]}}

    async def plan(self, replicas: int) -> str:
        envelope = await self.call(PLAN, {**WEB, "replicas": replicas})
        if not envelope.get("ok"):
            raise RuntimeError(f"plan failed: {envelope}")
        return envelope["data"]["plan_id"]


def code(envelope: dict[str, Any]) -> str | None:
    return (envelope.get("error") or {}).get("code")


async def expect(gw: Gateway, name: str, tool: str, arguments: dict[str, Any], expected: str) -> None:
    envelope = await gw.call(tool, arguments)
    report(name, code(envelope) == expected, code(envelope) or "ok")


async def expect_conflict(gw: Gateway, name: str, plan_id: str, changed: str) -> None:
    envelope = await gw.call(APPLY, {"plan_id": plan_id, **WEB, "replicas": 2})
    details = (envelope.get("error") or {}).get("details") or {}
    entry = details.get(changed) or {}
    planned = entry.get("planned", entry.get("planned_from"))
    passed = entry.get("current") is not None and entry.get("current") != planned
    report(name, code(envelope) == "conflict" and passed, code(envelope) or "ok")


def reset_web() -> None:
    kubectl("scale", "deploy/web", "--replicas=1")
    kubectl("rollout", "status", "deploy/web", "--timeout=120s")


async def validation_tests(gw: Gateway) -> None:
    await expect(gw, "namespace outside allowlist", PLAN, {"namespace": "kube-system", "name": "coredns", "replicas": 2}, "forbidden_namespace")
    await expect(gw, "replicas above maximum", PLAN, {**WEB, "replicas": 1000}, "out_of_bounds")
    await expect(gw, "replicas below minimum", PLAN, {**WEB, "replicas": -1}, "out_of_bounds")
    await expect(gw, "invalid name", PLAN, {**WEB, "name": "web;kubectl delete ns x", "replicas": 2}, "invalid_argument")
    await expect(gw, "invalid namespace", PLAN, {"namespace": "../x", "name": "web", "replicas": 2}, "invalid_argument")
    await expect(gw, "missing deployment", PLAN, {**WEB, "name": "does-not-exist", "replicas": 2}, "not_found")
    for bad in ("2", 2.5, True):
        envelope = await gw.call(PLAN, {**WEB, "replicas": bad})
        report(f"replicas={bad!r} is rejected", not envelope.get("ok"), code(envelope))


async def ownership_tests(gw: Gateway) -> None:
    kubectl("apply", "-f", str(FIXTURE))
    await expect(gw, "GitOps-managed deployment", PLAN, {"namespace": NAMESPACE, "name": "argo-managed", "replicas": 1}, "gitops_managed")
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "e2e-web", "namespace": NAMESPACE},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
            "minReplicas": 1,
            "maxReplicas": 1,
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 90}}}],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(hpa))
    try:
        await expect(gw, "deployment targeted by an HPA", PLAN, {**WEB, "replicas": 2}, "conflict")
    finally:
        kubectl("delete", "hpa", "e2e-web", "--ignore-not-found")


async def plan_tests(gw: Gateway, with_expiry: bool) -> None:
    await expect(gw, "apply with unknown plan", APPLY, {"plan_id": UNKNOWN_PLAN, **WEB, "replicas": 2}, "plan_not_found")

    plan_id = await gw.plan(2)
    await expect(gw, "apply with arguments that differ from the plan", APPLY, {"plan_id": plan_id, **WEB, "replicas": 3}, "plan_mismatch")
    try:
        # Label-only change: resourceVersion moves, generation and spec.replicas do not.
        # (An annotation change would bump a Deployment's generation.)
        kubectl("label", "deploy/web", "kube-handrail.io/e2e-touch=1", "--overwrite")
        envelope = await gw.call(APPLY, {"plan_id": plan_id, **WEB, "replicas": 2})
        report("apply after a label-only change succeeds", envelope.get("ok") is True, code(envelope) or "ok")
    finally:
        kubectl("label", "deploy/web", "kube-handrail.io/e2e-touch-")
        reset_web()

    plan_id = await gw.plan(2)
    try:
        # A pod template change bumps metadata.generation.
        kubectl("set", "env", "deploy/web", f"E2E_TOUCH={int(time.time())}")
        await expect_conflict(gw, "apply after a spec change", plan_id, "generation")
    finally:
        kubectl("set", "env", "deploy/web", "E2E_TOUCH-")
        reset_web()

    plan_id = await gw.plan(2)
    try:
        # The GitOps marker is a label, so only the re-check at apply time catches it.
        kubectl("label", "deploy/web", "app.kubernetes.io/instance=e2e", "--overwrite")
        await expect(gw, "apply after a GitOps label was added", APPLY, {"plan_id": plan_id, **WEB, "replicas": 2}, "gitops_managed")
    finally:
        kubectl("label", "deploy/web", "app.kubernetes.io/instance-")
        reset_web()

    plan_id = await gw.plan(2)
    try:
        kubectl("scale", "deploy/web", "--replicas=3")
        await expect_conflict(gw, "apply after a manual scale", plan_id, "replicas")
    finally:
        reset_web()

    plan_id = await gw.plan(2)
    first, second = await asyncio.gather(*(gw.call(APPLY, {"plan_id": plan_id, **WEB, "replicas": 2}) for _ in range(2)))
    outcomes = sorted([code(first) or "ok", code(second) or "ok"])
    report("concurrent applies of one plan: exactly one succeeds", outcomes == ["ok", "plan_used"], outcomes)
    reset_web()

    plan_id = await gw.plan(2)
    set_kill_switch("false")
    try:
        await expect(gw, "kill switch off takes effect immediately", APPLY, {"plan_id": plan_id, **WEB, "replicas": 2}, "remediation_disabled")
    finally:
        set_kill_switch("true")

    if with_expiry:
        plan_id = await gw.plan(2)
        ttl = int(os.environ.get("PLAN_TTL_SECONDS", "300"))
        print(f"waiting {ttl + 5}s for the plan to expire", flush=True)
        await asyncio.sleep(ttl + 5)
        await expect(gw, "apply after the plan expired", APPLY, {"plan_id": plan_id, **WEB, "replicas": 2}, "plan_expired")


async def run(with_expiry: bool) -> None:
    token = fetch_token()
    auth_tests(token)
    async with streamablehttp_client(os.environ["GATEWAY_URL"], headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            gw = Gateway(session)
            await validation_tests(gw)
            await ownership_tests(gw)
            await plan_tests(gw, with_expiry)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-expiry", action="store_true", help="also wait out a plan TTL")
    args = parser.parse_args()
    started = time.time()
    try:
        asyncio.run(run(args.with_expiry))
    finally:
        replicas = kubectl("get", "deploy/web", "-o", "jsonpath={.spec.replicas}")
        if replicas != "1":
            kubectl("scale", "deploy/web", "--replicas=1")
    print(f"{sum(results)}/{len(results)} passed in {int(time.time() - started)}s")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
