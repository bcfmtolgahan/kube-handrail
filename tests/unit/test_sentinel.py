import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handrail_common.config import ConfigError
from handrail_common.drift import load_classification

# Load by path: every Lambda directory contains a module named `handler`.
_SPEC = importlib.util.spec_from_file_location(
    "sentinel_handler", Path(__file__).parents[2] / "mcp" / "sentinel" / "handler.py"
)
handler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handler)

REPO = Path(__file__).parents[2]
CLASSIFICATION = load_classification(REPO / "mcp" / "classification.json")
REGION = "us-east-1"
PARAM = "/handrail/remediation-enabled"
TOPIC = "alert-topic-arn"
WRITE_TOOL = "k8sw___apply_scale_deployment"


def schema_names(directory: str) -> list[str]:
    return [t["name"] for t in json.loads((REPO / "mcp" / directory / "tools.json").read_text())]


def throttled(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, operation)


class FakeGateway:
    def __init__(self) -> None:
        self.targets = {
            "k8sr": schema_names("k8s_read"),
            "k8sw": schema_names("k8s_write"),
        }
        self.status = "READY"
        self.error: Exception | None = None

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):
        if self.error:
            raise self.error
        # One target per page, to exercise pagination.
        names = sorted(self.targets)
        index = int(nextToken or 0)
        page = {"items": [{"name": names[index], "targetId": f"id-{names[index]}"}]}
        if index + 1 < len(names):
            page["nextToken"] = str(index + 1)
        return page

    def get_gateway_target(self, gatewayIdentifier, targetId):
        name = targetId.removeprefix("id-")
        payload = [{"name": n, "description": "", "inputSchema": {"type": "object"}} for n in self.targets[name]]
        return {
            "name": name,
            "status": self.status,
            "targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": payload}}}},
        }


class FakeDevOps:
    def __init__(self) -> None:
        self.classifications = dict(CLASSIFICATION)
        self.preferences = {"elevatedActionsEnabled": True}
        self.error: Exception | None = None

    def get_association(self, agentSpaceId, associationId):
        if self.error:
            raise self.error
        tools = sorted(self.classifications)
        return {
            "association": {
                "associationId": associationId,
                "configuration": {
                    "mcpserver": {
                        "tools": tools,
                        "toolDetails": [
                            {"name": t, "toolClassification": c}
                            for t, c in self.classifications.items()
                            if c is not None
                        ],
                    }
                },
            }
        }

    def get_agent_space(self, agentSpaceId):
        return {"agentSpace": {"agentSpaceId": agentSpaceId, "preferences": self.preferences}}


class FakeSNS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def publish(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "m"}


def settings(devops: bool = True, expect_elevated: bool = True) -> handler.SentinelSettings:
    return handler.SentinelSettings(
        gateway_id="gw",
        kill_switch_param=PARAM,
        alert_topic_arn=TOPIC,
        metric_namespace="kube-handrail/sentinel",
        classification=dict(CLASSIFICATION),
        agent_space_id="space" if devops else None,
        association_id="assoc" if devops else None,
        expect_elevated_actions=expect_elevated,
    )


