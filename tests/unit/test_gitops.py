from types import SimpleNamespace

import pytest

from handrail_common.gitops import is_gitops_managed


def deployment(annotations=None, labels=None):
    return SimpleNamespace(metadata=SimpleNamespace(annotations=annotations, labels=labels))


def test_plain_deployment_not_managed():
    assert not is_gitops_managed(deployment({"note": "x"}, {"app": "web"}))
    assert not is_gitops_managed(deployment())


def test_argocd_annotation():
    assert is_gitops_managed(deployment({"argocd.argoproj.io/tracking-id": "app:apps/Deployment:ns/web"}))


def test_instance_label():
    assert is_gitops_managed(deployment(labels={"app.kubernetes.io/instance": "web"}))


def test_dict_form():
    assert is_gitops_managed({"metadata": {"labels": {"app.kubernetes.io/instance": "web"}}})
    assert not is_gitops_managed({"metadata": {"labels": {"app": "web"}}})


@pytest.mark.parametrize(
    "obj",
    [None, deployment(annotations="bad"), deployment(labels=["app.kubernetes.io/instance"])],
)
def test_fails_closed(obj):
    assert is_gitops_managed(obj)
