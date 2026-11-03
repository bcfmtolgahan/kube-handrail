import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from handrail_common.breaker import (
    APPLY_GRACE_SECONDS,
    BUDGET_WINDOW_SECONDS,
    LAST_PLAN_ANNOTATION,
    check_cooldown,
    check_flapping,
    check_history,
    check_manual_change,
    check_object_budget,
    check_plan_rate,
    last_applied_plan,
    last_plan_id,
    retention_seconds,
    with_last_applied,
)
from handrail_common.config import load_settings
from handrail_common.plans import SCALE_DEPLOYMENT, HistoryEntry, Plan
from handrail_common.validation import GuardError

NOW = 1_700_000_000
WINDOW = 600
UID = "8f7c1a2e-0000-4000-8000-000000000001"
PLAN_ID = "3b9f7c1e-0000-4000-8000-0000000000aa"


def applied(replicas_from: int, replicas_to: int, applied_at: int, n: int = 0) -> HistoryEntry:
    return HistoryEntry(
        plan_id=f"p{applied_at}-{n}",
        replicas_from=replicas_from,
        replicas_to=replicas_to,
        created_at=applied_at - 5,
        result="applied",
        applied_at=applied_at,
    )


def planned(created_at: int, result: str | None = None, n: int = 0) -> HistoryEntry:
    return HistoryEntry(
        plan_id=f"q{created_at}-{n}",
        replicas_from=1,
        replicas_to=2,
        created_at=created_at,
        result=result,
        applied_at=None,
    )


def code_of(fn, *args) -> str | None:
    try:
        fn(*args)
    except GuardError as exc:
        return exc.code
    return None


def only_ints_and_strings(value) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and only_ints_and_strings(v) for k, v in value.items())
    if isinstance(value, list):
        return all(only_ints_and_strings(v) for v in value)
    return isinstance(value, (int, str)) and not isinstance(value, bool)


# --- Flapping ---------------------------------------------------------------


def test_single_revert_is_allowed():
    # Up 1 -> 2, then back down: one reversal.
    assert code_of(check_flapping, [applied(1, 2, NOW - 60)], NOW, WINDOW, 2, 1) is None


def test_second_reversal_is_flapping():
    history = [applied(1, 2, NOW - 120), applied(2, 1, NOW - 60)]
    with pytest.raises(GuardError) as exc:
        check_flapping(history, NOW, WINDOW, 1, 2)
    assert exc.value.code == "flapping"
    assert exc.value.details == {
        "window_seconds": WINDOW,
        "count": 2,
        "requested": {"from": 1, "to": 2},
        "history": [
            {"applied_at": NOW - 120, "from": 1, "to": 2},
            {"applied_at": NOW - 60, "from": 2, "to": 1},
        ],
    }
    assert only_ints_and_strings(exc.value.details)
    json.dumps(exc.value.details)


def test_same_direction_is_not_flapping():
    history = [applied(1, 2, NOW - 120), applied(2, 1, NOW - 60)]
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 0) is None


def test_continuing_after_one_reversal_is_not_flapping():
    # Up, up, then down: only one reversal.
    history = [applied(1, 2, NOW - 120), applied(2, 3, NOW - 60)]
    assert code_of(check_flapping, history, NOW, WINDOW, 3, 1) is None


def test_reversal_after_two_ups_and_a_down_is_flapping():
    history = [applied(1, 2, NOW - 180), applied(2, 3, NOW - 120), applied(3, 1, NOW - 60)]
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 2) == "flapping"


@pytest.mark.parametrize(
    ("age", "code"),
    [
        (WINDOW - 1, "flapping"),  # still inside the window
        (WINDOW, None),  # exactly WINDOW seconds old: out of the window
    ],
)
def test_flapping_window_edge(age, code):
    # The oldest of the two earlier applies decides whether there are two reversals.
    history = [applied(1, 2, NOW - age), applied(2, 1, NOW - 10)]
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 2) == code


def test_unchanged_replica_count_has_no_direction():
    history = [applied(1, 2, NOW - 120), applied(2, 1, NOW - 60)]
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 1) is None
    # Earlier no-op applies are skipped when finding the last two directions.
    history = [applied(1, 2, NOW - 180), applied(2, 2, NOW - 150), applied(2, 1, NOW - 60)]
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 2) == "flapping"


def test_plans_that_were_not_applied_do_not_count():
    history = [
        applied(1, 2, NOW - 120),
        HistoryEntry("x", 2, 1, NOW - 70, "conflict", NOW - 60),
        planned(NOW - 50),
    ]
    assert code_of(check_flapping, history, NOW, WINDOW, 2, 1) is None
    assert code_of(check_flapping, history, NOW, WINDOW, 1, 2) is None