@pytest.fixture
def ssm(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("ssm", region_name=REGION)
        client.put_parameter(Name=PARAM, Value="true", Type="String")
        yield client


def kill_switch(ssm) -> tuple[str, int]:
    parameter = ssm.get_parameter(Name=PARAM)["Parameter"]
    return parameter["Value"], parameter["Version"]


def runtime(ssm, devops: FakeDevOps | None = None, **kwargs) -> handler.Runtime:
    return handler.Runtime(
        settings=settings(devops=devops is not None, **kwargs),
        gateway=FakeGateway(),
        devops=devops,
        ssm=ssm,
        sns=FakeSNS(),
    )


def test_no_drift_leaves_kill_switch_alone(ssm, capsys):
    rt = runtime(ssm, FakeDevOps())
    assert handler.run(rt) == {"status": "ok", "findings": []}
    assert kill_switch(ssm) == ("true", 1)
    assert rt.sns.messages == []
    record = json.loads(capsys.readouterr().out)
    assert (record["Drift"], record["Unknown"]) == (0, 0)
    assert record["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "kube-handrail/sentinel"


def test_write_tool_downgraded_turns_kill_switch_off(ssm):
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = "READ_ONLY"
    rt = runtime(ssm, devops)
    result = handler.run(rt)
    assert result["status"] == "drift"
    assert result["kill_switch"] == "turned_off"
    assert result["findings"] == [
        {"check": "association", "issue": "not_mutative", "subject": WRITE_TOOL, "actual": "READ_ONLY"}
    ]
    assert kill_switch(ssm)[0] == "false"
    [message] = rt.sns.messages
    assert message["TopicArn"] == TOPIC
    assert WRITE_TOOL in message["Message"] and "turned_off" in message["Message"]


def test_write_tool_without_tool_details_counts_as_read_only(ssm):
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = None
    result = handler.run(runtime(ssm, devops))
    assert result["status"] == "drift"
    assert result["findings"][0]["issue"] == "not_mutative"
    assert kill_switch(ssm)[0] == "false"


def test_drift_when_already_off_still_alerts(ssm):
    ssm.put_parameter(Name=PARAM, Value="false", Type="String", Overwrite=True)
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = "READ_ONLY"
    rt = runtime(ssm, devops)
    result = handler.run(rt)
    assert result["kill_switch"] == "already_off"
    assert kill_switch(ssm) == ("false", 2)
    assert len(rt.sns.messages) == 1


@pytest.mark.parametrize(
    "change, issue, subject",
    [
        (lambda t: t["k8sw"].append("delete_deployment"), "extra_tool", "k8sw___delete_deployment"),
        (lambda t: t["k8sr"].remove("get_workload_status"), "missing_tool", "k8sr___get_workload_status"),
        (lambda t: t.update(evil=["run"]), "extra_target", "evil"),
        (lambda t: t.pop("k8sw"), "missing_target", "k8sw"),
    ],
)
def test_gateway_tool_set_drift(ssm, change, issue, subject):
    rt = runtime(ssm)
    change(rt.gateway.targets)
    result = handler.run(rt)
    assert result["status"] == "drift"
    assert result["findings"] == [{"check": "gateway", "issue": issue, "subject": subject}]
    assert kill_switch(ssm)[0] == "false"
    assert len(rt.sns.messages) == 1


def test_extra_or_missing_associated_tool_is_drift(ssm):
    devops = FakeDevOps()
    devops.classifications["k8sw___delete_deployment"] = "MUTATIVE"
    del devops.classifications["k8sr___get_workload_status"]
    result = handler.run(runtime(ssm, devops))
    assert [(f["issue"], f["subject"]) for f in result["findings"]] == [
        ("extra_tool", "k8sw___delete_deployment"),
        ("missing_tool", "k8sr___get_workload_status"),
    ]


@pytest.mark.parametrize("preferences", [{"elevatedActionsEnabled": False}, {}])
def test_elevated_actions_off_when_expected_on(ssm, preferences):
    devops = FakeDevOps()
    devops.preferences = preferences
    result = handler.run(runtime(ssm, devops))
    assert result["status"] == "drift"
    assert result["findings"] == [
        {
            "check": "agent_space",
            "issue": "elevated_actions",
            "subject": "elevatedActionsEnabled",
            "expected": True,
            "actual": False,
        }
    ]
    assert kill_switch(ssm)[0] == "false"


def test_elevated_actions_on_when_expected_off(ssm):
    result = handler.run(runtime(ssm, FakeDevOps(), expect_elevated=False))
    assert result["status"] == "drift"


@pytest.mark.parametrize("where", ["gateway", "devops"])
def test_api_error_without_findings_is_unknown(ssm, capsys, where):
    devops = FakeDevOps()
    rt = runtime(ssm, devops)
    error = throttled("ListGatewayTargets" if where == "gateway" else "GetAssociation")
    if where == "gateway":
        rt.gateway.error = error
    else:
        devops.error = error
    result = handler.run(rt)
    assert result["status"] == "unknown"
    assert result["errors"][0].endswith("ThrottlingException")
    assert kill_switch(ssm) == ("true", 1)
    assert rt.sns.messages == []
    record = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (record["Drift"], record["Unknown"]) == (0, 1)


def test_drift_seen_by_one_check_is_acted_on_when_another_check_fails(ssm):
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = "READ_ONLY"
    rt = runtime(ssm, devops)
    rt.gateway.error = throttled("ListGatewayTargets")
    result = handler.run(rt)
    assert result["status"] == "drift"
    assert result["errors"] == ["ListGatewayTargets: ThrottlingException"]
    assert kill_switch(ssm)[0] == "false"
    assert len(rt.sns.messages) == 1


def test_association_drift_is_acted_on_when_the_agent_space_cannot_be_read(ssm):
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = "READ_ONLY"
    devops.get_agent_space = lambda **kw: (_ for _ in ()).throw(throttled("GetAgentSpace"))
    result = handler.run(runtime(ssm, devops))
    assert result["status"] == "drift"
    assert kill_switch(ssm)[0] == "false"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda gw: setattr(gw, "status", "UPDATING"),
        lambda gw: setattr(gw, "get_gateway_target", lambda **kw: {"name": "k8sr", "status": "READY"}),
    ],
)
def test_unreadable_or_transitional_target_is_unknown(ssm, mutate):
    rt = runtime(ssm)
    mutate(rt.gateway)
    assert handler.run(rt)["status"] == "unknown"
    assert kill_switch(ssm) == ("true", 1)


