"""Fixtures of the integration suite (tests/integration/README.md).

Skipped unless KH_INTEGRATION is set: the suite needs a kind cluster (KUBECONFIG naming a
kind context) and touches nothing else. AWS is moto only, with fake credentials.
"""

import json
import os
from pathlib import Path

import harness
import pytest
from kubernetes import client as k8s
from moto import mock_aws

from handrail_common import eks

HERE = Path(__file__).resolve().parent
ENABLED = os.environ.get("KH_INTEGRATION", "").lower() in ("1", "true", "yes")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="integration suite: set KH_INTEGRATION=1 and a kind KUBECONFIG")
    for item in items:
        if HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.integration)
            if not ENABLED:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def kubeconfig():
    return harness.require_kind_kubeconfig()


@pytest.fixture(scope="session")
def cluster(kubeconfig, tmp_path_factory):
    """The sandbox manifests as docs/deploy.md applies them, the admission policy with the
    Lambdas' bounds and its binding switched to Deny, and the BestEffort quota of the
    verification cases. Returns the admin configuration."""
    os.environ["KUBECONFIG"] = kubeconfig
    sandbox = harness.REPO / "k8s" / "sandbox"
    for name in ("namespace.yaml", "rbac.yaml", "web-deployment.yaml"):
        harness.kubectl("apply", "-f", str(sandbox / name))
    harness.kubectl(
        "apply",
        "-f",
        "-",
        stdin=harness.admission_policy(harness.MIN_REPLICAS, harness.MAX_REPLICAS),
    )
    harness.kubectl(
        "patch",
        "validatingadmissionpolicybinding",
        "kube-handrail-writer-bounds",
        "--type",
        "merge",
        "-p",
        '{"spec":{"validationActions":["Deny"]}}',
    )
    harness.kubectl("apply", "-f", "-", stdin=json.dumps(harness.best_effort_quota()))
    configuration = harness.admin_configuration(kubeconfig)
    directory = tmp_path_factory.mktemp("kubeconfigs")
    harness.write_impersonating_kubeconfig(
        kubeconfig, directory / "reader", "kh-int-reader", harness.READER_GROUP
    )
    harness.write_impersonating_kubeconfig(
        kubeconfig, directory / "writer", "kh-int-writer", harness.WRITER_GROUP
    )
    return configuration


@pytest.fixture(scope="session")
def clients(cluster):
    return {
        "admin": k8s.ApiClient(cluster),
        "reader": harness.impersonating_client(cluster, "kh-int-reader", harness.READER_GROUP),
        "writer": harness.impersonating_client(cluster, "kh-int-writer", harness.WRITER_GROUP),
    }


@pytest.fixture(scope="session")
def handlers():
    return {
        name: harness.load_handler(name)
        for name in ("k8s_read", "k8s_write", "approver", "verifier", "audit", "interceptor")
    }


@pytest.fixture
def kh(clients, handlers, monkeypatch):
    """A fresh moto account and fresh Lambda execution environments for one test."""
    for key in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", harness.REGION)
    # The read Lambda's only Kubernetes client: the impersonated reader.
    monkeypatch.setattr(eks, "_cache", eks.ClientCache(factory=lambda _name: clients["reader"]))

    # Optional settings the tests override: a value from the outer environment must not leak in.
    previous: set[str] = {
        "APPLY_COOLDOWN_SECONDS",
        "APPROVAL_MODE",
        "GITOPS_MARKERS",
        "GLOBAL_HOURLY_APPLY_BUDGET",
        "OBJECT_HOURLY_APPLY_BUDGET",
    }

    def set_env(env):
        # An override of an earlier configure() that the new call leaves out is removed.
        for key in previous - env.keys():
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        previous.clear()
        previous.update(env)

    with mock_aws():
        aws = harness.create_aws_resources()
        h = harness.Harness(
            handlers, aws, clients["reader"], clients["writer"], clients["admin"], set_env
        )
        try:
            yield h
        finally:
            h.cleanup()
