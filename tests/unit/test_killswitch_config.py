import pytest

from handrail_common.config import ConfigError, load_settings
from handrail_common.killswitch import KillSwitch

BASE = {
    "CLUSTER_NAME": "demo",
    "PLANS_TABLE": "plans",
    "ALLOWED_NAMESPACES": "sandbox, demo ,",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
    "PLAN_TTL_SECONDS": "300",
}


def test_settings_parse():
    s = load_settings(BASE)
    assert s.allowed_namespaces == {"sandbox", "demo"}
    assert (s.min_replicas, s.max_replicas, s.plan_ttl_seconds) == (0, 5, 300)
    assert s.remediation_enabled_param is None
    assert (s.flap_window_seconds, s.object_hourly_apply_budget) == (600, 6)
    assert (s.plan_rate_limit, s.plan_rate_window_seconds) == (20, 600)
    assert (s.apply_cooldown_seconds, s.global_hourly_apply_budget) == (60, 30)


def test_breaker_settings_parse():
    s = load_settings(
        BASE
        | {
            "FLAP_WINDOW_SECONDS": "900",
            "OBJECT_HOURLY_APPLY_BUDGET": "3",
            "PLAN_RATE_LIMIT": "10",
            "PLAN_RATE_WINDOW_SECONDS": "300",
            "APPLY_COOLDOWN_SECONDS": "0",
            "GLOBAL_HOURLY_APPLY_BUDGET": "12",
        }
    )
    assert (s.flap_window_seconds, s.object_hourly_apply_budget) == (900, 3)
    assert (s.plan_rate_limit, s.plan_rate_window_seconds) == (10, 300)
    assert (s.apply_cooldown_seconds, s.global_hourly_apply_budget) == (0, 12)


@pytest.mark.parametrize(
    "override",
    [
        {"CLUSTER_NAME": ""},
        {"ALLOWED_NAMESPACES": " , "},
        {"MAX_REPLICAS": "x"},
        {"MIN_REPLICAS": "6"},
        {"PLAN_TTL_SECONDS": "0"},
        {"MAX_REPLICAS": ""},
        {"FLAP_WINDOW_SECONDS": "0"},
        {"OBJECT_HOURLY_APPLY_BUDGET": "-1"},
        {"PLAN_RATE_LIMIT": "x"},
        {"PLAN_RATE_WINDOW_SECONDS": "0"},
        {"APPLY_COOLDOWN_SECONDS": "-1"},
        {"GLOBAL_HOURLY_APPLY_BUDGET": "0"},
    ],
)
def test_settings_invalid(override):
    with pytest.raises(ConfigError):
        load_settings(BASE | override)


def test_write_requires_param():
    with pytest.raises(ConfigError):
        load_settings(BASE, write=True)
    assert load_settings(BASE | {"REMEDIATION_ENABLED_PARAM": "/p"}, write=True)


class FakeSSM:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def get_parameter(self, Name):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return {"Parameter": {"Value": self.value}}


def test_killswitch_reads_every_time():
    ssm = FakeSSM("true")
    switch = KillSwitch(ssm, "/p")
    assert switch.enabled()
    ssm.value = "false"
    assert not switch.enabled() and ssm.calls == 2


@pytest.mark.parametrize("value", ["TRUE", "yes", "", RuntimeError("boom")])
def test_killswitch_fails_closed(value):
    assert not KillSwitch(FakeSSM(value), "/p").enabled()
