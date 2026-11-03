#!/usr/bin/env python3
"""End-to-end guard tests against a deployed gateway and test cluster.

Covers authentication, argument validation, namespace and replica limits,
GitOps and HPA checks, single-use and expiring plans, conflicts, concurrent
applies, the kill switch, the oscillation breaker (flapping and manual
change), the blast-radius budget and the impact preview. Run it after the smoke test, against a disposable cluster only.

Environment: GATEWAY_URL, TOKEN_URL, CLIENT_ID, CLIENT_SECRET, KILL_SWITCH_PARAM,
KUBECONFIG, the usual AWS variables, and APPROVAL_URL with --approval-mode server.
Pass --with-expiry to also wait out a plan TTL (PLAN_TTL_SECONDS, default 300). APPLY_COOLDOWN_SECONDS (default 60) must
match the deployment: the test waits it out after each apply. The blast-radius
case expects MAX_REPLICA_STEP_PERCENT=100 (the default) and MAX_REPLICAS >= 3.

The test never changes `web`, which belongs to the smoke test; it only plans
against it for validation cases that are refused before apply. Every case that
applies or edits a Deployment uses fixture Deployments created for this run in
kube-handrail-sandbox (e2e-target, e2e-flap, e2e-manual, plus a Service and a
PodDisruptionBudget for e2e-target) and deleted at the end, so each run starts
with an empty history. It also applies
tests/e2e/fixtures/argo-managed.yaml and creates and deletes a temporary HPA.

With --approval-mode server (the deployment's approval_mode is server), the
cases that need an apply to succeed through the gateway are skipped; instead the
server approval cases run: an apply without approval is approval_required,
request_approval notifies once per cooldown, the approval URL refuses a Cognito
token and an unsigned request, an approval signed with the operator's AWS
credentials (SigV4, service lambda) applies the plan and the record names the
approver, and a rejected plan cannot be applied. They need APPROVAL_URL (the
approval_url output) and an operator identity listed in approver_principals.

The per-object and global hourly budgets and the plan rate limit are covered by
unit tests only: reaching them here takes 6 or 30 applies or 20 plans. So are the
scale_to_zero and service_without_endpoints warnings: they need a plan to 0
replicas, which a sandbox with MIN_REPLICAS=1 refuses as out_of_bounds.
"""

import argparse
import asyncio
import base64
import importlib.util
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

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

NAMESPACE = "kube-handrail-sandbox"
WEB = {"namespace": NAMESPACE, "name": "web"}
TARGET = {"namespace": NAMESPACE, "name": "e2e-target"}
FLAP = {"namespace": NAMESPACE, "name": "e2e-flap"}
MANUAL = {"namespace": NAMESPACE, "name": "e2e-manual"}
FIXTURES = (TARGET["name"], FLAP["name"], MANUAL["name"])
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
# The plan history index is eventually consistent; give a just-recorded apply time to show up.
HISTORY_SETTLE_SECONDS = 3
# Must match the deployed APPLY_COOLDOWN_SECONDS; a lower value here makes cases fail.
COOLDOWN_SECONDS = int(os.environ.get("APPLY_COOLDOWN_SECONDS", "60"))
PLAN, APPLY = "k8sr___plan_scale_deployment", "k8sw___apply_scale_deployment"
REQUEST_APPROVAL = "k8sr___request_approval"
FIXTURE = Path(__file__).parent / "fixtures" / "argo-managed.yaml"
UNKNOWN_PLAN = "00000000-0000-4000-8000-000000000000"

results: list[bool] = []


async def cool_down() -> None:
    """Wait until the object last applied to is past its cooldown."""
    await asyncio.sleep(COOLDOWN_SECONDS + 1)


def report(name: str, ok: bool, detail: Any = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail not in ('', None) else ''}", flush=True)


def kubectl(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], check=True, capture_output=True, text=True, input=stdin).stdout


