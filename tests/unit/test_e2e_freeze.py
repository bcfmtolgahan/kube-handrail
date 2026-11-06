"""The e2e freeze helpers never drop or override a freeze an operator stored."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "e2e"))

import e2e_gateway  # noqa: E402

from handrail_common.freezes import format_time, parse_freezes  # noqa: E402

NOW = 1_800_000_000
OPERATOR = {
    "namespaces": ["payments"],
    "start": format_time(NOW - 86400),
    "until": format_time(NOW + 86400),
    "reason": "year-end freeze",
}


def stored(*entries: dict) -> str:
    return json.dumps(list(entries))


def test_test_windows_are_added_to_the_stored_freezes():
    value = json.loads(e2e_gateway.freeze_value(stored(OPERATOR), ("e2e-freeze-other",), NOW))
    assert value[0] == OPERATOR
    assert value[1]["namespaces"] == ["e2e-freeze-other"]


def test_removing_the_test_windows_keeps_the_stored_freezes():
    assert json.loads(e2e_gateway.freeze_value(stored(OPERATOR), (), NOW)) == [OPERATOR]
    assert e2e_gateway.freeze_value("[]", (), NOW) == "[]"


def test_the_value_is_valid_for_the_lambdas():
    parse_freezes(e2e_gateway.freeze_value(stored(OPERATOR), (e2e_gateway.NAMESPACE,), NOW))


def test_a_freeze_elsewhere_lets_the_run_proceed():
    assert not e2e_gateway.sandbox_freeze_during_run(stored(OPERATOR), NOW)


def test_a_past_sandbox_freeze_lets_the_run_proceed():
    past = {**OPERATOR, "namespaces": [e2e_gateway.NAMESPACE], "until": format_time(NOW - 1)}
    assert not e2e_gateway.sandbox_freeze_during_run(stored(past), NOW)


def test_a_current_or_upcoming_sandbox_freeze_stops_the_run():
    current = {**OPERATOR, "namespaces": [e2e_gateway.NAMESPACE]}
    upcoming = {
        **current,
        "start": format_time(NOW + 600),
        "until": format_time(NOW + 7200),
    }
    everywhere = {**OPERATOR, "namespaces": ["*"], "actions": ["restart"]}
    for entry in (current, upcoming, everywhere):
        assert e2e_gateway.sandbox_freeze_during_run(stored(entry), NOW)


def test_a_missing_parameter_name_stops_before_any_case(monkeypatch):
    monkeypatch.delenv("FREEZES_PARAM", raising=False)
    try:
        e2e_gateway.freezes_param()
    except SystemExit as exc:
        assert "FREEZES_PARAM" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