def test_history_order_is_by_applied_at():
    # A plan created earlier can be applied later.
    late = HistoryEntry("b", 2, 1, NOW - 200, "applied", NOW - 60)
    early = HistoryEntry("a", 1, 2, NOW - 100, "applied", NOW - 90)
    assert code_of(check_flapping, [late, early], NOW, WINDOW, 1, 2) == "flapping"


# --- Hourly object budget ---------------------------------------------------


def test_budget_below_limit_passes():
    history = [applied(1, 2, NOW - 100 * i, i) for i in range(1, 6)]
    assert code_of(check_object_budget, history, NOW, 6) is None


def test_budget_at_limit_is_rejected():
    history = [applied(1, 2, NOW - 100 * i, i) for i in range(1, 7)]
    with pytest.raises(GuardError) as exc:
        check_object_budget(history, NOW, 6)
    assert exc.value.code == "object_budget_exceeded"
    details = exc.value.details
    assert (details["window_seconds"], details["limit"], details["count"]) == (3600, 6, 6)
    assert [h["applied_at"] for h in details["history"]] == [NOW - 100 * i for i in range(6, 0, -1)]
    assert only_ints_and_strings(details)


@pytest.mark.parametrize(
    ("age", "code"),
    [(BUDGET_WINDOW_SECONDS - 1, "object_budget_exceeded"), (BUDGET_WINDOW_SECONDS, None)],
)
def test_budget_window_edge(age, code):
    history = [applied(1, 2, NOW - age)] + [applied(1, 2, NOW - 10 * i, i) for i in range(1, 6)]
    assert code_of(check_object_budget, history, NOW, 6) == code


def test_budget_counts_only_applied():
    history = [planned(NOW - 10 * i, n=i) for i in range(10)]
    history += [HistoryEntry(f"f{i}", 1, 2, NOW - 50, "conflict", NOW - 40) for i in range(3)]
    assert code_of(check_object_budget, history, NOW, 1) is None


# --- Plan rate limit --------------------------------------------------------


def test_plan_rate_below_limit_passes():
    history = [planned(NOW - i, n=i) for i in range(19)]
    assert code_of(check_plan_rate, history, NOW, WINDOW, 20) is None


def test_plan_rate_at_limit_is_rejected():
    history = [planned(NOW - i, n=i) for i in range(19)] + [applied(1, 2, NOW - 30)]
    with pytest.raises(GuardError) as exc:
        check_plan_rate(history, NOW, WINDOW, 20)
    assert exc.value.code == "plan_rate_limited"
    details = exc.value.details
    assert (details["window_seconds"], details["limit"], details["count"]) == (WINDOW, 20, 20)
    assert details["history"] == [{"applied_at": NOW - 30, "from": 1, "to": 2}]
    assert only_ints_and_strings(details)


@pytest.mark.parametrize(("age", "code"), [(WINDOW - 1, "plan_rate_limited"), (WINDOW, None)])
def test_plan_rate_window_edge(age, code):
    history = [planned(NOW - age)] + [planned(NOW - i, n=i) for i in range(19)]
    assert code_of(check_plan_rate, history, NOW, WINDOW, 20) == code


def test_plan_rate_counts_every_plan_whatever_its_result():
    history = [planned(NOW - i, result="conflict", n=i) for i in range(3)]
    assert code_of(check_plan_rate, history, NOW, WINDOW, 3) == "plan_rate_limited"


# --- Manual change ----------------------------------------------------------


def last_plan(replicas_to: int = 2, uid: str = UID) -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=replicas_to,
        args_hash="h",
        resource_version="1",
        generation=2,
        uid=uid,
        created_at=NOW - 100,
        expires_at=NOW + 200,
    )


def test_manual_change_is_rejected():
    with pytest.raises(GuardError) as exc:
        check_manual_change(last_plan(replicas_to=2), 3, False)
    assert exc.value.code == "manual_change"
    assert exc.value.details == {"last_applied_to": 2, "current": 3}


def test_manual_change_with_override_is_allowed():
    assert check_manual_change(last_plan(replicas_to=2), 3, True) is True


@pytest.mark.parametrize(
    ("plan", "current"),
    [
        (None, 3),  # annotation absent, or plan no longer in the table, or never consumed
        (last_plan(replicas_to=3), 3),  # nothing changed since the last apply
    ],
)
def test_no_manual_change(plan, current):
    assert check_manual_change(plan, current, False) is False


