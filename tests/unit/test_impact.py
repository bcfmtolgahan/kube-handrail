from typing import Any

import pytest

from handrail_common.impact import (
    NO_PODS,
    PDB_HEADROOM,
    RECREATE_STRATEGY,
    ROLLOUT_IN_PROGRESS,
    SCALE_TO_ZERO,
    SERVICE_WITHOUT_ENDPOINTS,
    ready_endpoints,
    rollout_impact,
    rollout_strategy,
    scale_impact,
    selector_matches,
    service_selects,
    slices_by_service,
)

LABELS = {"app": "web", "tier": "front"}
UNSET = object()


def expr(key: str, operator: str, values: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "operator": operator, **({"values": values} if values else {})}


def pdb(
    name: str = "web",
    selector: Any = UNSET,
    min_available: Any = 1,
    max_unavailable: Any = None,
    allowed: int = 1,
    status: bool = True,
) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if selector is UNSET:
        spec["selector"] = {"matchLabels": {"app": "web"}}
    elif selector is not None:
        spec["selector"] = selector
    if min_available is not None:
        spec["minAvailable"] = min_available
    if max_unavailable is not None:
        spec["maxUnavailable"] = max_unavailable
    obj: dict[str, Any] = {"metadata": {"name": name}, "spec": spec}
    if status:
        obj["status"] = {
            "currentHealthy": 2,
            "desiredHealthy": 1,
            "disruptionsAllowed": allowed,
            "expectedPods": 2,
        }
    return obj


def service(name: str = "web", selector: dict[str, str] | None = UNSET) -> dict[str, Any]:
    spec = {"selector": {"app": "web"} if selector is UNSET else selector}
    return {"metadata": {"name": name}, "spec": spec}


def endpoint(ready: Any = True, uid: str | None = None, address: str = "10.0.0.1") -> dict:
    obj: dict[str, Any] = {"addresses": [address]}
    if ready is not UNSET:
        obj["conditions"] = {} if ready is None else {"ready": ready}
    if uid:
        obj["targetRef"] = {"kind": "Pod", "uid": uid}
    return obj


def endpoint_slice(service_name: str | None, endpoints: list[dict] | None) -> dict[str, Any]:
    labels = {"kubernetes.io/service-name": service_name} if service_name else {}
    return {"addressType": "IPv4", "metadata": {"labels": labels}, "endpoints": endpoints}


def impact(
    replicas_from: int,
    replicas_to: int,
    pdbs: list[dict] = (),
    services: list[dict] = (),
    slices: list[dict] = (),
    ready_now: int = 2,
    labels: dict[str, str] = LABELS,
) -> dict[str, Any]:
    return scale_impact(labels, ready_now, replicas_from, replicas_to, pdbs, services, slices)


def codes(result: dict) -> list[str]:
    return [w["code"] for w in result["warnings"]]


# --- Label selectors ---------------------------------------------------------


def test_null_selector_matches_nothing():
    assert selector_matches(None, LABELS) is False
    assert selector_matches(None, {}) is False


def test_empty_selector_matches_everything():
    assert selector_matches({}, LABELS) is True
    assert selector_matches({"matchLabels": {}, "matchExpressions": []}, {}) is True
    assert selector_matches({"matchLabels": None, "matchExpressions": None}, LABELS) is True


@pytest.mark.parametrize(
    ("match_labels", "matches"),
    [
        ({"app": "web"}, True),
        ({"app": "web", "tier": "front"}, True),
        ({"app": "api"}, False),
        ({"app": "web", "zone": "a"}, False),
    ],
)
def test_match_labels(match_labels, matches):
    assert selector_matches({"matchLabels": match_labels}, LABELS) is matches


