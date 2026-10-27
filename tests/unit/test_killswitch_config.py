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


@pytest.mark.parametrize(
    "override",
    [
        {"CLUSTER_NAME": ""},
        {"ALLOWED_NAMESPACES": " , "},
        {"MAX_REPLICAS": "x"},
        {"MIN_REPLICAS": "6"},
        {"PLAN_TTL_SECONDS": "0"},
        {"MAX_REPLICAS": ""},
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


def test_killswitch_cache_and_refresh():
    now = [0.0]
    ssm = FakeSSM("true")
    switch = KillSwitch(ssm, "/p", ttl=30, clock=lambda: now[0])
    assert switch.enabled()
    ssm.value = "false"
    now[0] = 10
    assert switch.enabled() and ssm.calls == 1
    now[0] = 31
    assert not switch.enabled() and ssm.calls == 2


@pytest.mark.parametrize("value", ["TRUE", "yes", "", RuntimeError("boom")])
def test_killswitch_fails_closed(value):
    assert not KillSwitch(FakeSSM(value), "/p").enabled()
