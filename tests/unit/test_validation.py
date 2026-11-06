import pytest

from handrail_common.envelope import (
    BLAST_RADIUS_EXCEEDED,
    FORBIDDEN_NAMESPACE,
    INVALID_ARGUMENT,
    OUT_OF_BOUNDS,
)
from handrail_common.validation import (
    GuardError,
    check_replica_step,
    max_replica_step,
    require_int,
    require_name,
    require_namespace,
    require_plan_id,
    require_verify_args,
    validate_scale_args,
)

ALLOWED = {"sandbox"}


def bounds_for(_namespace: str) -> tuple[int, int]:
    return 0, 5


@pytest.mark.parametrize("value", ["sandbox", "a", "team-1", "x" * 63])
def test_namespace_valid(value):
    assert require_namespace(value) == value


@pytest.mark.parametrize("value", ["", "Sandbox", "-a", "a-", "a.b", "a_b", "x" * 64, None, 5])
def test_namespace_invalid(value):
    with pytest.raises(GuardError) as exc:
        require_namespace(value)
    assert exc.value.code == INVALID_ARGUMENT


@pytest.mark.parametrize("value", ["web", "web.v1", "web-1.api"])
def test_name_valid(value):
    assert require_name(value) == value


@pytest.mark.parametrize("value", ["", "Web", "web..v1", ".web", "web/x", "x" * 254, None])
def test_name_invalid(value):
    with pytest.raises(GuardError):
        require_name(value)


@pytest.mark.parametrize("value", [True, False, 1.0, "3", None])
def test_int_rejects_non_integers(value):
    with pytest.raises(GuardError) as exc:
        require_int(value, "replicas")
    assert exc.value.code == INVALID_ARGUMENT


def test_scale_args_ok():
    args = {"namespace": "sandbox", "name": "web", "replicas": 3}
    assert validate_scale_args(args, ALLOWED, bounds_for) == ("sandbox", "web", 3)


def test_scale_args_forbidden_namespace():
    with pytest.raises(GuardError) as exc:
        validate_scale_args(
            {"namespace": "prod", "name": "web", "replicas": 1}, ALLOWED, bounds_for
        )
    assert exc.value.code == FORBIDDEN_NAMESPACE


@pytest.mark.parametrize("replicas", [-1, 6])
def test_scale_args_out_of_bounds(replicas):
    with pytest.raises(GuardError) as exc:
        validate_scale_args(
            {"namespace": "sandbox", "name": "web", "replicas": replicas}, ALLOWED, bounds_for
        )
    assert exc.value.code == OUT_OF_BOUNDS


@pytest.mark.parametrize("replicas", [0, 5])
def test_scale_args_bounds_inclusive(replicas):
    args = {"namespace": "sandbox", "name": "web", "replicas": replicas}
    assert validate_scale_args(args, ALLOWED, bounds_for)[2] == replicas


def test_invalid_argument_checked_before_allowlist():
    with pytest.raises(GuardError) as exc:
        validate_scale_args({"namespace": "BAD", "name": "web", "replicas": 1}, ALLOWED, bounds_for)
    assert exc.value.code == INVALID_ARGUMENT


def test_plan_id():
    pid = "3f2b8c1e-8d4a-4f6b-9a3e-2c1d0e9f8a7b"
    assert require_plan_id(pid) == pid
    for bad in [pid.upper(), "not-a-uuid", "", None, 1]:
        with pytest.raises(GuardError):
            require_plan_id(bad)


@pytest.mark.parametrize(
    ("replicas_from", "percent", "step"),
    [
        (0, 50, 1),
        (0, 100, 1),
        (0, 200, 1),
        (1, 50, 1),
        (1, 100, 1),
        (1, 200, 2),
        (3, 50, 2),
        (3, 100, 3),
        (3, 200, 6),
        (10, 25, 3),
    ],
)
def test_max_replica_step(replicas_from, percent, step):
    assert max_replica_step(replicas_from, percent) == step


@pytest.mark.parametrize(
    ("replicas_from", "percent", "allowed", "refused"),
    [
        (0, 100, [0, 1], [2]),
        (1, 50, [0, 1, 2], [3]),
        (1, 100, [0, 1, 2], [3]),
        (1, 200, [0, 3], [4]),
        (3, 50, [1, 5], [0, 6]),
        (3, 100, [0, 6], [7]),
        (3, 200, [0, 9], [10]),
    ],
)
def test_replica_step_at_the_limits(replicas_from, percent, allowed, refused):
    for replicas_to in allowed:
        check_replica_step(replicas_from, replicas_to, percent)
    for replicas_to in refused:
        with pytest.raises(GuardError) as exc:
            check_replica_step(replicas_from, replicas_to, percent)
        assert exc.value.code == BLAST_RADIUS_EXCEEDED


def test_replica_step_details():
    with pytest.raises(GuardError) as exc:
        check_replica_step(3, 10, 100)
    assert exc.value.details == {"from": 3, "to": 10, "max_step": 3, "max_step_percent": 100}


def verify_args(args: dict, enabled: bool = True, revert_allowed: bool = True):
    return require_verify_args(args, enabled, (60, 1800), revert_allowed)


@pytest.mark.parametrize("window", [60, 61, 1799, 1800])
def test_verify_window_within_bounds(window):
    assert verify_args({"verify_window_seconds": window}) == (window, False)


@pytest.mark.parametrize("window", [59, 1801, 0, -60, "120", 120.0, True])
def test_verify_window_outside_bounds_or_not_an_integer(window):
    with pytest.raises(GuardError) as exc:
        verify_args({"verify_window_seconds": window})
    assert exc.value.code == INVALID_ARGUMENT


def test_verify_arguments_are_optional():
    assert verify_args({}) == (None, False)
    assert verify_args({"revert_on_failure": False}) == (None, False)
    assert verify_args({}, enabled=False) == (None, False)


def test_revert_needs_an_explicit_window():
    assert verify_args({"revert_on_failure": True, "verify_window_seconds": 60}) == (60, True)
    with pytest.raises(GuardError, match="explicit verify_window_seconds"):
        verify_args({"revert_on_failure": True})


@pytest.mark.parametrize("value", ["true", 1, None])
def test_revert_must_be_a_boolean(value):
    with pytest.raises(GuardError, match="must be a boolean"):
        verify_args({"revert_on_failure": value, "verify_window_seconds": 60})


def test_revert_is_refused_where_it_is_not_supported():
    with pytest.raises(GuardError, match="not supported"):
        verify_args({"revert_on_failure": True, "verify_window_seconds": 60}, revert_allowed=False)


@pytest.mark.parametrize(
    "args",
    [{"verify_window_seconds": 60}, {"revert_on_failure": True, "verify_window_seconds": 60}],
)
def test_verify_arguments_need_verification_on_the_server(args):
    with pytest.raises(GuardError, match="not enabled"):
        verify_args(args, enabled=False)
