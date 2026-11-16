import pytest

from handrail_common.config import ConfigError, load_settings
from handrail_common.killswitch import KillSwitch

AGENTS = (
    '{"client-a": {"name": "smoke_test", "hourly_apply_budget": 2},'
    ' "client-b": {"name": "devops_agent"}}'
)
BASE = {
    "CLUSTER_NAME": "demo",
    "PLANS_TABLE": "plans",
    "ALLOWED_NAMESPACES": "sandbox, demo ,",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
    "PLAN_TTL_SECONDS": "300",
    "AGENTS": AGENTS,
    "AGENT_KILL_SWITCH_PREFIX": "/kh/agents/",
}


def test_settings_parse():
    s = load_settings(BASE)
    assert s.allowed_namespaces == {"sandbox", "demo"}
    assert (s.min_replicas, s.max_replicas, s.plan_ttl_seconds) == (0, 5, 300)
    assert s.remediation_enabled_param is None
    assert (s.flap_window_seconds, s.object_hourly_apply_budget) == (600, 6)
    assert (s.apply_cooldown_seconds, s.global_hourly_apply_budget) == (60, 30)
    assert s.max_replica_step_percent == 100
    # Without an explicit mode, apply requires a server approval.
    assert s.approval_mode == "server" and s.approval_required
    assert (s.approval_wait_ttl_seconds, s.apply_after_approval_ttl_seconds) == (900, 120)
    assert (s.approval_request_cooldown_seconds, s.approval_request_limit) == (60, 10)
    assert s.approval_topic_arn is None and s.approval_url is None


@pytest.mark.parametrize("key", ["AGENTS", "AGENT_KILL_SWITCH_PREFIX"])
def test_agents_and_their_switch_prefix_are_required(key):
    """Every caller must be an agent, so the Lambda does not start without them, whatever
    a removed CALLER_INTERCEPTOR says."""
    env = BASE | {"CALLER_INTERCEPTOR": "false"}
    del env[key]
    with pytest.raises(ConfigError):
        load_settings(env)
    with pytest.raises(ConfigError):
        load_settings(env | {key: " "})


def test_agents_are_parsed_by_client_id():
    s = load_settings(BASE | {"AGENTS": AGENTS})
    assert {cid: a.name for cid, a in s.agents.items()} == {
        "client-a": "smoke_test",
        "client-b": "devops_agent",
    }
    assert s.agents["client-a"].client_id == "client-a"
    assert s.agent_kill_switch_prefix == "/kh/agents/"
    # A budget in AGENTS overrides the default for that agent only.
    assert (s.agents["client-a"].hourly_apply_budget, s.agents["client-b"].hourly_apply_budget) == (
        2,
        10,
    )
    s = load_settings(BASE | {"AGENTS": AGENTS, "AGENT_HOURLY_APPLY_BUDGET": "4"})
    assert s.agents["client-b"].hourly_apply_budget == s.agent_budget("devops_agent") == 4


@pytest.mark.parametrize("budget", ["0", "-1", "x"])
def test_the_default_agent_budget_must_be_positive(budget):
    with pytest.raises(ConfigError):
        load_settings(BASE | {"AGENT_HOURLY_APPLY_BUDGET": budget})


@pytest.mark.parametrize(
    "agents",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        '{"client-a": "smoke_test"}',
        '{"client-a": {}}',
        '{"client-a": {"name": "Smoke"}}',
        '{"client-a": {"name": "1agent"}}',
        '{"client-a": {"name": "a-b"}}',
        '{"client-a": {"name": "' + "a" * 33 + '"}}',
        '{"": {"name": "smoke_test"}}',
        '{"client-a": {"name": "x"}, "client-b": {"name": "x"}}',
        '{"client-a": {"name": "x", "hourly_apply_budget": 0}}',
        '{"client-a": {"name": "x", "hourly_apply_budget": true}}',
        '{"client-a": {"name": "x", "hourly_apply_budget": "2"}}',
        '{"client-a": {"name": "x", "hourly_apply_budget": 1.5}}',
    ],
)
def test_bad_agents_are_refused(agents):
    env = BASE | {"AGENTS": agents}
    if agents is None:
        del env["AGENTS"]
    with pytest.raises(ConfigError):
        load_settings(env)


def test_bounds_are_read_per_namespace():
    s = load_settings(BASE)
    assert s.bounds_for("sandbox") == s.bounds_for("demo") == (0, 5)


@pytest.mark.parametrize(
    ("mode", "required"), [("client", False), ("server", True), ("both", True)]
)
def test_approval_modes(mode, required):
    assert load_settings(BASE | {"APPROVAL_MODE": mode}).approval_required is required


def test_approval_settings_parse():
    s = load_settings(
        BASE
        | {
            "APPROVAL_MODE": "both",
            "APPROVAL_WAIT_TTL_SECONDS": "600",
            "APPLY_AFTER_APPROVAL_TTL_SECONDS": "30",
            "APPROVAL_REQUEST_COOLDOWN_SECONDS": "0",
            "APPROVAL_REQUEST_LIMIT": "3",
            "APPROVAL_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:approvals",
            "APPROVAL_URL": "https://example.lambda-url.us-east-1.on.aws/",
        }
    )
    assert (s.approval_wait_ttl_seconds, s.apply_after_approval_ttl_seconds) == (600, 30)
    assert (s.approval_request_cooldown_seconds, s.approval_request_limit) == (0, 3)
    assert s.approval_topic_arn.endswith(":approvals") and s.approval_url.startswith("https://")