class FakePlanLookup:
    def __init__(self, plan: Plan | None):
        self.plan = plan
        self.calls: list[str] = []

    def consumed_plan(self, plan_id: str) -> Plan | None:
        self.calls.append(plan_id)
        return self.plan


def annotated(plan_id: str | None, uid: str = UID) -> SimpleNamespace:
    annotations = {LAST_PLAN_ANNOTATION: plan_id} if plan_id else {}
    return SimpleNamespace(metadata=SimpleNamespace(annotations=annotations, uid=uid))


def test_last_applied_plan_is_the_annotated_plan_of_the_same_object():
    store = FakePlanLookup(last_plan())
    assert last_applied_plan(store, annotated(PLAN_ID)) == last_plan()
    assert store.calls == [PLAN_ID]


def test_last_applied_plan_is_none_without_an_annotation():
    store = FakePlanLookup(last_plan())
    assert last_applied_plan(store, annotated(None)) is None
    assert store.calls == []


def test_last_applied_plan_is_none_when_the_plan_is_gone():
    assert last_applied_plan(FakePlanLookup(None), annotated(PLAN_ID)) is None


def test_last_applied_plan_ignores_a_plan_for_a_recreated_object():
    # Same name, other uid: the annotation was copied or the object was recreated.
    other = "8f7c1a2e-0000-4000-8000-000000000002"
    assert last_applied_plan(FakePlanLookup(last_plan(uid=other)), annotated(PLAN_ID)) is None


def test_last_applied_plan_fails_closed_on_a_read_error():
    class Broken:
        def consumed_plan(self, plan_id):
            raise RuntimeError("throttled")

    with pytest.raises(RuntimeError):
        last_applied_plan(Broken(), annotated(PLAN_ID))


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (None, None),
        ({}, None),
        ({LAST_PLAN_ANNOTATION: "not-a-uuid"}, None),
        ({LAST_PLAN_ANNOTATION: PLAN_ID.upper()}, None),
        ({LAST_PLAN_ANNOTATION: PLAN_ID}, PLAN_ID),
    ],
)
def test_last_plan_id(annotations, expected):
    deployment = SimpleNamespace(metadata=SimpleNamespace(annotations=annotations))
    assert last_plan_id(deployment) == expected


# --- History lookup ---------------------------------------------------------

SETTINGS = load_settings(
    {
        "CLUSTER_NAME": "c",
        "PLANS_TABLE": "t",
        "ALLOWED_NAMESPACES": "sandbox",
        "MIN_REPLICAS": "0",
        "MAX_REPLICAS": "5",
    }
)


def test_retention_covers_the_longest_window_plus_plan_lifetime():
    assert retention_seconds(SETTINGS) == 3600 + 300 + APPLY_GRACE_SECONDS


class FakeStore:
    def __init__(self, entries=None, error: Exception | None = None):
        self.entries = entries or []
        self.error = error
        self.calls = []

    def history(self, key, since):
        self.calls.append((key, since))
        if self.error:
            raise self.error
        return self.entries


def test_check_history_reads_far_enough_back():
    store = FakeStore()
    check_history(store, "sandbox/web/u", NOW, SETTINGS, 1, 2, plan_rate=True)
    assert store.calls == [("sandbox/web/u", NOW - retention_seconds(SETTINGS))]


def test_check_history_fails_closed_on_query_error():
    store = FakeStore(error=RuntimeError("throttled"))
    with pytest.raises(RuntimeError):
        check_history(store, "k", NOW, SETTINGS, 1, 2, plan_rate=False)


def test_check_history_rate_limit_only_when_asked():
    store = FakeStore([planned(NOW - i, n=i) for i in range(20)])
    check_history(store, "k", NOW, SETTINGS, 1, 2, plan_rate=False)
    with pytest.raises(GuardError) as exc:
        check_history(store, "k", NOW, SETTINGS, 1, 2, plan_rate=True)
    assert exc.value.code == "plan_rate_limited"


# --- The last applied plan is merged into the history ------------------------
#
# The index is eventually consistent and the apply result is recorded best-effort, so the
# most recent apply can be missing from the history or marked unapplied. The Deployment's
# annotation names it and is read from the API server, so it is counted from there.

KEY = f"sandbox/web/{UID}"


def last_applied(
    replicas_from: int, replicas_to: int, applied_at: int | None, result: str | None
) -> Plan:
    return Plan(
        plan_id=PLAN_ID,
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=replicas_from,
        replicas_to=replicas_to,
        args_hash="h",
        resource_version="1",
        generation=2,
        uid=UID,
        created_at=NOW - 100,
        expires_at=NOW + 200,
        used=True,
        result=result,
        applied_at=applied_at,
    )


