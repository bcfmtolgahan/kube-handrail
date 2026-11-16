"""The e2e script resets budget counters only on a test deployment's table, and fills
unset variables from the Terraform outputs without overriding any that are set."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "e2e"))

import e2e_gateway  # noqa: E402

from handrail_common.plans import (  # noqa: E402
    agent_budget_key,
    approval_request_key,
    global_budget_key,
)

NOW = 1_800_001_234


@pytest.mark.parametrize(
    ("table", "test"),
    [
        ("kh-e2e-plans", True),
        ("e2e-plans", True),
        ("kube-handrail-test-plans", True),
        ("team-ci-handrail-plans", True),
        ("kube-handrail-plans", False),
        ("prod-plans", False),
        ("latest-plans", False),
        ("kh-e2e-plans-backup", False),
        ("kh-contest-plans", False),
        ("kh-e2e", False),
        ("kh-test-plans\n", False),
    ],
)
def test_only_a_test_table_is_reset(table, test):
    assert e2e_gateway.is_test_table(table) is test


def test_counter_keys_match_the_server():
    keys = e2e_gateway.counter_keys(("smoke_test", "e2e_peer"), NOW)
    assert keys == [
        global_budget_key(NOW),
        agent_budget_key("smoke_test", NOW),
        agent_budget_key("e2e_peer", NOW),
        approval_request_key(NOW),
    ]


class Table:
    def __init__(self):
        self.deleted = []

    def delete_item(self, Key):
        self.deleted.append(Key["plan_id"])


def test_reset_refuses_a_production_table_before_any_call():
    table = Table()
    with pytest.raises(SystemExit):
        e2e_gateway.reset_counters("kube-handrail-plans", ("smoke_test",), table)
    assert table.deleted == []


def test_reset_deletes_the_current_counters(monkeypatch):
    monkeypatch.setattr(e2e_gateway.time, "time", lambda: NOW)
    table = Table()
    e2e_gateway.reset_counters("kh-e2e-plans", ("smoke_test",), table)
    assert table.deleted == [
        global_budget_key(NOW),
        agent_budget_key("smoke_test", NOW),
        approval_request_key(NOW),
    ]


OUTPUTS = {
    "gateway_url": "https://gateway.example",
    "plans_table_name": "kh-e2e-plans",
    "plan_ttl_seconds": 60,
    "audit_bucket_name": None,
    "agent_client_ids": {"smoke_test": "client-a", "e2e_peer": "client-b"},
    "guard_settings": {
        "apply_cooldown_seconds": 20,
        "agent_budgets": {"e2e_peer": 2},
        "gitops_markers": ["app.kubernetes.io/instance"],
    },
}


def test_outputs_fill_only_unset_variables():
    env = {"GATEWAY_URL": "https://override.example", "PLAN_TTL_SECONDS": ""}
    filled = e2e_gateway.env_from_outputs(OUTPUTS, env)
    assert filled == {
        "PLANS_TABLE": "kh-e2e-plans",
        "PLAN_TTL_SECONDS": "60",
        "APPLY_COOLDOWN_SECONDS": "20",
        "CLIENT_ID": "client-a",
        "CLIENT_ID_B": "client-b",
        "AGENT_BUDGET_B": "2",
        "GITOPS_MARKERS": '["app.kubernetes.io/instance"]',
    }


def test_no_outputs_fill_nothing():
    assert e2e_gateway.env_from_outputs({}, {}) == {}


@pytest.mark.parametrize(
    ("raw", "markers"),
    [
        (None, []),
        ("", []),
        ("[]", []),
        ('["app.kubernetes.io/instance"]', ["app.kubernetes.io/instance"]),
        ('"app.kubernetes.io/instance"', []),
    ],
)
def test_gitops_markers_from_the_environment(monkeypatch, raw, markers):
    if raw is None:
        monkeypatch.delenv("GITOPS_MARKERS", raising=False)
    else:
        monkeypatch.setenv("GITOPS_MARKERS", raw)
    assert e2e_gateway.gitops_markers() == markers


def test_outputs_of_a_test_deployment_are_used():
    assert e2e_gateway.usable_outputs(OUTPUTS) == (OUTPUTS, "")


@pytest.mark.parametrize("table", ["kube-handrail-plans", None, "", 7])
def test_outputs_of_any_other_deployment_are_ignored(table):
    outputs = OUTPUTS | {"plans_table_name": table}
    used, reason = e2e_gateway.usable_outputs(outputs)
    assert used == {}
    assert reason


def test_no_outputs_are_not_reported():
    assert e2e_gateway.usable_outputs({}) == ({}, "")
