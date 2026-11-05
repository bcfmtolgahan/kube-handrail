"""Verifier Lambda: runs the verifications that are due (mcp/CONTRACT.md, Verification and
revert). Not a gateway target.

A scheduled sweeper outside the VPC, like the sentinel. Each run reads the plans whose
verify_at has passed from the verify-due index and invokes the write Lambda's internal
handler for each, on the verifier channel; the write Lambda reads the Deployment and records
the outcome. The verifier writes nothing itself, so a failed invoke leaves the plan due for
the next run.
"""

import base64
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

from handrail_common.config import ConfigError, int_env, required_env
from handrail_common.dispatch import INTERNAL_TARGET, TARGET_DELIMITER, TOOL_NAME_KEY
from handrail_common.observability import APPROVER_CORRELATION_KEY, CHANNEL_KEY, CHANNEL_VERIFIER
from handrail_common.plans import PlanStore
from handrail_common.verify import DEGRADED, REVERTED

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VERIFY_TOOL = f"{INTERNAL_TARGET}{TARGET_DELIMITER}verify_applied_plan"
DEFAULT_METRIC_NAMESPACE = "kube-handrail/verifier"


@dataclass(frozen=True)
class VerifierSettings:
    plans_table: str
    write_function: str
    # Plans verified per run, the longest due first; the rest wait for the next run.
    batch_limit: int
    metric_namespace: str
    region: str | None


def load_verifier_settings(env: Mapping[str, str] | None = None) -> VerifierSettings:
    env = os.environ if env is None else env
    limit = int_env(env, "VERIFY_BATCH_LIMIT", 10)
    if limit <= 0:
        raise ConfigError("VERIFY_BATCH_LIMIT must be positive")
    return VerifierSettings(
        plans_table=required_env(env, "PLANS_TABLE"),
        write_function=required_env(env, "WRITE_FUNCTION_NAME"),
        batch_limit=limit,
        metric_namespace=env.get("METRIC_NAMESPACE", "").strip() or DEFAULT_METRIC_NAMESPACE,
        region=env.get("AWS_REGION") or None,
    )


@dataclass
class Runtime:
    settings: VerifierSettings
    store: PlanStore
    lambda_client: Any
    clock: Callable[[], float] = time.time


def verify_plan(rt: Runtime, plan_id: str, correlation_id: str | None) -> dict[str, Any]:
    """Invoke the write Lambda's internal handler for one plan; returns its envelope."""
    custom = {TOOL_NAME_KEY: VERIFY_TOOL, CHANNEL_KEY: CHANNEL_VERIFIER}
    if correlation_id is not None:
        # The verification, a revert and its annotation then carry this run's id.
        custom[APPROVER_CORRELATION_KEY] = correlation_id
    response = rt.lambda_client.invoke(
        FunctionName=rt.settings.write_function,
        InvocationType="RequestResponse",
        ClientContext=base64.b64encode(json.dumps({"custom": custom}).encode()).decode(),
        Payload=json.dumps({"plan_id": plan_id}).encode(),
    )
    if response.get("FunctionError"):
        raise RuntimeError(f"function error: {response['FunctionError']}")
    envelope = json.loads(response["Payload"].read())
    if not isinstance(envelope, dict):
        raise RuntimeError("unexpected response")
    return envelope


def run(rt: Runtime, correlation_id: str | None = None) -> dict[str, Any]:
    due = rt.store.due_plans(int(rt.clock()), rt.settings.batch_limit)
    results = []
    for plan_id in due:
        try:
            envelope = verify_plan(rt, plan_id, correlation_id)
        except Exception as exc:
            logger.exception("cannot verify plan %s", plan_id)
            results.append({"plan_id": plan_id, "error": type(exc).__name__})
            continue
        if not envelope.get("ok"):
            error = (envelope.get("error") or {}).get("code")
            results.append({"plan_id": plan_id, "error": str(error)})
            continue
        data = envelope.get("data") or {}
        results.append(
            {
                "plan_id": plan_id,
                "outcome": data.get("outcome"),
                "reason": data.get("reason"),
                "revert": data.get("revert"),
            }
        )
    record = {
        "Runs": 1,
        "Due": len(due),
        "Processed": sum(1 for r in results if "error" not in r),
        "InvokeErrors": sum(1 for r in results if "error" in r),
        "Degraded": sum(1 for r in results if r.get("outcome") == DEGRADED),
        "Reverts": sum(1 for r in results if r.get("revert") == REVERTED),
        "correlation_id": correlation_id,
        "results": results,
    }
    emit(record, rt.settings.metric_namespace)
    return record


def emit(record: dict[str, Any], namespace: str) -> None:
    """One log line per run that is also a CloudWatch embedded metric format record. Only
    plan ids and words the server set are logged."""
    metrics = ("Runs", "Due", "Processed", "InvokeErrors", "Degraded", "Reverts")
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
                }
            ],
        },
        **record,
    }
    print(json.dumps(line, default=str), flush=True)


_runtime: Runtime | None = None


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    global _runtime
    if _runtime is None:
        settings = load_verifier_settings()
        _runtime = Runtime(
            settings=settings,
            store=PlanStore.from_name(settings.plans_table, settings.region),
            lambda_client=boto3.client("lambda", region_name=settings.region),
        )
    return run(_runtime, getattr(context, "aws_request_id", None))