def test_with_last_applied_adds_an_apply_the_index_has_not_got_yet():
    entries = [applied(1, 2, NOW - 120)]
    merged = with_last_applied(entries, last_applied(2, 1, NOW - 60, "applied"), NOW)
    assert [(e.plan_id, e.applied, e.applied_at) for e in merged] == [
        (entries[0].plan_id, True, NOW - 120),
        (PLAN_ID, True, NOW - 60),
    ]


def test_with_last_applied_replaces_an_entry_whose_result_was_not_recorded():
    # The patch succeeded but record_result failed: the index shows the plan as unapplied.
    unrecorded = HistoryEntry(PLAN_ID, 2, 1, NOW - 100, None, None)
    merged = with_last_applied([unrecorded], last_applied(2, 1, None, None), NOW)
    assert len(merged) == 1 and merged[0].applied


@pytest.mark.parametrize(
    ("now", "expected"),
    [(NOW, NOW), (NOW + 500, NOW + 200)],  # not expired yet: now; expired: expires_at
)
def test_with_last_applied_uses_the_latest_possible_time_when_none_was_recorded(now, expected):
    merged = with_last_applied([], last_applied(2, 1, None, None), now)
    assert merged[0].applied_at == expected


def test_with_last_applied_keeps_a_recorded_time_whatever_the_result():
    # A patch that succeeded after its response was lost was recorded as internal.
    merged = with_last_applied([], last_applied(2, 1, NOW - 60, "internal"), NOW)
    assert merged[0].applied and merged[0].applied_at == NOW - 60


def test_check_history_sees_flapping_when_the_index_missed_the_last_apply():
    store = FakeStore([applied(1, 2, NOW - 120)])
    last = last_applied(2, 1, NOW - 60, "applied")
    check_history(store, KEY, NOW, SETTINGS, 1, 2, plan_rate=False)
    with pytest.raises(GuardError) as exc:
        check_history(store, KEY, NOW, SETTINGS, 1, 2, plan_rate=False, last_applied=last)
    assert exc.value.code == "flapping"
    assert [h["to"] for h in exc.value.details["history"]] == [2, 1]


def test_check_history_counts_an_unrecorded_apply_toward_the_budget():
    store = FakeStore([applied(1, 2, NOW - 100 * i, i) for i in range(1, 6)])
    # The unrecorded apply counts as just applied, which the cooldown alone would refuse.
    settings = replace(SETTINGS, apply_cooldown_seconds=0)
    check_history(store, KEY, NOW, settings, 1, 2, plan_rate=False)
    last = last_applied(1, 2, None, None)
    with pytest.raises(GuardError) as exc:
        check_history(store, KEY, NOW, settings, 1, 2, plan_rate=False, last_applied=last)
    assert exc.value.code == "object_budget_exceeded"
    assert exc.value.details["count"] == 6


def test_check_history_ignores_a_last_plan_for_another_object():
    store = FakeStore([applied(1, 2, NOW - 120)])
    other_key = "sandbox/web/8f7c1a2e-0000-4000-8000-000000000002"
    last = last_applied(2, 1, NOW - 60, "applied")
    check_history(store, other_key, NOW, SETTINGS, 1, 2, plan_rate=False, last_applied=last)


# --- Cooldown ---------------------------------------------------------------


def test_cooldown_refuses_a_change_soon_after_the_last_apply():
    entries = [applied(1, 2, NOW - 3000), applied(2, 3, NOW - 59)]
    with pytest.raises(GuardError) as exc:
        check_cooldown(entries, NOW, 60)
    assert exc.value.code == "cooldown"
    assert exc.value.details["last_applied_at"] == NOW - 59
    assert exc.value.details["retry_after"] == NOW + 1
    assert [h["to"] for h in exc.value.details["history"]] == [3]


def test_cooldown_window_excludes_its_start_and_ignores_unapplied_plans():
    check_cooldown([applied(1, 2, NOW - 60), planned(NOW - 1)], NOW, 60)


def test_cooldown_of_zero_is_off():
    check_cooldown([applied(1, 2, NOW)], NOW, 0)


def test_check_history_runs_the_cooldown_first():
    store = FakeStore([applied(1, 2, NOW - 200), applied(2, 1, NOW - 10)])
    with pytest.raises(GuardError) as exc:
        check_history(store, KEY, NOW, SETTINGS, 1, 2, plan_rate=False)
    assert exc.value.code == "cooldown"


def test_retention_covers_a_long_cooldown():
    settings = replace(SETTINGS, apply_cooldown_seconds=7200)
    assert retention_seconds(settings) == 7200 + 300 + APPLY_GRACE_SECONDS
