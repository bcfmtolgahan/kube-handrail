from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from kubernetes import client as k8s

from handrail_common.config import ConfigError, load_settings
from handrail_common.gitops import INSTANCE_LABEL, is_gitops_managed, valid_marker

FIXTURES = Path(__file__).parent / "fixtures" / "gitops"
NONE: tuple[str, ...] = ()
LABEL_TRACKING = (INSTANCE_LABEL,)


def manifest(name: str) -> dict:
    return yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())


def model(name: str) -> k8s.V1Deployment:
    """The fixture as the Kubernetes client returns it."""
    meta = manifest(name)["metadata"]
    return k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(
            name=meta["name"],
            namespace=meta["namespace"],
            labels=meta.get("labels"),
            annotations=meta.get("annotations"),
        )
    )


def deployment(annotations=None, labels=None):
    return SimpleNamespace(metadata=SimpleNamespace(annotations=annotations, labels=labels))


# --- Golden manifests -------------------------------------------------------
#
# (fixture, managed with the default markers, managed with Argo CD label tracking listed)
GOLDEN = [
    ("argo-annotation-tracking", True, True),
    ("argo-label-tracking", False, True),
    ("flux-kustomization", True, True),
    ("flux-helmrelease", True, True),
    # The point of the change: a plain Helm release is not GitOps-managed by default.
    ("helm-release", False, True),
]


@pytest.mark.parametrize(("name", "default", "label_tracking"), GOLDEN)
def test_golden_manifests(name, default, label_tracking):
    for form in (manifest(name), model(name)):
        assert is_gitops_managed(form, NONE) is default
        assert is_gitops_managed(form, LABEL_TRACKING) is label_tracking


def test_every_flux_label_alone_is_a_marker():
    for key in (
        "kustomize.toolkit.fluxcd.io/name",
        "kustomize.toolkit.fluxcd.io/namespace",
        "helm.toolkit.fluxcd.io/name",
        "helm.toolkit.fluxcd.io/namespace",
    ):
        assert is_gitops_managed(deployment(labels={key: "x"}), NONE), key


def test_argo_tracking_label_alone_is_not_a_marker():
    assert not is_gitops_managed(deployment(labels={INSTANCE_LABEL: "web"}), NONE)


def test_a_configured_marker_matches_a_label_or_an_annotation():
    markers = ("example.com/managed-by-gitops",)
    assert is_gitops_managed(deployment(labels={markers[0]: ""}), markers)
    assert is_gitops_managed(deployment(annotations={markers[0]: "yes"}), markers)
    assert not is_gitops_managed(deployment(labels={"example.com/other": "x"}), markers)


def test_default_markers_only_where_controllers_put_them():
    # The Argo CD annotation as a label, or a Flux label as an annotation, is not set by
    # the controllers, so only the place they use counts.
    tracking = {"argocd.argoproj.io/tracking-id": "a:apps/Deployment:ns/web"}
    assert not is_gitops_managed(deployment(labels=tracking), NONE)
    assert not is_gitops_managed(
        deployment(annotations={"kustomize.toolkit.fluxcd.io/name": "apps"}), NONE
    )


def test_plain_deployment_not_managed():
    assert not is_gitops_managed(deployment({"note": "x"}, {"app": "web"}), NONE)
    assert not is_gitops_managed(deployment(), NONE)
    assert not is_gitops_managed({"metadata": {"labels": {"app": "web"}}}, NONE)


@pytest.mark.parametrize(
    "obj",
    [
        None,
        deployment(annotations="bad"),
        deployment(labels=["kustomize.toolkit.fluxcd.io/name"]),
        {"metadata": {"labels": "bad"}},
        {},
        {"metadata": "x"},
        {"metadata": None},
        SimpleNamespace(metadata=None),
        SimpleNamespace(),
    ],
)
def test_fails_closed(obj):
    assert is_gitops_managed(obj, NONE)


# --- GITOPS_MARKERS ---------------------------------------------------------

BASE = {
    "CLUSTER_NAME": "c",
    "PLANS_TABLE": "t",
    "ALLOWED_NAMESPACES": "sandbox",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
}


@pytest.mark.parametrize(("raw", "markers"), [("", ()), ("[]", ()), ("  ", ())])
def test_no_markers_configured(raw, markers):
    assert load_settings(BASE | {"GITOPS_MARKERS": raw}).gitops_markers == markers
    assert load_settings(BASE).gitops_markers == ()


def test_markers_are_parsed_in_order_without_duplicates():
    raw = '["app.kubernetes.io/instance", "example.com/gitops", "app.kubernetes.io/instance"]'
    settings = load_settings(BASE | {"GITOPS_MARKERS": raw})
    assert settings.gitops_markers == ("app.kubernetes.io/instance", "example.com/gitops")


@pytest.mark.parametrize(
    "raw",
    [
        "app.kubernetes.io/instance",
        '{"app.kubernetes.io/instance": true}',
        "[1]",
        '[""]',
        '["has space"]',
        '["Example.com/upper-prefix"]',
        '["a/b/c"]',
        '["' + "x" * 64 + '"]',
        "[null]",
    ],
)
def test_malformed_markers_fail_the_load(raw):
    with pytest.raises(ConfigError):
        load_settings(BASE | {"GITOPS_MARKERS": raw})


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("app.kubernetes.io/instance", True),
        ("argocd.argoproj.io/instance", True),
        ("managed", True),
        ("example.com/My_Key.1", True),
        ("-bad", False),
        ("/name", False),
        ("prefix/", False),
        (7, False),
    ],
)
def test_valid_marker(key, valid):
    assert valid_marker(key) is valid
