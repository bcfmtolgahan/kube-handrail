"""Kubernetes API client for an EKS cluster, authenticated with an IAM token."""

import base64
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import boto3
from botocore.signers import RequestSigner
from kubernetes import client as k8s

TOKEN_PREFIX = "k8s-aws-v1."
CLUSTER_ID_HEADER = "x-k8s-aws-id"
# EKS accepts a token for 15 minutes after signing; refresh well before that.
CLIENT_TTL_SECONDS = 600.0
PRESIGN_EXPIRES_SECONDS = 60


def get_token(cluster_name: str, session: boto3.Session, region: str) -> str:
    sts = session.client("sts", region_name=region)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"{sts.meta.endpoint_url}/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {CLUSTER_ID_HEADER: cluster_name},
            "context": {},
        },
        region_name=region,
        expires_in=PRESIGN_EXPIRES_SECONDS,
        operation_name="",
    )
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return TOKEN_PREFIX + encoded


def _write_ca(ca_data: str) -> str:
    with tempfile.NamedTemporaryFile("wb", suffix=".crt", delete=False) as handle:
        handle.write(base64.b64decode(ca_data))
        return handle.name


def build_api_client(cluster_name: str, session: boto3.Session | None = None) -> k8s.ApiClient:
    session = session or boto3.Session()
    region = session.region_name
    if not region:
        raise RuntimeError("AWS region is not configured")
    cluster = session.client("eks", region_name=region).describe_cluster(name=cluster_name)["cluster"]

    config = k8s.Configuration()
    config.host = cluster["endpoint"]
    config.ssl_ca_cert = _write_ca(cluster["certificateAuthority"]["data"])
    config.verify_ssl = True
    config.api_key = {"authorization": get_token(cluster_name, session, region)}
    config.api_key_prefix = {"authorization": "Bearer"}
    return k8s.ApiClient(config)


@dataclass
class _Entry:
    client: k8s.ApiClient
    created_at: float


class ClientCache:
    def __init__(
        self,
        factory: Callable[[str], k8s.ApiClient] = build_api_client,
        ttl: float = CLIENT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, cluster_name: str) -> k8s.ApiClient:
        with self._lock:
            now = self._clock()
            entry = self._entries.get(cluster_name)
            if entry is None or now - entry.created_at >= self._ttl:
                entry = _Entry(self._factory(cluster_name), now)
                self._entries[cluster_name] = entry
            return entry.client


_cache = ClientCache()


def api_client(cluster_name: str) -> k8s.ApiClient:
    """Cached client for `cluster_name`, rebuilt before the token expires."""
    return _cache.get(cluster_name)