@pytest.mark.parametrize(
    ("expression", "matches"),
    [
        (expr("app", "In", ["web", "api"]), True),
        (expr("app", "In", ["api"]), False),
        (expr("zone", "In", ["a"]), False),
        (expr("app", "NotIn", ["api"]), True),
        (expr("app", "NotIn", ["web"]), False),
        (expr("zone", "NotIn", ["a"]), True),
        (expr("app", "Exists"), True),
        (expr("zone", "Exists"), False),
        (expr("zone", "DoesNotExist"), True),
        (expr("app", "DoesNotExist"), False),
        (expr("app", "In"), False),
    ],
)
def test_match_expressions(expression, matches):
    assert selector_matches({"matchExpressions": [expression]}, LABELS) is matches


def test_labels_and_expressions_must_all_match():
    selector = {
        "matchLabels": {"app": "web"},
        "matchExpressions": [expr("tier", "In", ["front"]), expr("zone", "Exists")],
    }
    assert selector_matches(selector, LABELS) is False
    assert selector_matches(selector, LABELS | {"zone": "a"}) is True


def test_unknown_operator_is_refused():
    with pytest.raises(ValueError):
        selector_matches({"matchExpressions": [expr("app", "Gt", ["1"])]}, LABELS)


@pytest.mark.parametrize(
    ("selector", "selects"),
    [
        ({"app": "web"}, True),
        ({"app": "web", "tier": "front"}, True),
        ({"app": "web", "zone": "a"}, False),
        ({"app": "api"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_service_selector_is_a_subset_of_the_pod_labels(selector, selects):
    assert service_selects(service(selector=selector), LABELS) is selects


def test_service_without_spec_is_skipped():
    assert service_selects({"metadata": {"name": "x"}}, LABELS) is False


# --- Endpoints ---------------------------------------------------------------


def test_ready_endpoints_counts_only_ready_true():
    slices = [
        endpoint_slice(
            "web",
            [
                endpoint(True, address="10.0.0.1"),
                endpoint(False, address="10.0.0.2"),
                endpoint(None, address="10.0.0.3"),  # conditions without ready
                endpoint(UNSET, address="10.0.0.4"),  # no conditions at all
                endpoint(True, address="10.0.0.5"),
            ],
        )
    ]
    assert ready_endpoints(slices) == 2


def test_ready_endpoints_counts_a_pod_once_across_slices():
    slices = [
        endpoint_slice(
            "web",
            [endpoint(uid="pod-a", address="10.0.0.1"), endpoint(uid="pod-b", address="10.0.0.2")],
        ),
        endpoint_slice("web", [endpoint(uid="pod-a", address="fd00::1")]),
    ]
    assert ready_endpoints(slices) == 2


def test_ready_endpoints_of_empty_slices():
    assert ready_endpoints([]) == 0
    # A Service with no pods has a placeholder slice with `endpoints: null`.
    assert ready_endpoints([endpoint_slice("web", None), endpoint_slice("web", [])]) == 0


def test_slices_are_grouped_by_service_name_label():
    a, b, c, unlabelled = (
        endpoint_slice("web", []),
        endpoint_slice("web", []),
        endpoint_slice("api", []),
        endpoint_slice(None, []),
    )
    assert slices_by_service([a, b, c, unlabelled, {"endpoints": []}]) == {
        "web": [a, b],
        "api": [c],
    }


# --- Impact ------------------------------------------------------------------


def test_impact_of_a_scale_up():
    result = impact(
        2,
        3,
        [pdb(), pdb("other", selector={"matchLabels": {"app": "api"}})],
        [service(), service("api", {"app": "api"})],
        [endpoint_slice("web", [endpoint(address="10.0.0.1"), endpoint(address="10.0.0.2")])],
    )
    assert result == {
        "pods": {"ready_now": 2, "from": 2, "to": 3, "added": 1, "removed": 0},
        "pod_disruption_budgets": [
            {
                "name": "web",
                "min_available": 1,
                "max_unavailable": None,
                "current_healthy": 2,
                "desired_healthy": 1,
                "disruptions_allowed": 1,
            }
        ],
        "services": [{"name": "web", "ready_endpoints_now": 2, "projected_ready_endpoints": 3}],
        "warnings": [],
        "truncated": False,
    }


def test_impact_without_pdbs_or_services():
    result = impact(1, 2, ready_now=0)
    assert result["pods"] == {"ready_now": 0, "from": 1, "to": 2, "added": 1, "removed": 0}
    assert (result["pod_disruption_budgets"], result["services"], result["warnings"]) == (
        [],
        [],
        [],
    )


def test_impact_reports_pdb_fields_as_given():
    result = impact(2, 2, [pdb(min_available=None, max_unavailable="25%", status=False)])
    assert result["pod_disruption_budgets"] == [
        {
            "name": "web",
            "min_available": None,
            "max_unavailable": "25%",
            "current_healthy": None,
            "desired_healthy": None,
            "disruptions_allowed": None,
        }
    ]


def test_impact_includes_pdbs_with_an_empty_selector_and_skips_null_ones():
    result = impact(2, 3, [pdb("all", selector={}), pdb("null", selector=None)])
    assert [p["name"] for p in result["pod_disruption_budgets"]] == ["all"]


def test_impact_with_no_pod_template_labels():
    result = impact(
        2, 3, [pdb("all", selector={}), pdb()], [service(), service("any", {})], labels={}
    )
    assert [p["name"] for p in result["pod_disruption_budgets"]] == ["all"]
    assert result["services"] == []


def test_pdb_headroom_warns_when_more_pods_are_removed_than_allowed():
    result = impact(3, 1, [pdb(allowed=1), pdb("strict", allowed=2)])
    assert codes(result) == [PDB_HEADROOM]
    assert "web" in result["warnings"][0]["message"]
    assert "not an eviction" in result["warnings"][0]["message"]


def test_pdb_headroom_at_the_limit_does_not_warn():
    assert codes(impact(3, 2, [pdb(allowed=1)])) == []


def test_pdb_headroom_treats_unknown_disruptions_allowed_as_zero():
    assert codes(impact(2, 1, [pdb(status=False)])) == [PDB_HEADROOM]


def test_pdb_headroom_never_warns_on_a_scale_up():
    assert codes(impact(1, 3, [pdb(allowed=0)])) == []


def test_projected_endpoints_keep_other_backends():
    # 3 ready endpoints, 2 of them from this Deployment: 1 other backend stays.
    slices = [endpoint_slice("web", [endpoint(address=f"10.0.0.{i}") for i in range(3)])]
    result = impact(2, 0, services=[service()], slices=slices)
    assert result["services"] == [
        {"name": "web", "ready_endpoints_now": 3, "projected_ready_endpoints": 1}
    ]
    assert codes(result) == [SCALE_TO_ZERO]


def test_projected_endpoints_do_not_count_more_pods_than_are_ready():
    # Fewer ready endpoints than ready pods (slices lag behind): no other backend is assumed.
    slices = [endpoint_slice("web", [endpoint()])]
    result = impact(2, 1, services=[service()], slices=slices)
    assert result["services"][0]["projected_ready_endpoints"] == 1


def test_scale_to_zero_warns_for_pods_pdbs_and_services():
    slices = [endpoint_slice("web", [endpoint(address="10.0.0.1"), endpoint(address="10.0.0.2")])]
    result = impact(2, 0, [pdb(allowed=1)], [service()], slices)
    assert codes(result) == [SCALE_TO_ZERO, PDB_HEADROOM, SERVICE_WITHOUT_ENDPOINTS]
    assert result["pods"]["removed"] == 2
    assert result["services"][0]["projected_ready_endpoints"] == 0


def test_service_without_endpoints_when_it_has_none_now_or_after():
    result = impact(0, 0, services=[service()], slices=[endpoint_slice("web", None)], ready_now=0)
    assert codes(result) == [SCALE_TO_ZERO, SERVICE_WITHOUT_ENDPOINTS]


def test_services_count_only_their_own_slices():
    slices = [
        endpoint_slice("api", [endpoint(address="10.0.1.1")]),
        endpoint_slice(None, [endpoint(address="10.0.2.1")]),
    ]
    result = impact(0, 1, services=[service()], slices=slices, ready_now=0)
    assert result["services"] == [
        {"name": "web", "ready_endpoints_now": 0, "projected_ready_endpoints": 1}
    ]
    assert codes(result) == []


def test_services_and_pdbs_are_sorted_by_name():
    result = impact(2, 3, [pdb("b"), pdb("a")], [service("z"), service("m")])
    assert [p["name"] for p in result["pod_disruption_budgets"]] == ["a", "b"]
    assert [s["name"] for s in result["services"]] == ["m", "z"]


def test_long_lists_are_cut_after_the_warnings_are_computed():
    services = [service(f"svc-{i:02d}") for i in range(25)]
    result = impact(1, 0, [], services, [])
    assert len(result["services"]) == 20
    assert result["services"][0]["name"] == "svc-00"
    # scale_to_zero plus one service_without_endpoints per Service, cut to 20.
    assert len(result["warnings"]) == 20
    assert result["truncated"] is True


# --- Rollout (restart) -------------------------------------------------------


def deployment(
    replicas: int = 2,
    strategy: dict | None = None,
    generation: int = 3,
    observed: int = 3,
    updated: int | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"replicas": replicas}
    if strategy is not None:
        spec["strategy"] = strategy
    return {
        "metadata": {"generation": generation},
        "spec": spec,
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": replicas if updated is None else updated,
        },
    }


def rolling(max_unavailable: Any = UNSET, max_surge: Any = UNSET) -> dict[str, Any]:
    params = {}
    if max_unavailable is not UNSET:
        params["maxUnavailable"] = max_unavailable
    if max_surge is not UNSET:
        params["maxSurge"] = max_surge
    return {"type": "RollingUpdate", "rollingUpdate": params}


def rollout(
    dep: dict[str, Any],
    pdbs: list[dict] = (),
    services: list[dict] = (),
    slices: list[dict] = (),
    ready_now: int = 2,
) -> dict[str, Any]:
    replicas = dep["spec"]["replicas"]
    return rollout_impact(dep, LABELS, ready_now, replicas, pdbs, services, slices)


@pytest.mark.parametrize(
    ("replicas", "strategy", "unavailable", "surge"),
    [
        # The API server's defaults, 25 % each: unavailable rounds down, surge rounds up.
        (4, None, 1, 1),
        (3, None, 0, 1),
        (1, None, 0, 1),
        (10, rolling("33%", "33%"), 3, 4),
        (5, rolling(2, 0), 2, 0),
        # Both 0 would never progress, so the controller lets one pod be unavailable.
        (3, rolling(0, 0), 1, 0),
        (3, rolling("0%", "0%"), 1, 0),
        (0, None, 1, 0),
    ],
)
def test_rollout_strategy_resolves_counts_like_the_controller(
    replicas, strategy, unavailable, surge
):
    assert rollout_strategy(deployment(replicas, strategy), replicas) == {
        "strategy": "RollingUpdate",
        "max_unavailable": unavailable,
        "max_surge": surge,
    }


def test_rollout_strategy_recreate_has_no_counts():
    assert rollout_strategy(deployment(strategy={"type": "Recreate"}), 2) == {
        "strategy": "Recreate",
        "max_unavailable": None,
        "max_surge": None,
    }


@pytest.mark.parametrize("value", ["25", "a%", True, None, 1.5])
def test_rollout_strategy_refuses_values_it_cannot_resolve(value):
    with pytest.raises(ValueError):
        rollout_strategy(deployment(strategy=rolling(max_unavailable=value)), 2)


def test_rollout_replaces_every_pod_and_keeps_the_count():
    result = rollout(deployment(2, rolling(0, 1)))
    assert result["pods"] == {
        "ready_now": 2,
        "from": 2,
        "to": 2,
        "added": 0,
        "removed": 0,
        "replaced": 2,
    }
    assert result["rollout"] == {"strategy": "RollingUpdate", "max_unavailable": 0, "max_surge": 1}
    assert result["warnings"] == [] and result["truncated"] is False


def test_rollout_projects_the_lowest_ready_endpoints():
    slices = [endpoint_slice("web", [endpoint(address=f"10.0.0.{i}") for i in range(4)])]
    # Three pods of this Deployment are ready, one other backend; one pod can be down.
    result = rollout(deployment(3, rolling(1, 0)), services=[service()], slices=slices, ready_now=3)
    assert result["services"] == [
        {"name": "web", "ready_endpoints_now": 4, "projected_min_ready_endpoints": 3}
    ]


def test_rollout_of_a_degraded_deployment_projects_no_more_than_are_ready_now():
    # One replica, not ready: the Service has no ready endpoint before the rollout starts.
    result = rollout(deployment(1), services=[service()], slices=[], ready_now=0)
    assert result["services"] == [
        {"name": "web", "ready_endpoints_now": 0, "projected_min_ready_endpoints": 0}
    ]
    assert codes(result) == [SERVICE_WITHOUT_ENDPOINTS]
    # Four replicas, two ready, one other backend; one pod can be down.
    slices = [endpoint_slice("web", [endpoint(address=f"10.0.0.{i}") for i in range(3)])]
    result = rollout(deployment(4, rolling(1, 0)), services=[service()], slices=slices)
    assert result["services"][0]["projected_min_ready_endpoints"] == 3


def test_rollout_pdb_headroom_counts_the_pods_down_at_once():
    assert codes(rollout(deployment(4, rolling(1, 1)), pdbs=[pdb(allowed=1)])) == []
    result = rollout(deployment(4, rolling(2, 0)), pdbs=[pdb(allowed=1)])
    assert codes(result) == [PDB_HEADROOM]
    assert "2 pod(s) down at once" in result["warnings"][0]["message"]
    # Never more than the pods there are.
    assert codes(rollout(deployment(1, rolling(5, 0)), pdbs=[pdb(allowed=1)])) == []


def test_rollout_recreate_warns_and_empties_the_service():
    slices = [endpoint_slice("web", [endpoint(address=f"10.0.0.{i}") for i in range(2)])]
    result = rollout(
        deployment(2, {"type": "Recreate"}),
        pdbs=[pdb(allowed=1)],
        services=[service()],
        slices=slices,
    )
    assert codes(result) == [RECREATE_STRATEGY, PDB_HEADROOM, SERVICE_WITHOUT_ENDPOINTS]
    assert result["services"][0]["projected_min_ready_endpoints"] == 0


def test_rollout_keeps_the_action_warnings_when_the_list_is_cut():
    dep = deployment(1, {"type": "Recreate"})
    services = [service(f"svc-{i:02d}") for i in range(20)]
    action = [
        {"code": "mutable_image_tag", "message": "m"},
        {"code": "config_not_rolled_back", "message": "c"},
    ]
    result = rollout_impact(dep, LABELS, 1, 1, [], services, [], action)
    assert result["truncated"] is True
    assert codes(result)[:3] == ["mutable_image_tag", "config_not_rolled_back", RECREATE_STRATEGY]


@pytest.mark.parametrize(
    ("dep", "in_progress"),
    [
        (deployment(2), False),
        (deployment(2, generation=4, observed=3), True),
        (deployment(2, updated=1), True),
    ],
)
def test_rollout_in_progress_is_reported(dep, in_progress):
    assert (ROLLOUT_IN_PROGRESS in codes(rollout(dep))) is in_progress


def test_rollout_of_no_pods_warns():
    result = rollout(deployment(0), ready_now=0)
    assert codes(result) == [NO_PODS]
    assert result["pods"]["replaced"] == 0