def fixture(name: str) -> dict[str, Any]:
    """A 1-replica unprivileged nginx that passes Pod Security `restricted` and has no GitOps markers."""
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": NAMESPACE}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "replicas": 1,
            "revisionHistoryLimit": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 101, "runAsGroup": 101, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "nginxinc/nginx-unprivileged:1.27.5-alpine",
                            "resources": {"requests": {"cpu": "5m", "memory": "16Mi"}, "limits": {"cpu": "50m", "memory": "64Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}],
                },
            },
        },
    }


def target_labels() -> dict[str, str]:
    return fixture(TARGET["name"])["spec"]["selector"]["matchLabels"]


def target_service() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": TARGET["name"], "namespace": NAMESPACE, "labels": {"app.kubernetes.io/part-of": NAMESPACE}},
        "spec": {"selector": target_labels(), "ports": [{"name": "http", "port": 80, "targetPort": 8080}]},
    }


def target_pdb(min_available: int) -> dict[str, Any]:
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": TARGET["name"], "namespace": NAMESPACE, "labels": {"app.kubernetes.io/part-of": NAMESPACE}},
        "spec": {"minAvailable": min_available, "selector": {"matchLabels": target_labels()}},
    }


def delete_fixtures() -> None:
    kubectl("delete", "service", TARGET["name"], "--ignore-not-found")
    kubectl("delete", "pdb", TARGET["name"], "--ignore-not-found")
    kubectl("delete", "deployment", *FIXTURES, "--ignore-not-found", "--wait=true")


def create_fixtures() -> None:
    # Delete leftovers from an aborted run first: a new object (new uid) has no history.
    delete_fixtures()
    for name in FIXTURES:
        kubectl("create", "-f", "-", stdin=json.dumps(fixture(name)))
    kubectl("create", "-f", "-", stdin=json.dumps(target_service()))
    kubectl("create", "-f", "-", stdin=json.dumps(target_pdb(1)))
    for name in FIXTURES:
        kubectl("rollout", "status", f"deploy/{name}", "--timeout=120s")


def web_spec() -> str:
    return kubectl("get", "deploy/web", "-o", "jsonpath={.metadata.uid}/{.metadata.generation}/{.spec.replicas}")


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

    async def plan(self, target: dict[str, str], replicas: int, **extra: Any) -> str:
        envelope = await self.call(PLAN, {**target, "replicas": replicas, **extra})
        if not envelope.get("ok"):
            raise RuntimeError(f"plan failed: {envelope}")
        return envelope["data"]["plan_id"]

    async def apply(self, target: dict[str, str], plan_id: str, replicas: int, **extra: Any) -> dict[str, Any]:
        return await self.call(APPLY, {"plan_id": plan_id, **target, "replicas": replicas, **extra})


def code(envelope: dict[str, Any]) -> str | None:
    return (envelope.get("error") or {}).get("code")


async def expect(gw: Gateway, name: str, tool: str, arguments: dict[str, Any], expected: str) -> None:
    envelope = await gw.call(tool, arguments)
    report(name, code(envelope) == expected, code(envelope) or "ok")


async def expect_conflict(gw: Gateway, name: str, plan_id: str, changed: str) -> None:
    envelope = await gw.apply(TARGET, plan_id, 2)
    details = (envelope.get("error") or {}).get("details") or {}
    entry = details.get(changed) or {}
    planned = entry.get("planned", entry.get("planned_from"))
    passed = entry.get("current") is not None and entry.get("current") != planned
    report(name, code(envelope) == "conflict" and passed, code(envelope) or "ok")


