import base64
from types import SimpleNamespace

import boto3
import pytest
from kubernetes import client as k8s
from kubernetes.client.rest import RESTClientObject

from handrail_common import __version__
from handrail_common.eks import ClientCache, HandrailApiClient, get_token, user_agent
from handrail_common.observability import (
    bind_correlation_id,
    bind_plan_id,
    unbind_correlation_id,
    unbind_plan_id,
)


def test_token_format():
    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
    )
    token = get_token("demo", session, "us-east-1")
    assert token.startswith("k8s-aws-v1.")
    encoded = token.removeprefix("k8s-aws-v1.")
    assert "=" not in encoded
    url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    assert url.startswith("https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity")
    assert "x-k8s-aws-id" in url and "X-Amz-Signature=" in url


def test_client_cache_ttl():
    now = [0.0]
    built = []

    def factory(name):
        built.append(name)
        return object()

    cache = ClientCache(factory=factory, ttl=600, clock=lambda: now[0])
    first = cache.get("demo")
    now[0] = 599
    assert cache.get("demo") is first
    now[0] = 600
    assert cache.get("demo") is not first
    assert built == ["demo", "demo"]


PLAN_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def no_invocation():
    """Other tests call plan tools outside dispatch, which leaves their plan bound."""
    tokens = bind_plan_id(None), bind_correlation_id(None)
    yield
    unbind_plan_id(tokens[0])
    unbind_correlation_id(tokens[1])


def test_user_agent_names_the_version_plan_and_correlation_id():
    assert user_agent() == f"kube-handrail/{__version__}"
    plan_token, corr_token = bind_plan_id(PLAN_ID), bind_correlation_id("req-1")
    try:
        assert user_agent() == f"kube-handrail/{__version__} plan/{PLAN_ID} corr/req-1"
    finally:
        unbind_plan_id(plan_token)
        unbind_correlation_id(corr_token)
    corr_token = bind_correlation_id("req-2")
    try:
        assert user_agent() == f"kube-handrail/{__version__} corr/req-2"
    finally:
        unbind_correlation_id(corr_token)


def test_api_requests_carry_the_user_agent_of_the_invocation(monkeypatch):
    sent = []

    def fake_request(self, method, url, **kwargs):
        sent.append(kwargs["headers"]["User-Agent"])
        return SimpleNamespace(status=200, data=b"{}", getheaders=dict, getheader=lambda *a: None)

    monkeypatch.setattr(RESTClientObject, "request", fake_request)
    config = k8s.Configuration()
    config.host = "https://example.invalid"
    api = HandrailApiClient(config)
    apps = k8s.AppsV1Api(api)
    plan_token, corr_token = bind_plan_id(PLAN_ID), bind_correlation_id("req-1")
    try:
        apps.read_namespaced_deployment("web", "sandbox", _preload_content=False)
    finally:
        unbind_plan_id(plan_token)
        unbind_correlation_id(corr_token)
    apps.read_namespaced_deployment("web", "sandbox", _preload_content=False)
    assert sent == [
        f"kube-handrail/{__version__} plan/{PLAN_ID} corr/req-1",
        f"kube-handrail/{__version__}",
    ]
    # The shared client keeps no per-request value.
    assert api.user_agent.startswith("OpenAPI-Generator/")
