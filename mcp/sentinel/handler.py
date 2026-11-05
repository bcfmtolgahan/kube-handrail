"""Drift sentinel Lambda: turns the kill switch off when the deployed tools or their
classification no longer match mcp/classification.json, or the gateway lost the caller
interceptor or allows a client that is not an agent's. Not a gateway target.
"""

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from handrail_common.config import ConfigError
from handrail_common.drift import (
    Finding,
    association_classifications,
    compare_association,
    compare_elevated,
    compare_gateway,
    compare_gateway_configuration,
    elevated_actions,
    gateway_target_tools,
    parse_classification,
    tools_by_target,
)

logger = logging.getLogger(__name__)

OK = "ok"
DRIFT = "drift"
UNKNOWN = "unknown"
DISABLED = "false"
ALERT_SUBJECT = "kube-handrail: drift, remediation disabled"


@dataclass(frozen=True)
class SentinelSettings:
    gateway_id: str
    kill_switch_param: str
    alert_topic_arn: str
    metric_namespace: str
    classification: dict[str, str]
    agent_space_id: str | None
    association_id: str | None
    expect_elevated_actions: bool
    # The REQUEST interceptor's Lambda ARN; None when none is deployed (legacy mode).
    expect_interceptor_arn: str | None = None
    # The agents' OAuth clients: the only ones the gateway may allow.
    expect_allowed_clients: frozenset[str] = frozenset()


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def load_sentinel_settings(env: Mapping[str, str] | None = None) -> SentinelSettings:
    env = os.environ if env is None else env
    try:
        classification = parse_classification(json.loads(_required(env, "TOOL_CLASSIFICATION")))
    except ValueError as exc:
        raise ConfigError(f"TOOL_CLASSIFICATION: {exc}") from None
    space = env.get("AGENT_SPACE_ID", "").strip() or None
    association = env.get("ASSOCIATION_ID", "").strip() or None
    if (space is None) != (association is None):
        raise ConfigError("set both AGENT_SPACE_ID and ASSOCIATION_ID, or neither")
    elevated = env.get("EXPECT_ELEVATED_ACTIONS", "true").strip()
    if elevated not in ("true", "false"):
        raise ConfigError("EXPECT_ELEVATED_ACTIONS must be true or false")
    try:
        clients = json.loads(_required(env, "EXPECT_ALLOWED_CLIENTS"))
    except ValueError:
        clients = None
    if not isinstance(clients, list) or not clients or not all(isinstance(c, str) for c in clients):
        raise ConfigError("EXPECT_ALLOWED_CLIENTS must be a non-empty JSON list of client IDs")
    return SentinelSettings(
        gateway_id=_required(env, "GATEWAY_ID"),
        kill_switch_param=_required(env, "REMEDIATION_ENABLED_PARAM"),
        alert_topic_arn=_required(env, "ALERT_TOPIC_ARN"),
        metric_namespace=_required(env, "METRIC_NAMESPACE"),
        classification=classification,
        agent_space_id=space,
        association_id=association,
        expect_elevated_actions=elevated == "true",
        expect_interceptor_arn=env.get("EXPECT_INTERCEPTOR_ARN", "").strip() or None,
        expect_allowed_clients=frozenset(clients),
    )


@dataclass
class Runtime:
    settings: SentinelSettings
    gateway: Any
    devops: Any | None
    ssm: Any
    sns: Any


def deployed_tools(
    gateway: Any, gateway_id: str, expected: set[str]
) -> tuple[dict[str, set[str]], list[str]]:
    """Tools per expected target, and the names of targets that are not expected."""
    target_ids: dict[str, str] = {}
    params: dict[str, Any] = {"gatewayIdentifier": gateway_id}
    while True:
        page = gateway.list_gateway_targets(**params)
        target_ids.update({item["name"]: item["targetId"] for item in page["items"]})
        if not page.get("nextToken"):
            break
        params["nextToken"] = page["nextToken"]
    actual = {
        name: gateway_target_tools(
            gateway.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        )
        for name, target_id in target_ids.items()
        if name in expected
    }
    return actual, sorted(set(target_ids) - expected)


def check(rt: Runtime) -> tuple[list[Finding], list[str]]:
    """Findings and errors. Each check runs on its own, so drift seen by one check is
    acted on even when another check's API call fails."""
    s = rt.settings
    expected = tools_by_target(s.classification)

    def gateway() -> list[Finding]:
        actual, others = deployed_tools(rt.gateway, s.gateway_id, set(expected))
        return compare_gateway(expected, actual, others)

    def gateway_config() -> list[Finding]:
        response = rt.gateway.get_gateway(gatewayIdentifier=s.gateway_id)
        return compare_gateway_configuration(
            s.expect_interceptor_arn, s.expect_allowed_clients, response
        )

    def association() -> list[Finding]:
        response = rt.devops.get_association(
            agentSpaceId=s.agent_space_id, associationId=s.association_id
        )
        return compare_association(s.classification, association_classifications(response))

    def elevated() -> list[Finding]:
        space = rt.devops.get_agent_space(agentSpaceId=s.agent_space_id)
        return compare_elevated(s.expect_elevated_actions, elevated_actions(space))

    checks = [gateway, gateway_config]
    if rt.devops is not None:
        checks += [association, elevated]
    findings: list[Finding] = []
    errors: list[str] = []
    for one in checks:
        try:
            findings += one()
        except Exception as exc:
            # Throttling, outages and unreadable responses are not evidence of drift.
            logger.warning("%s check failed: %s", one.__name__, describe_error(exc))
            errors.append(describe_error(exc))
    return findings, errors