def test_without_devops_ids_only_gateway_is_checked(ssm):
    rt = runtime(ssm, devops=None)
    assert rt.settings.agent_space_id is None
    assert handler.run(rt) == {"status": "ok", "findings": []}


class FailingSSM:
    def get_parameter(self, Name):
        raise throttled("GetParameter")

    def put_parameter(self, **kwargs):
        raise throttled("PutParameter")


def test_failed_kill_switch_write_still_alerts_and_fails_the_run(ssm):
    devops = FakeDevOps()
    devops.classifications[WRITE_TOOL] = "READ_ONLY"
    rt = runtime(ssm, devops)
    rt.ssm = FailingSSM()
    with pytest.raises(RuntimeError):
        handler.run(rt)
    [message] = rt.sns.messages
    assert "write_failed" in message["Message"]


ENV = {
    "GATEWAY_ID": "gw",
    "REMEDIATION_ENABLED_PARAM": PARAM,
    "ALERT_TOPIC_ARN": TOPIC,
    "METRIC_NAMESPACE": "ns",
    "TOOL_CLASSIFICATION": json.dumps(CLASSIFICATION),
}


def test_settings_parse():
    s = handler.load_sentinel_settings(ENV)
    assert s.classification == CLASSIFICATION
    assert (s.agent_space_id, s.association_id, s.expect_elevated_actions) == (None, None, True)
    s = handler.load_sentinel_settings(
        ENV | {"AGENT_SPACE_ID": "a", "ASSOCIATION_ID": "b", "EXPECT_ELEVATED_ACTIONS": "false"}
    )
    assert (s.agent_space_id, s.association_id, s.expect_elevated_actions) == ("a", "b", False)


@pytest.mark.parametrize(
    "override",
    [
        {"GATEWAY_ID": ""},
        {"AGENT_SPACE_ID": "a"},
        {"EXPECT_ELEVATED_ACTIONS": "yes"},
        {"TOOL_CLASSIFICATION": "{}"},
        {"TOOL_CLASSIFICATION": json.dumps({WRITE_TOOL: "DESTRUCTIVE"})},
        {"TOOL_CLASSIFICATION": "not json"},
    ],
)
def test_settings_invalid(override):
    with pytest.raises(ConfigError):
        handler.load_sentinel_settings(ENV | override)


def test_configuration_error_is_unknown(monkeypatch):
    monkeypatch.setattr(handler, "_runtime", None)
    monkeypatch.delenv("GATEWAY_ID", raising=False)
    assert handler.lambda_handler({}, None)["status"] == "unknown"
