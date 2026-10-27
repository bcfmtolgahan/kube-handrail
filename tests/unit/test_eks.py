import base64

import boto3

from handrail_common.eks import ClientCache, get_token


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
