"""e2e_gateway.py --live-only: only the live-only cases run, and without the flag the run
is unchanged."""

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "e2e"))

import e2e_gateway  # noqa: E402


def test_default_arguments_are_unchanged():
    args = e2e_gateway.parse_args([])
    assert args.live_only is False
    assert args.with_verify is True
    args = e2e_gateway.parse_args(["--with-rollout", "--with-freeze", "--with-expiry"])
    assert (args.with_rollout, args.with_freeze, args.with_expiry) == (True, True, True)


def test_live_only_drops_the_cases_the_integration_suite_covers():
    args = e2e_gateway.parse_args(
        ["--live-only", "--with-rollout", "--with-freeze", "--with-expiry", "--with-identity"]
    )
    assert args.live_only is True
    assert (args.with_rollout, args.with_freeze, args.with_expiry, args.with_verify) == (
        False,
        False,
        False,
        False,
    )
    assert args.with_identity is True


def _run(monkeypatch, live_only, with_identity=False, with_audit=False):
    calls = []

    @contextlib.asynccontextmanager
    async def client(*_args, **_kwargs):
        yield None, None, None

    class Session:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            pass

    def record(name):
        async def fake(*_args, **_kwargs):
            calls.append(name)

        return fake

    monkeypatch.setenv("GATEWAY_URL", "https://gateway.invalid/mcp")
    monkeypatch.setattr(e2e_gateway, "fetch_token", lambda *_a: "token")
    monkeypatch.setattr(e2e_gateway, "auth_tests", lambda _t: calls.append("auth"))
    monkeypatch.setattr(e2e_gateway, "streamablehttp_client", client)
    monkeypatch.setattr(e2e_gateway, "ClientSession", Session)
    for name in ("live_only_tests", "guard_tests", "run_identity", "audit_tests"):
        monkeypatch.setattr(e2e_gateway, name, record(name))
    asyncio.run(
        e2e_gateway.run(
            False,
            "client",
            True,
            with_identity,
            True,
            True,
            with_audit,
            0,
            live_only,
        )
    )
    return calls


def test_live_only_runs_only_the_live_only_cases(monkeypatch):
    assert _run(monkeypatch, True) == ["auth", "live_only_tests"]
    assert _run(monkeypatch, True, with_identity=True, with_audit=True) == [
        "auth",
        "live_only_tests",
        "run_identity",
        "audit_tests",
    ]


def test_without_the_flag_every_case_runs(monkeypatch):
    assert _run(monkeypatch, False, with_audit=True) == ["auth", "guard_tests", "audit_tests"]


def test_live_only_cases_are_listed():
    assert any("Cognito" in case for case in e2e_gateway.LIVE_ONLY_CASES)
    assert any("concurrent" in case for case in e2e_gateway.LIVE_ONLY_CASES)