def reset_target() -> None:
    """Back to 1 replica by hand. Dropping the last-plan annotation as well keeps the next
    case from being refused as a manual change (the manual-change case has its own fixture)."""
    kubectl("scale", "deploy/e2e-target", "--replicas=1")
    kubectl("annotate", "deploy/e2e-target", f"{LAST_PLAN_ANNOTATION}-")
    kubectl("rollout", "status", "deploy/e2e-target", "--timeout=120s")


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
        "metadata": {"name": "e2e-target", "namespace": NAMESPACE},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "e2e-target"},
            "minReplicas": 1,
            "maxReplicas": 1,
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 90}}}],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(hpa))
    try:
        await expect(gw, "deployment targeted by an HPA", PLAN, {**TARGET, "replicas": 2}, "conflict")
    finally:
        kubectl("delete", "hpa", "e2e-target", "--ignore-not-found")


async def plan_tests(gw: Gateway, with_expiry: bool) -> None:
    await expect(gw, "apply with unknown plan", APPLY, {"plan_id": UNKNOWN_PLAN, **TARGET, "replicas": 2}, "plan_not_found")

    plan_id = await gw.plan(TARGET, 2)
    await expect(gw, "apply with arguments that differ from the plan", APPLY, {"plan_id": plan_id, **TARGET, "replicas": 3}, "plan_mismatch")
    try:
        # Label-only change: resourceVersion moves, generation and spec.replicas do not.
        # (An annotation change would bump a Deployment's generation.)
        kubectl("label", "deploy/e2e-target", "kube-handrail.io/e2e-touch=1", "--overwrite")
        envelope = await gw.apply(TARGET, plan_id, 2)
        report("apply after a label-only change succeeds", envelope.get("ok") is True, code(envelope) or "ok")
    finally:
        kubectl("label", "deploy/e2e-target", "kube-handrail.io/e2e-touch-")
        reset_target()
    await cool_down()

    plan_id = await gw.plan(TARGET, 2)
    try:
        # A pod template change bumps metadata.generation.
        kubectl("set", "env", "deploy/e2e-target", f"E2E_TOUCH={int(time.time())}")
        await expect_conflict(gw, "apply after a spec change", plan_id, "generation")
    finally:
        kubectl("set", "env", "deploy/e2e-target", "E2E_TOUCH-")
        reset_target()

    plan_id = await gw.plan(TARGET, 2)
    try:
        # The GitOps marker is a label, so only the re-check at apply time catches it.
        kubectl("label", "deploy/e2e-target", "app.kubernetes.io/instance=e2e", "--overwrite")
        await expect(gw, "apply after a GitOps label was added", APPLY, {"plan_id": plan_id, **TARGET, "replicas": 2}, "gitops_managed")
    finally:
        kubectl("label", "deploy/e2e-target", "app.kubernetes.io/instance-")
        reset_target()

    plan_id = await gw.plan(TARGET, 2)
    try:
        kubectl("scale", "deploy/e2e-target", "--replicas=3")
        await expect_conflict(gw, "apply after a manual scale", plan_id, "replicas")
    finally:
        reset_target()

    plan_id = await gw.plan(TARGET, 2)
    first, second = await asyncio.gather(*(gw.apply(TARGET, plan_id, 2) for _ in range(2)))
    outcomes = sorted([code(first) or "ok", code(second) or "ok"])
    report("concurrent applies of one plan: exactly one succeeds", outcomes == ["ok", "plan_used"], outcomes)
    reset_target()
    await cool_down()

    plan_id = await gw.plan(TARGET, 2)
    set_kill_switch("false")
    try:
        await expect(gw, "kill switch off takes effect immediately", APPLY, {"plan_id": plan_id, **TARGET, "replicas": 2}, "remediation_disabled")
    finally:
        set_kill_switch("true")

    if with_expiry:
        plan_id = await gw.plan(TARGET, 2)
        ttl = int(os.environ.get("PLAN_TTL_SECONDS", "300"))
        print(f"waiting {ttl + 5}s for the plan to expire", flush=True)
        await asyncio.sleep(ttl + 5)
        await expect(gw, "apply after the plan expired", APPLY, {"plan_id": plan_id, **TARGET, "replicas": 2}, "plan_expired")


