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
