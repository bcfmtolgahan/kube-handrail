from typing import Any

import pytest

from handrail_common.impact import (
    PDB_HEADROOM,
    SCALE_TO_ZERO,
    SERVICE_WITHOUT_ENDPOINTS,
    ready_endpoints,
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
