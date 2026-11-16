"""The namespace policy (namespace_policy.json), the action guard and the plan-time
admission policy check (mcp/CONTRACT.md, Namespace policy and Admission policy)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from kubernetes.client.rest import ApiException

from handrail_common import admission
from handrail_common.config import (
    ACTIONS,
    ConfigError,
    NamespacePolicy,
    Settings,
    action_name,
    load_settings,
    parse_namespace_policy,
)
from handrail_common.validation import (
    GuardError,
    check_action_allowed,
    validate_scale_args,
    validate_target_args,
)

AGENTS = '{"client-a": {"name": "smoke_test"}}'
BASE = {
    "CLUSTER_NAME": "demo",
    "PLANS_TABLE": "plans",
    "ALLOWED_NAMESPACES": "team-a,team-b",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
    "AGENTS": AGENTS,
    "AGENT_KILL_SWITCH_PREFIX": "/kh/agents/",
}
ROLE = "arn:aws:iam::123456789012:role/team-a-approver"
POLICY = {
    "namespaces": {
        "team-a": {
            "min_replicas": 1,
            "max_replicas": 3,
            "actions": ["restart"],
            "approvers": [ROLE],
            "gitops_markers": ["example.com/owner"],
        },
        "team-b": {
            "min_replicas": 0,
            "max_replicas": 8,
            "actions": ["restart", "rollback", "scale"],
            "approvers": [],
            "gitops_markers": [],
        },
    }
}


def write_policy(tmp_path, policy=POLICY) -> str:
    path = tmp_path / "namespace_policy.json"
    path.write_text(policy if isinstance(policy, str) else json.dumps(policy))
    return str(path)


def settings_from(tmp_path, policy=POLICY, **env) -> Settings:
    return load_settings({**BASE, "NAMESPACE_POLICY_FILE": write_policy(tmp_path, policy), **env})


# --- Loading ---------------------------------------------------------------------------


def test_policy_gives_each_namespace_its_bounds_actions_and_markers(tmp_path):
    s = settings_from(tmp_path, GITOPS_MARKERS='["example.com/global"]')
    assert s.allowed_namespaces == {"team-a", "team-b"}
    assert s.bounds_for("team-a") == (1, 3)
    assert s.bounds_for("team-b") == (0, 8)
    assert s.actions_for("team-a") == {"restart"}
    assert s.actions_for("team-b") == set(ACTIONS)
    assert s.markers_for("team-a") == ("example.com/global", "example.com/owner")
    assert s.markers_for("team-b") == ("example.com/global",)
    assert s.namespace_policy["team-a"].approvers == (ROLE,)


def test_without_a_policy_file_every_namespace_gets_the_legacy_bounds_and_every_action():
    """An older package, or a test harness: the environment alone, as before."""
    s = load_settings(BASE)
    assert s.namespace_policy == {}
    assert s.bounds_for("team-a") == (0, 5)
    assert s.actions_for("team-b") == set(ACTIONS)
    assert s.markers_for("team-a") == ()


def test_the_policy_must_list_exactly_the_allowed_namespaces(tmp_path):
    with pytest.raises(ConfigError, match="ALLOWED_NAMESPACES does not match"):
        settings_from(tmp_path, ALLOWED_NAMESPACES="team-a")


def test_a_named_policy_file_that_is_missing_fails_the_load(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings({**BASE, "NAMESPACE_POLICY_FILE": str(tmp_path / "missing.json")})


@pytest.mark.parametrize(
    "policy",
    [
        "not json",
        "[]",
        {"namespaces": {}},
        {"namespaces": {"Team_A": {"min_replicas": 0, "max_replicas": 1, "actions": []}}},
        {"namespaces": {"a": {"min_replicas": 2, "max_replicas": 1, "actions": []}}},
        {"namespaces": {"a": {"min_replicas": -1, "max_replicas": 1, "actions": []}}},
        {"namespaces": {"a": {"min_replicas": True, "max_replicas": 1, "actions": []}}},
        {"namespaces": {"a": {"min_replicas": 0, "max_replicas": 1, "actions": ["delete"]}}},
        {"namespaces": {"a": {"min_replicas": 0, "max_replicas": 1}}},
        {
            "namespaces": {
                "a": {"min_replicas": 0, "max_replicas": 1, "actions": [], "approvers": ""}
            }
        },
        {
            "namespaces": {
                "a": {"min_replicas": 0, "max_replicas": 1, "actions": [], "gitops_markers": ["/"]}
            }
        },
    ],
)
def test_a_malformed_policy_fails_the_load(policy):
    with pytest.raises(ConfigError):
        parse_namespace_policy(policy if isinstance(policy, str) else json.dumps(policy))


def test_a_namespace_may_allow_no_action():
    policy = parse_namespace_policy(
        json.dumps({"namespaces": {"a": {"min_replicas": 0, "max_replicas": 1, "actions": []}}})
    )
    assert policy["a"].actions == frozenset()


@pytest.mark.parametrize(("raw", "expected"), [("", True), ("true", True), ("false", False)])
def test_admission_check_is_on_unless_explicitly_false(raw, expected):
    assert load_settings({**BASE, "ADMISSION_CHECK": raw}).admission_check is expected


def test_admission_check_rejects_anything_else():
    with pytest.raises(ConfigError, match="ADMISSION_CHECK"):
        load_settings({**BASE, "ADMISSION_CHECK": "no"})


def test_action_names():
    assert action_name("scale_deployment") == "scale"
    assert action_name("rollback_deployment") == "rollback"


# --- The action guard ------------------------------------------------------------------

SETTINGS = Settings(
    cluster_name="demo",
    plans_table="plans",
    allowed_namespaces=frozenset({"team-a", "team-b"}),
    min_replicas=0,
    max_replicas=5,
    plan_ttl_seconds=300,
    remediation_enabled_param=None,
    region=None,
    namespace_policy={
        "team-a": NamespacePolicy(1, 3, frozenset({"restart"})),
        "team-b": NamespacePolicy(0, 8),
    },
)


def test_an_action_the_namespace_does_not_allow_is_forbidden_namespace_with_details():
    with pytest.raises(GuardError) as exc:
        check_action_allowed("team-a", "scale", {"restart"})
    assert exc.value.code == "forbidden_namespace"
    assert exc.value.details == {"action": "scale", "allowed_actions": ["restart"]}


def test_scale_is_refused_by_action_before_the_bounds():
    """A namespace that does not allow scale says so, not that the count is out of bounds."""
    args = {"namespace": "team-a", "name": "web", "replicas": 9}
    with pytest.raises(GuardError) as exc:
        validate_scale_args(
            args, SETTINGS.allowed_namespaces, SETTINGS.bounds_for, SETTINGS.actions_for
        )
    assert (exc.value.code, exc.value.details["action"]) == ("forbidden_namespace", "scale")


def test_scale_uses_the_namespaces_own_bounds():
    args = {"namespace": "team-b", "name": "web", "replicas": 8}
    allowed = SETTINGS.allowed_namespaces
    assert validate_scale_args(args, allowed, SETTINGS.bounds_for, SETTINGS.actions_for)[2] == 8
    with pytest.raises(GuardError) as exc:
        validate_scale_args(
            {**args, "replicas": 9}, allowed, SETTINGS.bounds_for, SETTINGS.actions_for
        )
    assert exc.value.code == "out_of_bounds"


def test_an_unknown_namespace_stays_forbidden_without_details():
    with pytest.raises(GuardError) as exc:
        validate_target_args(
            {"namespace": "kube-system", "name": "x"},
            SETTINGS.allowed_namespaces,
            "restart",
            SETTINGS.actions_for,
        )
    assert exc.value.code == "forbidden_namespace" and exc.value.details is None


def test_restart_is_allowed_where_the_policy_allows_it():
    target = {"namespace": "team-a", "name": "web"}
    assert validate_target_args(
        target, SETTINGS.allowed_namespaces, "restart", SETTINGS.actions_for
    ) == ("team-a", "web")
    with pytest.raises(GuardError):
        validate_target_args(target, SETTINGS.allowed_namespaces, "rollback", SETTINGS.actions_for)


# --- The admission policy check --------------------------------------------------------

NAME = "kubernetes.io/metadata.name"


def selector(names):
    return {"matchExpressions": [{"key": NAME, "operator": "In", "values": names}]}


def policy(bounds, names=None):
    names = sorted(bounds) if names is None else names
    return {
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"namespaceSelector": selector(names)},
            "variables": [
                {
                    "name": "minReplicas",
                    "expression": json.dumps({n: b[0] for n, b in bounds.items()}),
                },
                {
                    "name": "maxReplicas",
                    "expression": json.dumps({n: b[1] for n, b in bounds.items()}),
                },
                {"name": "replicas", "expression": "object.spec.replicas"},
            ],
        }
    }


def binding(names, actions=("Deny",), policy_name="kube-handrail-writer-bounds"):
    return {
        "spec": {
            "policyName": policy_name,
            "validationActions": list(actions),
            "matchResources": {"namespaceSelector": selector(names)},
        }
    }


def api(policy_obj, binding_obj, error=None):
    def serve(value):
        def read(name, _preload_content=True):
            assert name == "kube-handrail-writer-bounds" and _preload_content is False
            if error is not None:
                raise error
            if value is None:
                raise ApiException(status=404)
            return SimpleNamespace(data=json.dumps(value).encode())

        return read

    fake = MagicMock()
    fake.read_validating_admission_policy.side_effect = serve(policy_obj)
    fake.read_validating_admission_policy_binding.side_effect = serve(binding_obj)
    return fake


BOUNDS = {"team-a": (1, 3), "team-b": (0, 8)}


def refusal(fake, namespace="team-a", bounds=(1, 3)) -> GuardError:
    with pytest.raises(GuardError) as exc:
        admission.check_admission_policy(fake, namespace, bounds)
    assert exc.value.code == "conflict"
    assert exc.value.details["check"] == "admission_policy_match"
    return exc.value


def test_a_matching_policy_passes_for_each_namespace():
    fake = api(policy(BOUNDS), binding(sorted(BOUNDS)))
    admission.check_admission_policy(fake, "team-a", (1, 3))
    admission.check_admission_policy(fake, "team-b", (0, 8))


def test_the_legacy_single_namespace_selector_is_understood():
    legacy = {"matchLabels": {NAME: "team-a"}}
    p = policy({"team-a": (1, 3)})
    p["spec"]["matchConstraints"]["namespaceSelector"] = legacy
    b = binding(["team-a"])
    b["spec"]["matchResources"]["namespaceSelector"] = legacy
    admission.check_admission_policy(api(p, b), "team-a", (1, 3))


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (api(None, binding(["team-a"])), "policy_missing"),
        (api(policy(BOUNDS), None), "binding_missing"),
        (api(policy(BOUNDS), binding(["team-a"], ("Warn", "Audit"))), "binding_not_deny"),
        (api(policy(BOUNDS), binding(["team-a"], policy_name="other")), "binding_other_policy"),
        (api(policy(BOUNDS, ["team-b"]), binding(["team-a"])), "policy_not_selecting_namespace"),
        (api(policy(BOUNDS), binding(["team-b"])), "binding_not_selecting_namespace"),
        (api(policy({"team-a": (1, 4)}), binding(["team-a"])), "bounds_mismatch"),
        (api(policy({"team-b": (0, 8)}, ["team-a"]), binding(["team-a"])), "bounds_mismatch"),
        (api(policy(BOUNDS), binding(["team-a"]), ApiException(status=403)), "unreadable"),
        (api(policy(BOUNDS), binding(["team-a"]), ConnectionError()), "unreadable"),
    ],
)
def test_anything_short_of_a_matching_deny_policy_is_a_conflict(fake, reason):
    assert refusal(fake).details["reason"] == reason


def test_a_warn_binding_reports_its_actions():
    error = refusal(api(policy(BOUNDS), binding(["team-a"], ("Warn", "Audit"))))
    assert error.details["validation_actions"] == ["Warn", "Audit"]


def test_a_bounds_mismatch_names_both_sides():
    error = refusal(api(policy({"team-a": (1, 4)}), binding(["team-a"])))
    assert error.details["expected"] == {"min_replicas": 1, "max_replicas": 3}
    assert error.details["found"] == {"min_replicas": 1, "max_replicas": 4}


def test_an_unparseable_bounds_variable_is_a_mismatch():
    p = policy(BOUNDS)
    p["spec"]["variables"][0]["expression"] = '{"team-a": 1} + {}'
    assert refusal(api(p, binding(["team-a"]))).details["reason"] == "bounds_mismatch"


def test_the_cluster_message_is_never_returned():
    error = refusal(api(policy(BOUNDS), binding(["team-a"]), ApiException(status=500, reason="x")))
    assert "HTTP 500" in error.message and error.details["status"] == 500


@pytest.mark.parametrize(
    ("value", "selected"),
    [
        (None, True),
        ({}, True),
        ({"matchLabels": {NAME: "team-a"}}, True),
        ({"matchLabels": {NAME: "team-b"}}, False),
        ({"matchLabels": {"team": "a"}}, False),
        (selector(["team-a", "team-b"]), True),
        ({"matchExpressions": [{"key": NAME, "operator": "NotIn", "values": ["x"]}]}, True),
        ({"matchExpressions": [{"key": NAME, "operator": "NotIn", "values": ["team-a"]}]}, False),
        ({"matchExpressions": [{"key": NAME, "operator": "Exists"}]}, True),
        ({"matchExpressions": [{"key": "team", "operator": "Exists"}]}, False),
        ({"matchExpressions": [{"key": NAME, "operator": "DoesNotExist"}]}, False),
        ("garbage", False),
    ],
)
def test_namespace_selectors(value, selected):
    assert admission.selects(value, "team-a") is selected


# --- The rendered manifests and the Lambdas agree --------------------------------------

GOLDEN = Path(__file__).resolve().parents[1] / "k8s_render" / "golden"


@pytest.mark.parametrize("fixture", sorted(p.name for p in GOLDEN.iterdir()))
def test_the_rendered_policy_backs_every_namespace_of_the_rendered_policy_file(fixture):
    """What terraform/modules/namespace-policy renders (tests/k8s_render/golden) passes the
    read Lambda's check for each namespace with the bounds of the packaged policy file, in
    Deny, and is a conflict in Warn."""
    policy = parse_namespace_policy((GOLDEN / fixture / "namespace_policy.json").read_text())
    docs = list(yaml.safe_load_all((GOLDEN / fixture / "admission-policy.yaml").read_text()))
    vap = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
    vapb = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
    allowed = (GOLDEN / fixture / "allowed_namespaces.txt").read_text().strip().split(",")
    assert sorted(policy) == allowed
    for namespace, entry in policy.items():
        bounds = (entry.min_replicas, entry.max_replicas)
        if vapb["spec"]["validationActions"] == ["Deny"]:
            admission.check_admission_policy(api(vap, vapb), namespace, bounds)
        else:
            assert refusal(api(vap, vapb), namespace, bounds).details["reason"] == (
                "binding_not_deny"
            )
        with pytest.raises(GuardError):
            admission.check_admission_policy(
                api(vap, {**vapb, "spec": {**vapb["spec"], "validationActions": ["Deny"]}}),
                namespace,
                (bounds[0], bounds[1] + 1),
            )


def test_the_rendered_rbac_gives_the_writer_only_where_an_action_is_allowed():
    docs = list(yaml.safe_load_all((GOLDEN / "two-namespaces" / "rbac.yaml").read_text()))
    roles = {
        (d["metadata"]["namespace"], d["metadata"]["name"]): d for d in docs if d["kind"] == "Role"
    }
    assert set(roles) == {
        ("kube-handrail-sandbox", "kube-handrail-reader"),
        ("kube-handrail-sandbox", "kube-handrail-writer"),
        ("kube-handrail-sandbox-2", "kube-handrail-reader"),
        ("kube-handrail-sandbox-2", "kube-handrail-writer"),
    }
    # Restart only: no ReplicaSet read for the writer.
    restart_only = roles[("kube-handrail-sandbox", "kube-handrail-writer")]["rules"]
    assert [r["resources"] for r in restart_only] == [["deployments"]]
    every_action = roles[("kube-handrail-sandbox-2", "kube-handrail-writer")]["rules"]
    assert [r["resources"] for r in every_action] == [["deployments"], ["replicasets"]]
