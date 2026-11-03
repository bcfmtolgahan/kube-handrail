import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "approve_script", Path(__file__).parents[2] / "scripts" / "approve.py"
)
approve = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(approve)

URL = "https://abc123.lambda-url.eu-central-1.on.aws/"
PLAN_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


def test_region_from_url():
    assert approve.region_of(URL, None) == "eu-central-1"
    assert approve.region_of("https://example.com/", "us-east-1") == "us-east-1"
    with pytest.raises(SystemExit):
        approve.region_of("https://example.com/", None)


def test_requests_are_signed_for_lambda():
    request = approve.signed_request(URL, "eu-central-1", "POST", b"{}")
    auth = request.get_header("Authorization")
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/eu-central-1/lambda/aws4_request" in auth
    assert request.get_method() == "POST" and request.data == b"{}"
    assert "x-amz-content-sha256" in auth
    assert request.get_header("X-amz-content-sha256") == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )


@pytest.mark.parametrize(
    ("decision", "reason", "apply", "body"),
    [
        ("approve", None, True, {"plan_id": PLAN_ID, "decision": "approve"}),
        ("approve", None, False, {"plan_id": PLAN_ID, "decision": "approve", "apply": False}),
        (
            "reject",
            "no",
            False,
            {"plan_id": PLAN_ID, "decision": "reject", "reason": "no", "apply": False},
        ),
    ],
)
def test_decide_body(monkeypatch, decision, reason, apply, body):
    sent = []
    monkeypatch.setattr(
        approve, "send", lambda request: sent.append(request) or (200, {"ok": True})
    )
    approve.decide(URL, "eu-central-1", PLAN_ID, decision, reason, apply)
    (request,) = sent
    assert json.loads(request.data) == body
    assert request.full_url == URL


def test_show_puts_the_plan_id_in_the_query(monkeypatch):
    sent = []
    monkeypatch.setattr(
        approve, "send", lambda request: sent.append(request) or (200, {"ok": True})
    )
    approve.show(URL, "eu-central-1", PLAN_ID)
    assert sent[0].full_url == f"{URL}?plan_id={PLAN_ID}" and sent[0].get_method() == "GET"