def wait_for(name: str, condition: Any, timeout: int = 120) -> None:
    """Poll until condition() is true: controllers update EndpointSlices and PDB status
    shortly after the pods change."""
    deadline = time.time() + timeout
    while not condition():
        if time.time() > deadline:
            raise RuntimeError(f"timed out waiting for {name}")
        time.sleep(2)


def ready_endpoints() -> int:
    out = kubectl("get", "endpointslices", "-l", f"kubernetes.io/service-name={TARGET['name']}", "-o", "json")
    return sum(
        1
        for item in json.loads(out)["items"]
        for endpoint in item.get("endpoints") or []
        if (endpoint.get("conditions") or {}).get("ready") is True
    )


def pdb_status() -> tuple[int, int]:
    status = json.loads(kubectl("get", "pdb", TARGET["name"], "-o", "json")).get("status") or {}
    return status.get("currentHealthy", -1), status.get("disruptionsAllowed", -1)


async def impact_tests(gw: Gateway) -> None:
    """Plans only, never applied, so no cooldown is needed between them. Runs after the
    plan tests have waited out the cooldown of e2e-target's last apply; that apply was a
    scale-up, so the 2 -> 1 plan below is a single revert, not flapping."""
    wait_for("one ready endpoint of e2e-target", lambda: ready_endpoints() == 1)
    wait_for("PDB status of e2e-target", lambda: pdb_status() == (1, 0))
    envelope = await gw.call(PLAN, {**TARGET, "replicas": 2})
    impact = (envelope.get("data") or {}).get("impact") or {}
    services = impact.get("services") or []
    pdbs = impact.get("pod_disruption_budgets") or []
    report(
        "impact: plan 1 -> 2 reports the Service with its ready endpoints",
        services == [{"name": TARGET["name"], "ready_endpoints_now": 1, "projected_ready_endpoints": 2}],
        code(envelope) or services,
    )
    report(
        "impact: plan 1 -> 2 reports the PodDisruptionBudget, no warnings",
        [(p.get("name"), p.get("min_available"), p.get("disruptions_allowed")) for p in pdbs] == [(TARGET["name"], 1, 0)]
        and impact.get("pods", {}).get("added") == 1
        and impact.get("warnings") == [],
        code(envelope) or pdbs,
    )

    envelope = await gw.call(PLAN, {**TARGET, "replicas": 3})
    details = (envelope.get("error") or {}).get("details") or {}
    report(
        "blast radius: plan 1 -> 3 is refused",
        code(envelope) == "blast_radius_exceeded" and details == {"from": 1, "to": 3, "max_step": 1, "max_step_percent": 100},
        code(envelope) or "ok",
    )

    try:
        kubectl("scale", "deploy/e2e-target", "--replicas=2")
        kubectl("rollout", "status", "deploy/e2e-target", "--timeout=120s")
        kubectl("apply", "-f", "-", stdin=json.dumps(target_pdb(2)))
        wait_for("PDB of e2e-target with no headroom", lambda: pdb_status() == (2, 0))
        envelope = await gw.call(PLAN, {**TARGET, "replicas": 1})
        warnings = ((envelope.get("data") or {}).get("impact") or {}).get("warnings") or []
        report(
            "impact: plan 2 -> 1 past the PDB headroom warns pdb_headroom and is not refused",
            envelope.get("ok") is True and [w.get("code") for w in warnings] == ["pdb_headroom"],
            code(envelope) or warnings,
        )
    finally:
        kubectl("apply", "-f", "-", stdin=json.dumps(target_pdb(1)))
        reset_target()


async def plan_and_apply(gw: Gateway, name: str, target: dict[str, str], replicas: int, **extra: Any) -> None:
    envelope = await gw.apply(target, await gw.plan(target, replicas, **extra), replicas, **extra)
    report(name, envelope.get("ok") is True, code(envelope) or "ok")
    await asyncio.sleep(HISTORY_SETTLE_SECONDS)


