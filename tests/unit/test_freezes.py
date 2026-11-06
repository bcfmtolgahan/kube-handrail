import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from handrail_common.config import load_settings
from handrail_common.freezes import (
    Freeze,
    FreezeConfigError,
    FreezeStore,
    active_freeze,
    check_not_frozen,
    dump_freezes,
    format_time,
    freeze_store,
    parse_freezes,
    parse_time,
)
from handrail_common.validation import GuardError

REGION = "us-east-1"
PARAM = "/handrail/freezes"
START = "2026-12-23T00:00:00Z"
UNTIL = "2027-01-02T00:00:00Z"
T0 = parse_time(START, "start")
T1 = parse_time(UNTIL, "until")
SCALE, RESTART, ROLLBACK = "scale_deployment", "restart_deployment", "rollback_deployment"


def entry(**overrides) -> dict:
    return {"namespaces": ["sandbox"], "start": START, "until": UNTIL, "reason": "year end"} | (
        overrides
    )


def freezes(*entries: dict) -> list[Freeze]:
    return parse_freezes(json.dumps(list(entries)))


# --- validation ---------------------------------------------------------------


def test_an_empty_list_is_valid():
    assert parse_freezes("[]") == []


def test_an_entry_parses_to_epoch_seconds():
    [freeze] = freezes(entry())
    assert (freeze.start, freeze.until) == (T0, T1)
    assert freeze.namespaces == ("sandbox",) and freeze.actions is None
    assert format_time(T0) == START


def test_actions_name_plan_actions():
    [freeze] = freezes(entry(actions=["scale", "rollback"]))
    assert freeze.actions == {SCALE, ROLLBACK}


def test_dump_round_trips():
    original = freezes(entry(actions=["restart"]), entry(namespaces=["*"]))
    assert parse_freezes(dump_freezes(original)) == original


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "{}", '"x"', "null", json.dumps([entry()] * 51)],
)
def test_the_value_must_be_a_list_of_at_most_50(raw):
    with pytest.raises(FreezeConfigError):
        parse_freezes(raw)


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        entry(until=START),
        entry(until="2026-12-22T00:00:00Z"),
        entry(start="2026-12-23T00:00:00+01:00"),
        entry(start="2026-12-23T00:00:00"),
        entry(start="2026-12-23"),
        entry(start="2026-02-30T00:00:00Z"),
        entry(start=1700000000),
        entry(namespaces=[]),
        entry(namespaces="sandbox"),
        entry(namespaces=["Sandbox"]),
        entry(namespaces=["sandbox", "sandbox"]),
        entry(namespaces=["a" * 64]),
        entry(namespaces=["sandbox\n"]),
        entry(namespaces=["sandbox\n", "other"]),
        entry(namespaces=["\u0661\u0662"]),
        entry(start="2026-12-23T00:00:00Z\n"),
        entry(reason=""),
        entry(reason="   "),
        entry(reason="x" * 201),
        entry(reason="line\nbreak"),
        entry(reason=7),
        entry(actions=[]),
        entry(actions=["delete"]),
        entry(actions=["scale", "scale"]),
        entry(actions="scale"),
        entry(extra=True),
        {k: v for k, v in entry().items() if k != "reason"},
    ],
)
def test_invalid_entries_are_refused(bad):
    with pytest.raises(FreezeConfigError) as exc:
        parse_freezes(json.dumps([entry(), bad]))
    assert str(exc.value).startswith("freeze 1:")


def test_a_past_window_is_valid_and_inactive():
    old = freezes(entry(start="2020-01-01T00:00:00Z", until="2020-01-02T00:00:00Z"))
    assert active_freeze(old, "sandbox", SCALE, T0) is None


# --- windows ------------------------------------------------------------------


def test_start_is_inclusive_and_until_exclusive():
    fs = freezes(entry())
    assert active_freeze(fs, "sandbox", SCALE, T0 - 1) is None
    assert active_freeze(fs, "sandbox", SCALE, T0) == (fs[0], T1)
    assert active_freeze(fs, "sandbox", SCALE, T1 - 1) == (fs[0], T1)
    assert active_freeze(fs, "sandbox", SCALE, T1) is None


def test_other_namespaces_are_not_frozen():
    fs = freezes(entry(namespaces=["other"]))
    assert active_freeze(fs, "sandbox", SCALE, T0) is None
    assert active_freeze(fs, "other", SCALE, T0) is not None