def describe_error(exc: Exception) -> str:
    """Error code and operation only: messages can carry ARNs and account IDs."""
    if isinstance(exc, ClientError):
        return f"{exc.operation_name}: {exc.response.get('Error', {}).get('Code', 'unknown')}"
    return type(exc).__name__


def disable_remediation(ssm: Any, name: str) -> str:
    """Set the kill switch to "false". It is never set back to "true" here."""
    try:
        current = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception:
        logger.exception("cannot read kill switch parameter; writing it anyway")
        current = None
    if current == DISABLED:
        return "already_off"
    ssm.put_parameter(Name=name, Value=DISABLED, Type="String", Overwrite=True)
    return "turned_off"


def alert_message(findings: list[Finding], kill_switch: str) -> str:
    lines = [
        "The kube-handrail drift sentinel found deployed tools or classifications that do not "
        "match mcp/classification.json, or a gateway configuration that does not match the "
        "deployment (caller interceptor, allowed clients).",
        f"Kill switch: {kill_switch}.",
        "",
        "Findings:",
        *(f"- {f['check']} {f['issue']}: {f['subject']}" for f in findings),
        "",
        "Restore: fix the registration and re-run devops_agent_register.py enable-write (it "
        "verifies the classification), or re-apply Terraform for gateway drift, then set the "
        "kill switch parameter back to true by hand. See mcp/CONTRACT.md, Drift sentinel.",
    ]
    return "\n".join(lines)


def emit(result: dict[str, Any], namespace: str) -> None:
    """One log line that is also a CloudWatch embedded metric format record."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [[]],
                    "Metrics": [
                        {"Name": "Drift", "Unit": "Count"},
                        {"Name": "Unknown", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Drift": int(result["status"] == DRIFT),
        "Unknown": int(result["status"] == UNKNOWN),
        **result,
    }
    print(json.dumps(record, default=str))


def respond_to_drift(rt: Runtime, findings: list[Finding], errors: list[str]) -> dict[str, Any]:
    s = rt.settings
    failed: list[str] = []
    try:
        kill_switch = disable_remediation(rt.ssm, s.kill_switch_param)
    except Exception:
        logger.exception("cannot turn the kill switch off")
        kill_switch, failed = "write_failed", ["kill_switch"]
    try:
        rt.sns.publish(
            TopicArn=s.alert_topic_arn,
            Subject=ALERT_SUBJECT,
            Message=alert_message(findings, kill_switch),
        )
    except Exception:
        logger.exception("cannot publish the drift alert")
        failed.append("alert")
    result = {
        "status": DRIFT,
        "findings": findings,
        "kill_switch": kill_switch,
        "failed": failed,
        "errors": errors,
    }
    emit(result, s.metric_namespace)
    if failed:
        # Fail the invocation so it is retried and the function's error alarm fires.
        raise RuntimeError(f"drift response incomplete: {failed}")
    return result


def run(rt: Runtime) -> dict[str, Any]:
    findings, errors = check(rt)
    if findings:
        # Acting on partial evidence is safe: the only action is turning remediation off.
        return respond_to_drift(rt, findings, errors)
    if errors:
        result = {"status": UNKNOWN, "findings": [], "errors": errors}
        emit(result, rt.settings.metric_namespace)
        return result
    result: dict[str, Any] = {"status": OK, "findings": []}
    emit(result, rt.settings.metric_namespace)
    return result


def build_runtime(settings: SentinelSettings) -> Runtime:
    configured = settings.agent_space_id is not None
    return Runtime(
        settings=settings,
        gateway=boto3.client("bedrock-agentcore-control"),
        devops=boto3.client("devops-agent") if configured else None,
        ssm=boto3.client("ssm"),
        sns=boto3.client("sns"),
    )


_runtime: Runtime | None = None


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    global _runtime
    if _runtime is None:
        try:
            _runtime = build_runtime(load_sentinel_settings())
        except ConfigError as exc:
            logger.error("configuration error: %s", exc)
            result = {"status": UNKNOWN, "findings": [], "error": "configuration"}
            emit(result, os.environ.get("METRIC_NAMESPACE", "kube-handrail/sentinel"))
            return result
    return run(_runtime)