async def breaker_tests(gw: Gateway) -> None:
    await plan_and_apply(gw, "breaker: scale up 1 -> 2 is applied", FLAP, 2)
    envelope = await gw.call(PLAN, {**FLAP, "replicas": 1})
    retry_after = ((envelope.get("error") or {}).get("details") or {}).get("retry_after")
    report("cooldown: a change right after an apply is refused", code(envelope) == "cooldown" and bool(retry_after), code(envelope) or "ok")
    await cool_down()
    await plan_and_apply(gw, "breaker: scale back down 2 -> 1 is applied (single revert)", FLAP, 1)
    await cool_down()
    envelope = await gw.call(PLAN, {**FLAP, "replicas": 2})
    history = ((envelope.get("error") or {}).get("details") or {}).get("history") or []
    report(
        "breaker: scaling up again within the window is flapping",
        code(envelope) == "flapping" and [(h.get("from"), h.get("to")) for h in history] == [(1, 2), (2, 1)],
        code(envelope) or "ok",
    )

    await plan_and_apply(gw, "manual change: scale up 1 -> 2 is applied", MANUAL, 2)
    kubectl("rollout", "status", "deploy/e2e-manual", "--timeout=120s")
    kubectl("scale", "deploy/e2e-manual", "--replicas=3")
    await cool_down()
    envelope = await gw.call(PLAN, {**MANUAL, "replicas": 1})
    details = (envelope.get("error") or {}).get("details") or {}
    report(
        "manual change: plan after a scale by hand is refused",
        code(envelope) == "manual_change" and details == {"last_applied_to": 2, "current": 3},
        code(envelope) or "ok",
    )
    envelope = await gw.call(PLAN, {**MANUAL, "replicas": 1, "override_manual_change": True})
    report("manual change: plan with override_manual_change succeeds", envelope.get("ok") is True, code(envelope) or "ok")
    if not envelope.get("ok"):
        return
    plan_id = envelope["data"]["plan_id"]
    await expect(gw, "manual change: apply without the planned override", APPLY, {"plan_id": plan_id, **MANUAL, "replicas": 1}, "plan_mismatch")
    envelope = await gw.apply(MANUAL, plan_id, 1, override_manual_change=True)
    report("manual change: apply with the planned override succeeds", envelope.get("ok") is True, code(envelope) or "ok")


def approve_script() -> Any:
    """scripts/approve.py, for its SigV4 signing: the test signs exactly as an operator would."""
    spec = importlib.util.spec_from_file_location("approve", Path(__file__).parents[2] / "scripts" / "approve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def url_status(url: str, headers: dict[str, str]) -> int:
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers)).status
    except urllib.error.HTTPError as error:
        return error.code


def operator_arn() -> str:
    return boto3.client("sts").get_caller_identity()["Arn"]


def last_plan_id(name: str) -> str:
    return kubectl("get", f"deploy/{name}", "-o", f"jsonpath={{.metadata.annotations.kube-handrail\\.io/last-plan-id}}")