def test_star_freezes_every_namespace():
    assert active_freeze(freezes(entry(namespaces=["*"])), "sandbox", SCALE, T0) is not None


def test_actions_limit_the_freeze():
    fs = freezes(entry(actions=["scale"]))
    assert active_freeze(fs, "sandbox", SCALE, T0) is not None
    assert active_freeze(fs, "sandbox", RESTART, T0) is None
    assert active_freeze(fs, "sandbox", ROLLBACK, T0) is None


def test_until_follows_windows_that_touch_or_overlap():
    fs = freezes(
        entry(until="2026-12-24T00:00:00Z", reason="first"),
        # Starts exactly when the first ends.
        entry(start="2026-12-24T00:00:00Z", until="2026-12-26T00:00:00Z"),
        entry(start="2026-12-25T00:00:00Z", until="2026-12-27T00:00:00Z"),
        # A gap: not part of the frozen period.
        entry(start="2026-12-28T00:00:00Z", until="2026-12-29T00:00:00Z"),
        # Another namespace does not extend it.
        entry(namespaces=["other"], start="2026-12-27T00:00:00Z", until="2026-12-28T00:00:00Z"),
    )
    freeze, until = active_freeze(fs, "sandbox", SCALE, T0)
    assert freeze.reason == "first"
    assert format_time(until) == "2026-12-27T00:00:00Z"


def test_the_reason_is_that_of_the_longest_current_window():
    fs = freezes(entry(reason="short", until="2026-12-24T00:00:00Z"), entry(reason="long"))
    assert active_freeze(fs, "sandbox", SCALE, T0)[0].reason == "long"


# --- reading the parameter ----------------------------------------------------


@pytest.fixture
def ssm(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.client("ssm", region_name=REGION)


def put(ssm, value: str) -> None:
    ssm.put_parameter(Name=PARAM, Value=value, Type="String", Overwrite=True)


def test_a_freeze_refuses_with_its_reason_and_until(ssm):
    put(ssm, json.dumps([entry()]))
    with pytest.raises(GuardError) as exc:
        FreezeStore(ssm, PARAM).check("sandbox", SCALE, T0)
    assert exc.value.code == "change_frozen"
    assert exc.value.details == {"namespace": "sandbox", "reason": "year end", "until": UNTIL}


def test_no_active_freeze_passes(ssm):
    put(ssm, json.dumps([entry()]))
    FreezeStore(ssm, PARAM).check("sandbox", SCALE, T1)


def test_a_missing_parameter_refuses(ssm):
    with pytest.raises(GuardError) as exc:
        FreezeStore(ssm, PARAM).check("sandbox", SCALE, T0)
    assert exc.value.code == "remediation_disabled"
    assert exc.value.details == {"scope": "freezes", "reason": "unreadable"}


def test_an_unreadable_parameter_refuses():
    client = MagicMock()
    client.get_parameter.side_effect = RuntimeError("throttled")
    with pytest.raises(GuardError) as exc:
        FreezeStore(client, PARAM).check("sandbox", SCALE, T0)
    assert exc.value.details == {"scope": "freezes", "reason": "unreadable"}


@pytest.mark.parametrize("value", ["not json", "{}", json.dumps([entry(until=START)])])
def test_an_invalid_parameter_refuses_every_namespace(ssm, value):
    put(ssm, value)
    with pytest.raises(GuardError) as exc:
        FreezeStore(ssm, PARAM).check("anything", RESTART, T0)
    assert exc.value.code == "remediation_disabled"
    assert exc.value.details == {"scope": "freezes", "reason": "invalid"}


def test_the_parameter_is_read_on_every_check(ssm):
    store = FreezeStore(ssm, PARAM)
    put(ssm, "[]")
    store.check("sandbox", SCALE, T0)
    put(ssm, json.dumps([entry()]))
    with pytest.raises(GuardError):
        store.check("sandbox", SCALE, T0)


def test_without_a_parameter_nothing_is_checked():
    assert freeze_store(MagicMock(), None) is None
    check_not_frozen(None, "sandbox", SCALE, T0)


def test_settings_read_the_parameter_name():
    base = {
        "CLUSTER_NAME": "c",
        "PLANS_TABLE": "t",
        "ALLOWED_NAMESPACES": "sandbox",
        "MIN_REPLICAS": "0",
        "MAX_REPLICAS": "5",
    }
    assert load_settings(base).freezes_param is None
    assert load_settings(base | {"FREEZES_PARAM": PARAM}).freezes_param == PARAM
