import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from handrail_common.freezes import FreezeStore, format_time, parse_freezes
from handrail_common.validation import GuardError

_SPEC = importlib.util.spec_from_file_location(
    "freeze_script", Path(__file__).parents[2] / "scripts" / "freeze.py"
)
freeze = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(freeze)

REGION = "us-east-1"
PARAM = "/handrail/freezes"
NOW = 1_700_000_000


@pytest.fixture
def ssm(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("FREEZES_PARAM", raising=False)
    with mock_aws():
        client = boto3.client("ssm", region_name=REGION)
        client.put_parameter(Name=PARAM, Value="[]", Type="String")
        yield client


def run(ssm, *argv: str, now: int = NOW) -> None:
    freeze.main(["--parameter", PARAM, *argv], ssm=ssm, now=now)


def stored(ssm) -> list:
    return json.loads(ssm.get_parameter(Name=PARAM)["Parameter"]["Value"])


def add(ssm, *extra: str, until: int = NOW + 3600) -> None:
    run(
        ssm, "add", "--namespace", "sandbox", "--until", format_time(until), "--reason", "r", *extra
    )


def test_add_stores_a_window_from_now_that_the_lambdas_enforce(ssm):
    add(ssm)
    assert stored(ssm) == [
        {
            "namespaces": ["sandbox"],
            "start": format_time(NOW),
            "until": format_time(NOW + 3600),
            "reason": "r",
        }
    ]
    with pytest.raises(GuardError) as exc:
        FreezeStore(ssm, PARAM).check("sandbox", "scale_deployment", NOW)
    assert exc.value.code == "change_frozen"


def test_add_with_actions_and_start(ssm):
    add(ssm, "--action", "scale", "--start", format_time(NOW + 60), "--namespace", "other")
    [entry] = stored(ssm)
    assert entry["actions"] == ["scale"] and entry["namespaces"] == ["sandbox", "other"]


@pytest.mark.parametrize(
    "extra",
    [("--start", format_time(NOW + 7200)), ("--start", "tomorrow"), ("--reason", "a\nb")],
)
def test_add_refuses_what_the_lambdas_would_refuse(ssm, extra):
    with pytest.raises(SystemExit):
        add(ssm, *extra)
    assert stored(ssm) == []


def test_add_refuses_a_window_that_has_ended(ssm):
    with pytest.raises(SystemExit):
        run(
            ssm,
            "add",
            "--namespace",
            "sandbox",
            "--start",
            format_time(NOW - 60),
            "--until",
            format_time(NOW),
            "--reason",
            "r",
        )
    assert stored(ssm) == []


def test_list_shows_the_state_of_each(ssm, capsys):
    add(ssm)
    add(ssm, "--start", format_time(NOW + 7200), until=NOW + 9000)
    capsys.readouterr()
    run(ssm, "list")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("0  active") and out[1].startswith("1  upcoming")
    run(ssm, "list", now=NOW + 3600)
    assert capsys.readouterr().out.splitlines()[0].startswith("0  expired")


def test_remove_by_index_and_expired(ssm):
    add(ssm, until=NOW + 60)
    add(ssm, until=NOW + 120)
    add(ssm, until=NOW + 180)
    run(ssm, "remove", "1")
    assert [e["until"] for e in stored(ssm)] == [format_time(NOW + 60), format_time(NOW + 180)]
    run(ssm, "remove", "--expired", now=NOW + 60)
    assert [e["until"] for e in stored(ssm)] == [format_time(NOW + 180)]
    with pytest.raises(SystemExit):
        run(ssm, "remove", "5")


def test_remove_all_resets_even_an_invalid_value(ssm):
    ssm.put_parameter(Name=PARAM, Value="{broken", Type="String", Overwrite=True)
    with pytest.raises(SystemExit):
        run(ssm, "list")
    with pytest.raises(SystemExit):
        add(ssm)
    run(ssm, "remove", "--all")
    assert parse_freezes(ssm.get_parameter(Name=PARAM)["Parameter"]["Value"]) == []


def test_remove_needs_exactly_one_selection(ssm):
    for argv in (("remove",), ("remove", "0", "--all"), ("remove", "--expired", "--all")):
        with pytest.raises(SystemExit):
            run(ssm, *argv)


def test_the_parameter_name_comes_from_the_environment():
    assert freeze.parameter_name(None, {"FREEZES_PARAM": "/x/freezes"}) == "/x/freezes"
    assert freeze.parameter_name("/y/freezes", {"FREEZES_PARAM": "/x/freezes"}) == "/y/freezes"
