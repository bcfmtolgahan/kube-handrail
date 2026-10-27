import pytest

from handrail_common.envelope import FORBIDDEN_NAMESPACE, INVALID_ARGUMENT, OUT_OF_BOUNDS
from handrail_common.validation import (
    GuardError,
    require_int,
    require_name,
    require_namespace,
    require_plan_id,
    validate_scale_args,
)

ALLOWED = {"sandbox"}


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
    assert validate_scale_args(args, ALLOWED, 0, 5) == ("sandbox", "web", 3)


def test_scale_args_forbidden_namespace():
    with pytest.raises(GuardError) as exc:
        validate_scale_args({"namespace": "prod", "name": "web", "replicas": 1}, ALLOWED, 0, 5)
    assert exc.value.code == FORBIDDEN_NAMESPACE


@pytest.mark.parametrize("replicas", [-1, 6])
def test_scale_args_out_of_bounds(replicas):
    with pytest.raises(GuardError) as exc:
        validate_scale_args(
            {"namespace": "sandbox", "name": "web", "replicas": replicas}, ALLOWED, 0, 5
        )
    assert exc.value.code == OUT_OF_BOUNDS


@pytest.mark.parametrize("replicas", [0, 5])
def test_scale_args_bounds_inclusive(replicas):
    args = {"namespace": "sandbox", "name": "web", "replicas": replicas}
    assert validate_scale_args(args, ALLOWED, 0, 5)[2] == replicas


def test_invalid_argument_checked_before_allowlist():
    with pytest.raises(GuardError) as exc:
        validate_scale_args({"namespace": "BAD", "name": "web", "replicas": 1}, ALLOWED, 0, 5)
    assert exc.value.code == INVALID_ARGUMENT


def test_plan_id():
    pid = "3f2b8c1e-8d4a-4f6b-9a3e-2c1d0e9f8a7b"
    assert require_plan_id(pid) == pid
    for bad in [pid.upper(), "not-a-uuid", "", None, 1]:
        with pytest.raises(GuardError):
            require_plan_id(bad)
