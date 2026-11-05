import base64
import dataclasses
import importlib.util
import io
import json
import uuid
from pathlib import Path

import pytest
from moto import mock_aws

from handrail_common.config import ConfigError
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, scale_args_hash

# Load by path: every Lambda directory contains a module named `handler`.
_SPEC = importlib.util.spec_from_file_location(
    "verifier_handler", Path(__file__).parents[2] / "mcp" / "verifier" / "handler.py"
)
handler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handler)

NOW = 1_700_000_000
REGION = "us-east-1"
TABLE = "plans-test"
SETTINGS = handler.VerifierSettings(
    plans_table=TABLE,
    write_function="kube-handrail-k8s-write",
    batch_limit=10,
    metric_namespace="kube-handrail/verifier",
    region=REGION,
)


class WriteLambda:
    """Stands in for lambda:Invoke of the write function; answers from `responses`."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.responses[json.loads(kwargs["Payload"])["plan_id"]]
        if isinstance(answer, Exception):
            raise answer
        if answer == "crash":
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(answer).encode())}


@pytest.fixture
def store(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        plans_table(TABLE, REGION)
        yield PlanStore.from_name(TABLE, REGION)


def due_plan(store: PlanStore, window: int) -> str:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=2,
        args_hash=scale_args_hash("sandbox", "web", 2),
        resource_version="1",
        generation=4,
        uid="u",
        created_at=NOW,
        expires_at=NOW + 300,
        verify_window_seconds=window,
    )
    store.create(plan)
    store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW, verify_window=120)
    return plan.plan_id


def envelope(outcome: str, revert: str | None = None) -> dict:
    data = {"due": True, "outcome": outcome, "reason": "r", "revert": revert}
    return {"ok": True, "data": data, "error": None, "truncated": False}


def records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def runtime(store, responses: dict, now: int = NOW + 100, **settings) -> handler.Runtime:
    return handler.Runtime(
        settings=dataclasses.replace(SETTINGS, **settings),
        store=store,
        lambda_client=WriteLambda(responses),
        clock=lambda: now,
    )


def test_each_due_plan_is_verified_on_the_verifier_channel(store, capsys):
    first, second = due_plan(store, 60), due_plan(store, 90)
    due_plan(store, 600)  # not due yet
    rt = runtime(store, {first: envelope("healthy"), second: envelope("degraded", "reverted")})

    record = handler.run(rt, "run-1")
    assert [json.loads(c["Payload"]) for c in rt.lambda_client.calls] == [
        {"plan_id": first},
        {"plan_id": second},
    ]
    call = rt.lambda_client.calls[0]
    assert call["FunctionName"] == "kube-handrail-k8s-write"
    assert call["InvocationType"] == "RequestResponse"
    assert json.loads(base64.b64decode(call["ClientContext"])) == {
        "custom": {
            "bedrockAgentCoreToolName": "kh_internal___verify_applied_plan",
            "kubeHandrailChannel": "verifier",
            "kubeHandrailCorrelationId": "run-1",
        }
    }
    assert (record["Runs"], record["Due"], record["Processed"], record["InvokeErrors"]) == (
        1,
        2,
        2,
        0,
    )
    assert (record["Degraded"], record["Reverts"]) == (1, 1)
    (line,) = records(capsys)
    (directive,) = line["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "kube-handrail/verifier"
    assert directive["Dimensions"] == [[]]
    assert [m["Name"] for m in directive["Metrics"]] == [
        "Runs",
        "Due",
        "Processed",
        "InvokeErrors",
        "Degraded",
        "Reverts",
    ]
    assert line["results"][1] == {
        "plan_id": second,
        "outcome": "degraded",
        "reason": "r",
        "revert": "reverted",
    }


def test_the_batch_limit_takes_the_longest_due_first(store, capsys):
    first, second = due_plan(store, 60), due_plan(store, 90)
    rt = runtime(store, {first: envelope("healthy"), second: envelope("healthy")}, batch_limit=1)
    assert handler.run(rt)["Due"] == 1
    assert [json.loads(c["Payload"]) for c in rt.lambda_client.calls] == [{"plan_id": first}]


def test_a_failed_verification_is_counted_and_the_run_goes_on(store, capsys):
    ids = [due_plan(store, window) for window in (60, 70, 80, 90)]
    error = {"ok": False, "data": None, "error": {"code": "internal", "message": "x"}}
    rt = runtime(
        store,
        {
            ids[0]: RuntimeError("throttled"),
            ids[1]: "crash",
            ids[2]: error,
            ids[3]: envelope("unknown"),
        },
    )
    record = handler.run(rt)
    assert (record["Processed"], record["InvokeErrors"]) == (1, 3)
    assert [r.get("error") for r in record["results"]] == [
        "RuntimeError",
        "RuntimeError",
        "internal",
        None,
    ]
    # The verifier writes nothing: every plan is still due for the next run.
    assert store.due_plans(NOW + 100, 10) == ids


def test_an_empty_queue_still_counts_the_run(store, capsys):
    record = handler.run(runtime(store, {}))
    assert (record["Runs"], record["Due"], record["Processed"]) == (1, 0, 0)
    assert len(records(capsys)) == 1


def test_settings_parse():
    env = {"PLANS_TABLE": TABLE, "WRITE_FUNCTION_NAME": "w", "AWS_REGION": REGION}
    settings = handler.load_verifier_settings(env)
    assert (settings.batch_limit, settings.metric_namespace) == (10, "kube-handrail/verifier")
    with pytest.raises(ConfigError):
        handler.load_verifier_settings(env | {"VERIFY_BATCH_LIMIT": "0"})
    with pytest.raises(ConfigError):
        handler.load_verifier_settings({"PLANS_TABLE": TABLE})