async def approval_tests(gw: Gateway, token: str) -> None:
    approve = approve_script()
    url = os.environ["APPROVAL_URL"]
    region = approve.region_of(url, os.environ.get("APPROVAL_REGION"))

    plan_id = await gw.plan(TARGET, 2)
    envelope = await gw.apply(TARGET, plan_id, 2)
    details = (envelope.get("error") or {}).get("details")
    report("approval: apply without approval is approval_required", code(envelope) == "approval_required" and details == {"status": "planned"}, code(envelope) or "ok")

    envelope = await gw.call(REQUEST_APPROVAL, {"plan_id": plan_id})
    data = envelope.get("data") or {}
    report("approval: request_approval notifies", data.get("status") == "awaiting_approval" and data.get("notified") is True, code(envelope) or data)
    envelope = await gw.call(REQUEST_APPROVAL, {"plan_id": plan_id})
    data = envelope.get("data") or {}
    report("approval: a second request within the cooldown notifies nobody", data.get("notified") is False and bool(data.get("retry_after")), code(envelope) or data)

    show_url = f"{url.rstrip('/')}/?{urllib.parse.urlencode({'plan_id': plan_id})}"
    status = url_status(show_url, {"Authorization": f"Bearer {token}"})
    report("approval: the approval URL refuses a Cognito token", status == 403, status)
    status = url_status(show_url, {})
    report("approval: the approval URL refuses an unsigned request", status == 403, status)

    status, envelope = approve.show(url, region, plan_id)
    view = envelope.get("data") or {}
    report(
        "approval: show renders the server record",
        status == 200 and view.get("status") == "awaiting_approval" and view.get("replicas") == {"from": 1, "to": 2} and bool(view.get("impact")),
        status,
    )

    status, envelope = approve.decide(url, region, plan_id, "approve", None, True)
    applied = (envelope.get("data") or {}).get("apply") or {}
    report("approval: an approval signed by an approver applies the plan", status == 200 and applied.get("ok") is True, (applied.get("error") or {}).get("code") or status)
    report("approval: the Deployment carries the approved plan", last_plan_id(TARGET["name"]) == plan_id)
    status, envelope = approve.show(url, region, plan_id)
    view = envelope.get("data") or {}
    approver = (view.get("approval") or {}).get("approved_by")
    report("approval: the record is applied and names the approver", view.get("status") == "applied" and approver == operator_arn(), approver)

    status, envelope = approve.decide(url, region, plan_id, "approve", None, True)
    report("approval: an applied plan cannot be approved again", status == 409 and code(envelope) == "plan_used", code(envelope))
    reset_target()
    await cool_down()

    plan_id = await gw.plan(TARGET, 2)
    status, envelope = approve.decide(url, region, plan_id, "reject", "e2e rejection", False)
    report("approval: a plan can be rejected", status == 200 and (envelope.get("data") or {}).get("plan", {}).get("status") == "rejected", status)
    envelope = await gw.apply(TARGET, plan_id, 2)
    details = (envelope.get("error") or {}).get("details")
    report("approval: a rejected plan cannot be applied", code(envelope) == "approval_required" and details == {"status": "rejected"}, code(envelope) or "ok")


async def run(with_expiry: bool, approval_mode: str) -> None:
    token = fetch_token()
    auth_tests(token)
    async with streamablehttp_client(os.environ["GATEWAY_URL"], headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            gw = Gateway(session)
            await validation_tests(gw)
            await ownership_tests(gw)
            if approval_mode == "server":
                print("server approval mode: skipping the cases that apply through the gateway", flush=True)
                await impact_tests(gw)
                await approval_tests(gw, token)
                return
            await plan_tests(gw, with_expiry)
            await impact_tests(gw)
            await breaker_tests(gw)


def describe(exc: BaseException) -> str:
    """The innermost error: the MCP client wraps failures in nested exception groups."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    stderr = getattr(exc, "stderr", None)
    return f"{type(exc).__name__}: {exc}" + (f" stderr: {stderr.strip()}" if stderr else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-expiry", action="store_true", help="also wait out a plan TTL")
    parser.add_argument(
        "--approval-mode",
        choices=["client", "server"],
        default="client",
        help="server: the deployment's approval_mode is server",
    )
    args = parser.parse_args()
    started = time.time()
    web_before = web_spec()
    try:
        create_fixtures()
        asyncio.run(run(args.with_expiry, args.approval_mode))
    except Exception as exc:
        # A case that raised (for example a plan that failed unexpectedly) still gets the
        # fixtures deleted and the summary printed.
        report("run completed", False, describe(exc)[:400])
    finally:
        delete_fixtures()
    report("web was not changed", web_spec() == web_before, web_before)
    print(f"{sum(results)}/{len(results)} passed in {int(time.time() - started)}s")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