def test_client_mode_plan_ttl_fits_the_default_approval_wait():
    # Terraform's client-mode default is 900 s, the longest TTL the default wait allows.
    s = load_settings(BASE | {"APPROVAL_MODE": "client", "PLAN_TTL_SECONDS": "900"})
    assert (s.plan_ttl_seconds, s.approval_wait_ttl_seconds) == (900, 900)


def test_plan_ttl_at_most_an_hour():
    env = BASE | {"PLAN_TTL_SECONDS": "3600", "APPROVAL_WAIT_TTL_SECONDS": "3600"}
    assert load_settings(env).plan_ttl_seconds == 3600


def test_breaker_settings_parse():
    s = load_settings(
        BASE
        | {
            "FLAP_WINDOW_SECONDS": "900",
            "OBJECT_HOURLY_APPLY_BUDGET": "3",
            "PLAN_RATE_LIMIT": "x",
            "PLAN_RATE_WINDOW_SECONDS": "0",
            "APPLY_COOLDOWN_SECONDS": "0",
            "GLOBAL_HOURLY_APPLY_BUDGET": "12",
            "MAX_REPLICA_STEP_PERCENT": "250",
        }
    )
    assert (s.flap_window_seconds, s.object_hourly_apply_budget) == (900, 3)
    # The plan rate limit was removed: its variables are ignored, even when invalid.
    assert not hasattr(s, "plan_rate_limit")
    assert (s.apply_cooldown_seconds, s.global_hourly_apply_budget) == (0, 12)
    assert s.max_replica_step_percent == 250


@pytest.mark.parametrize(
    "override",
    [
        {"CLUSTER_NAME": ""},
        {"ALLOWED_NAMESPACES": " , "},
        {"MAX_REPLICAS": "x"},
        {"MIN_REPLICAS": "6"},
        {"PLAN_TTL_SECONDS": "0"},
        {"PLAN_TTL_SECONDS": "3601"},
        {"MAX_REPLICAS": ""},
        {"FLAP_WINDOW_SECONDS": "0"},
        {"OBJECT_HOURLY_APPLY_BUDGET": "-1"},
        {"APPLY_COOLDOWN_SECONDS": "-1"},
        {"GLOBAL_HOURLY_APPLY_BUDGET": "0"},
        {"MAX_REPLICA_STEP_PERCENT": "0"},
        {"MAX_REPLICA_STEP_PERCENT": "-50"},
        {"MAX_REPLICA_STEP_PERCENT": "1.5"},
        {"APPROVAL_MODE": "none"},
        {"APPROVAL_MODE": "SERVER"},
        {"APPROVAL_WAIT_TTL_SECONDS": "0"},
        # Shorter than the plan TTL: requesting an approval would shorten the plan.
        {"APPROVAL_WAIT_TTL_SECONDS": "299"},
        {"APPLY_AFTER_APPROVAL_TTL_SECONDS": "0"},
        {"APPROVAL_REQUEST_COOLDOWN_SECONDS": "-1"},
        {"APPROVAL_REQUEST_LIMIT": "0"},
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


def test_killswitch_state_tells_unreadable_from_off():
    assert KillSwitch(FakeSSM("true"), "/p").state() is True
    assert KillSwitch(FakeSSM("false"), "/p").state() is False
    assert KillSwitch(FakeSSM(""), "/p").state() is False
    assert KillSwitch(FakeSSM(RuntimeError("throttled")), "/p").state() is None


def test_verify_settings_parse():
    s = load_settings(BASE)
    assert (s.verify_window_default_seconds, s.verify_window_bounds) == (120, (60, 1800))
    assert s.verify_grace_seconds == 300
    s = load_settings(
        BASE
        | {
            "VERIFY_WINDOW_DEFAULT_SECONDS": "90",
            "VERIFY_WINDOW_MIN_SECONDS": "90",
            "VERIFY_WINDOW_MAX_SECONDS": "600",
            "VERIFY_GRACE_SECONDS": "120",
        }
    )
    assert (s.verify_window_default_seconds, s.verify_window_bounds) == (90, (90, 600))
    assert s.verify_grace_seconds == 120


@pytest.mark.parametrize(
    "override",
    [
        {"VERIFY_WINDOW_MIN_SECONDS": "59", "VERIFY_WINDOW_DEFAULT_SECONDS": "59"},
        {"VERIFY_WINDOW_DEFAULT_SECONDS": "30"},
        {"VERIFY_WINDOW_DEFAULT_SECONDS": "2000"},
        {"VERIFY_WINDOW_MAX_SECONDS": "100"},
        {"VERIFY_GRACE_SECONDS": "0"},
        {"VERIFY_WINDOW_MAX_SECONDS": "x"},
    ],
)
def test_verify_settings_invalid(override):
    with pytest.raises(ConfigError):
        load_settings(BASE | override)
