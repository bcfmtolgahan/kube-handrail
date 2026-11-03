import importlib.util
from pathlib import Path

import pytest

from handrail_common.drift import (
    MUTATIVE,
    READ_ONLY,
    UnexpectedResponse,
    association_classifications,
    compare_association,
    compare_gateway,
    elevated_actions,
    gateway_target_tools,
    load_classification,
    parse_classification,
    tools_by_target,
)

REPO = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "devops_agent_register", REPO / "scripts" / "devops_agent_register.py"
)
register = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(register)


def test_repository_classification():
    assert load_classification(REPO / "mcp" / "classification.json") == {
        "k8sr___get_workload_events": READ_ONLY,
        "k8sr___get_workload_status": READ_ONLY,
        "k8sr___plan_scale_deployment": READ_ONLY,
        "k8sr___request_approval": READ_ONLY,
        "k8sw___apply_scale_deployment": MUTATIVE,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {},
        [],
        {"no_delimiter": READ_ONLY},
        {"a___b": "DESTRUCTIVE"},
        # A write tool can never be READ_ONLY, so a tampered expectation is refused.
        {"k8sw___apply_scale_deployment": READ_ONLY},
        {"k8sw___anything": READ_ONLY},
        {"k8sr___apply_restart": READ_ONLY},
    ],
)
def test_invalid_classification(raw):
    with pytest.raises(ValueError):
        parse_classification(raw)


def test_tools_by_target():
    assert tools_by_target({"a___x": READ_ONLY, "a___y": READ_ONLY, "b___z": MUTATIVE}) == {
        "a": {"a___x", "a___y"},
        "b": {"b___z"},
    }


def test_association_defaults_to_read_only():
    response = {
        "association": {
            "configuration": {
                "mcpserver": {
                    "tools": ["a___x", "b___z"],
                    "toolDetails": [{"name": "b___z", "toolClassification": MUTATIVE}],
                }
            }
        }
    }
    assert association_classifications(response) == {"a___x": READ_ONLY, "b___z": MUTATIVE}


@pytest.mark.parametrize(
    "response", [{}, {"association": None}, {"association": {"configuration": []}}]
)
def test_unreadable_association(response):
    with pytest.raises(UnexpectedResponse):
        association_classifications(response)


@pytest.mark.parametrize(
    "response", [{}, {"agentSpace": {"preferences": {"elevatedActionsEnabled": "true"}}}]
)
def test_unreadable_agent_space(response):
    with pytest.raises(UnexpectedResponse):
        elevated_actions(response)


def test_gateway_target_tools():
    response = {
        "name": "k8sw",
        "status": "READY",
        "targetConfiguration": {
            "mcp": {"lambda": {"toolSchema": {"inlinePayload": [{"name": "apply"}]}}}
        },
    }
    assert gateway_target_tools(response) == {"k8sw___apply"}


def test_compare_gateway_ok():
    assert compare_gateway({"a": {"a___x"}}, {"a": {"a___x"}}) == []


def test_compare_association_allows_stricter_read_tool():
    # A read tool classified MUTATIVE only adds an approval; it is not drift.
    expected = {"a___x": READ_ONLY, "b___z": MUTATIVE}
    assert compare_association(expected, {"a___x": MUTATIVE, "b___z": MUTATIVE}) == []
    assert (
        compare_association(expected, {"a___x": READ_ONLY, "b___z": "DESTRUCTIVE"})[0]["issue"]
        == "not_mutative"
    )


class RecordingClient:
    def __init__(self, classifications):
        self.classifications = classifications
        self.updates = []

    def get_association(self, agentSpaceId, associationId):
        tools = sorted(self.classifications)
        details = [{"name": t, "toolClassification": c} for t, c in self.classifications.items()]
        return {
            "association": {
                "configuration": {"mcpserver": {"tools": tools, "toolDetails": details}}
            }
        }

    def update_association(self, agentSpaceId, associationId, configuration):
        self.updates.append(configuration)
        details = configuration["mcpserver"]["toolDetails"]
        self.classifications = {d["name"]: d["toolClassification"] for d in details}

    def get_agent_space(self, agentSpaceId):
        return {"agentSpace": {"preferences": {"other": True, **getattr(self, "preferences", {})}}}

    def update_agent_space(self, agentSpaceId, preferences):
        self.preferences = preferences


def test_enable_write_classifies_from_file(capsys):
    client = RecordingClient(
        {"k8sr___get_workload_status": READ_ONLY, "k8sr___plan_scale_deployment": READ_ONLY}
    )
    args = type("Args", (), {"agent_space_id": "s", "association_id": "a"})()
    register.enable_write(client, args)
    [configuration] = client.updates
    assert configuration["mcpserver"]["tools"] == sorted(configuration["mcpserver"]["tools"])
    assert client.classifications == load_classification(REPO / "mcp" / "classification.json")
    assert client.preferences == {"other": True, "elevatedActionsEnabled": True}


def test_associate_read_uses_read_only_tools():
    class Client:
        def associate_service(self, agentSpaceId, serviceId, configuration):
            self.configuration = configuration
            return {"association": {"associationId": "a"}}

    client = Client()
    register.associate_read(client, type("Args", (), {"agent_space_id": "s", "service_id": "v"})())
    assert client.configuration["mcpserver"]["toolDetails"] == [
        {"name": "k8sr___get_workload_events", "toolClassification": READ_ONLY},
        {"name": "k8sr___get_workload_status", "toolClassification": READ_ONLY},
        {"name": "k8sr___plan_scale_deployment", "toolClassification": READ_ONLY},
        {"name": "k8sr___request_approval", "toolClassification": READ_ONLY},
    ]
