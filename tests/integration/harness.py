"""The integration harness: the Lambda handlers in process, against a kind cluster and moto.

Nothing here runs at import time beyond loading the handler modules, so collecting
tests/integration without a cluster stays cheap and side-effect free (conftest.py skips the
tests). See tests/integration/README.md.
"""

import base64
import importlib.util
import io
import json
import os
import subprocess
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import boto3
from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from handrail_common.freezes import Freeze, dump_freezes
from handrail_common.plans import OBJECT_HISTORY_INDEX, VERIFY_DUE_INDEX

REPO = Path(__file__).resolve().parents[2]
NAMESPACE = "kube-handrail-sandbox"
REGION = "us-east-1"
TABLE = "kh-int-plans"
BUCKET = "kh-int-audit"
KILL_SWITCH = "/kh-int/remediation-enabled"
AGENT_SWITCHES = "/kh-int/agents/"
FREEZES = "/kh-int/freezes"
WRITE_FUNCTION = "kh-int-k8s-write"
CALLER_KEY = "kh-int-caller-key"
CLIENT_A, AGENT_A = "kh-int-client-a", "smoke_test"
CLIENT_B, AGENT_B = "kh-int-client-b", "e2e_peer"
AGENT_BUDGET_B = 2
# 123456789012 is the account id the AWS documentation uses in examples; nothing real.
APPROVER_ROLE = "arn:aws:iam::123456789012:role/kh-int-approver"
APPROVER = "arn:aws:sts::123456789012:assumed-role/kh-int-approver/operator"
NOT_APPROVER = "arn:aws:sts::123456789012:assumed-role/kh-int-other/operator"
LAMBDA_ROLE = "arn:aws:iam::123456789012:role/kh-int-lambda"
LAMBDA_SESSION = "arn:aws:sts::123456789012:assumed-role/kh-int-lambda/kh-int-k8s-write"
# The impersonated groups are those of the two EKS access entries (k8s/sandbox/rbac.yaml).
READER_GROUP = "kube-handrail-reader"
WRITER_GROUP = "kube-handrail-writer"
# The bounds of the Lambdas and, through the deploy sed, of the admission policy.
MIN_REPLICAS, MAX_REPLICAS = 0, 5
# Preloaded on every kind node, so fixtures start without pulling anything.
IMAGE = "registry.k8s.io/pause:3.10"
QUOTA = "kh-int-besteffort"

BASE_ENV = {
    "CLUSTER_NAME": "kh-int",
    "PLANS_TABLE": TABLE,
    "ALLOWED_NAMESPACES": NAMESPACE,
    "MIN_REPLICAS": str(MIN_REPLICAS),
    "MAX_REPLICAS": str(MAX_REPLICAS),
    "REMEDIATION_ENABLED_PARAM": KILL_SWITCH,
    "AGENT_KILL_SWITCH_PREFIX": AGENT_SWITCHES,
    "FREEZES_PARAM": FREEZES,
    "AGENTS": json.dumps(
        {
            CLIENT_A: {"name": AGENT_A, "hourly_apply_budget": 30},
            CLIENT_B: {"name": AGENT_B, "hourly_apply_budget": AGENT_BUDGET_B},
        }
    ),
    "CALLER_KEY": CALLER_KEY,
    "APPROVAL_MODE": "client",
    # Most cases apply more than once to an object; the cooldown case sets its own.
    "APPLY_COOLDOWN_SECONDS": "0",
    "APPROVAL_URL": "https://approval.invalid/",
    "WRITE_FUNCTION_NAME": WRITE_FUNCTION,
    "APPROVER_PRINCIPALS": APPROVER_ROLE,
    "DENIED_PRINCIPALS": LAMBDA_ROLE,
    "AUDIT_BUCKET": BUCKET,
    "AWS_REGION": REGION,
    "AWS_DEFAULT_REGION": REGION,
    "METRIC_NAMESPACE": "kube-handrail/integration",
}


def load_handler(name: str) -> ModuleType:
    """mcp/<name>/handler.py by path: every Lambda's module is called `handler`."""
    spec = importlib.util.spec_from_file_location(
        f"kh_int_{name}_handler", REPO / "mcp" / name / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"kh_int_{name}_script", REPO / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Kubernetes ------------------------------------------------------------------------


def require_kind_kubeconfig() -> str:
    """The KUBECONFIG of a kind cluster. Never the default kubeconfig, and never a context
    that kind did not create, so the harness cannot reach a real cluster."""
    path = os.environ.get("KUBECONFIG", "")
    if not path or os.pathsep in path or not Path(path).is_file():
        raise RuntimeError("set KUBECONFIG to the kubeconfig file of a kind cluster")
    _, active = k8s_config.list_kube_config_contexts(config_file=path)
    if not active["name"].startswith("kind-"):
        raise RuntimeError(f"refusing context {active['name']!r}: not a kind cluster")
    server = admin_configuration(path).host
    if urllib.parse.urlsplit(server).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"refusing API server {server!r}: a kind cluster listens on localhost")
    return path


def wait_out_hour_boundary(margin: int = 90) -> None:
    """Budgets count applies per clock hour (on the Lambdas' real clock, which the read
    Lambda does not let a test move). A case that needs one hour sleeps past the boundary
    when fewer than `margin` seconds of the hour are left."""
    left = 3600 - int(time.time()) % 3600
    if left < margin:
        time.sleep(left + 1)


def admin_configuration(kubeconfig: str) -> k8s.Configuration:
    configuration = k8s.Configuration()
    k8s_config.load_kube_config(config_file=kubeconfig, client_configuration=configuration)
    return configuration


def impersonating_client(configuration: k8s.Configuration, user: str, group: str) -> k8s.ApiClient:
    """A client whose requests the API server authorizes as `user` in `group` only: the
    same identity the EKS access entry gives the Lambda's role. The Python client ignores
    `as` and `as-groups` in a kubeconfig, so the headers are set here."""
    api = k8s.ApiClient(configuration)
    api.set_default_header("Impersonate-User", user)
    api.set_default_header("Impersonate-Group", group)
    return api


def write_impersonating_kubeconfig(admin: str, path: Path, user: str, group: str) -> Path:
    """A kubeconfig for kubectl that acts as the reader or the writer, for debugging."""
    out = subprocess.run(
        ["kubectl", "config", "view", "--raw", "--minify", "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": admin},
    ).stdout
    config = json.loads(out)
    for entry in config["users"]:
        entry["user"]["as"] = user
        entry["user"]["as-groups"] = [group]
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """kubectl as the cluster admin, in the sandbox namespace unless args say otherwise."""
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def admission_policy(min_replicas: int, max_replicas: int) -> str:
    """k8s/sandbox/admission-policy.yaml with its bounds set by the sed in docs/deploy.md."""
    return subprocess.run(
        [
            "sed",
            "-e",
            f's/"[0-9]*" # min_replicas/"{min_replicas}" # min_replicas/',
            "-e",
            f's/"[0-9]*" # max_replicas/"{max_replicas}" # max_replicas/',
            "-e",
            f"s/kube-handrail-sandbox/{NAMESPACE}/",
            str(REPO / "k8s" / "sandbox" / "admission-policy.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def wait_until(what: str, condition: Callable[[], Any], timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.5)


SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 65535,
    "runAsGroup": 65535,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}


def deployment_manifest(
    name: str,
    replicas: int = 1,
    *,
    labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
    best_effort: bool = False,
    env: Mapping[str, str] | None = None,
    image: str = IMAGE,
    revision_history_limit: int | None = None,
    surge_all: bool = False,
) -> dict[str, Any]:
    """A Deployment the namespace's restricted Pod Security admits. Without best_effort it
    has requests, so the BestEffort quota never applies to it."""
    selector = {"app.kubernetes.io/name": name}
    container: dict[str, Any] = {
        "name": "main",
        "image": image,
        "securityContext": CONTAINER_SECURITY,
    }
    if not best_effort:
        container["resources"] = {
            "requests": {"cpu": "1m", "memory": "4Mi"},
            "limits": {"cpu": "10m", "memory": "16Mi"},
        }
    if env:
        container["env"] = [{"name": k, "value": v} for k, v in env.items()]
    spec: dict[str, Any] = {
        "replicas": replicas,
        "selector": {"matchLabels": selector},
        "template": {
            "metadata": {"labels": selector},
            "spec": {
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": 0,
                "securityContext": SECURITY_CONTEXT,
                "containers": [container],
            },
        },
    }
    if revision_history_limit is not None:
        spec["revisionHistoryLimit"] = revision_history_limit
    if surge_all:
        # Every new pod at once: a rollout takes one pod start, not one per replica.
        spec["strategy"] = {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": "100%", "maxUnavailable": 0},
        }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {**selector, **(labels or {})},
            "annotations": dict(annotations or {}),
        },
        "spec": spec,
    }


def best_effort_quota() -> dict[str, Any]:
    """Admits one BestEffort pod in the namespace: scaling a BestEffort fixture from 1 to 2
    leaves the second pod refused (ReplicaFailure), as in the live end-to-end test."""
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": QUOTA, "namespace": NAMESPACE},
        "spec": {"hard": {"pods": "1"}, "scopes": ["BestEffort"]},
    }


# --- AWS (moto) ------------------------------------------------------------------------


def create_plans_table() -> Any:
    """The plan table as terraform/main.tf defines it: key, both indexes and the stream the
    audit Lambda reads (NEW_AND_OLD_IMAGES)."""
    return boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "plan_id", "AttributeType": "S"},
            {"AttributeName": "object_key", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
            {"AttributeName": "verify_queue", "AttributeType": "S"},
            {"AttributeName": "verify_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": OBJECT_HISTORY_INDEX,
                "KeySchema": [
                    {"AttributeName": "object_key", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": VERIFY_DUE_INDEX,
                "KeySchema": [
                    {"AttributeName": "verify_queue", "KeyType": "HASH"},
                    {"AttributeName": "verify_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
    )


def create_aws_resources() -> dict[str, Any]:
    """Everything Terraform creates that the handlers touch, in moto."""
    table = create_plans_table()
    boto3.client("dynamodb", region_name=REGION).update_time_to_live(
        TableName=TABLE,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "retain_until"},
    )
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name=KILL_SWITCH, Value="true", Type="String")
    for agent in (AGENT_A, AGENT_B):
        ssm.put_parameter(Name=f"{AGENT_SWITCHES}{agent}/enabled", Value="true", Type="String")
    ssm.put_parameter(Name=FREEZES, Value="[]", Type="String")
    topic = boto3.client("sns", region_name=REGION).create_topic(Name="kh-int-approvals")
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
    s3.put_object_lock_configuration(
        Bucket=BUCKET,
        ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}},
        },
    )
    return {"table": table, "topic_arn": topic["TopicArn"], "ssm": ssm, "s3": s3}


def fake_jwt(client_id: str) -> str:
    """A token shaped like a Cognito client-credentials access token. The gateway validated
    the real one before the interceptor runs, so the interceptor reads only its claims."""

    def part(value: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'client_id': client_id})}.signature"


def lambda_context(custom: Mapping[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom=dict(custom)),
        aws_request_id=str(uuid.uuid4()),
    )


class FakeLambda:
    """lambda.invoke of the write Lambda, as the approver and the verifier call it, run in
    process with the client context the Lambda service would pass."""

    def __init__(self, write_handler: ModuleType) -> None:
        self._write = write_handler
        self.calls: list[dict[str, Any]] = []

    def invoke(self, FunctionName, InvocationType, ClientContext, Payload):  # noqa: N803
        assert FunctionName == WRITE_FUNCTION
        assert InvocationType == "RequestResponse"
        custom = json.loads(base64.b64decode(ClientContext))["custom"]
        event = json.loads(Payload)
        self.calls.append({"custom": custom, "event": event})
        envelope = self._write.lambda_handler(event, lambda_context(custom))
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(envelope).encode())}


def stream_records(stream_arn: str, after: str | None = None) -> list[dict[str, Any]]:
    """The plan table's stream records, as the event source mapping hands them to the audit
    Lambda: the creation time in epoch seconds, binary values as base64 text, and no
    OldImage on an INSERT."""
    streams = boto3.client("dynamodbstreams", region_name=REGION)
    records: list[dict[str, Any]] = []
    for shard in streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"]:
        iterator = streams.get_shard_iterator(
            StreamArn=stream_arn, ShardId=shard["ShardId"], ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]
        while iterator:
            page = streams.get_records(ShardIterator=iterator)
            records.extend(page["Records"])
            if not page["Records"]:
                break
            iterator = page.get("NextShardIterator")
    out = []
    for record in records:
        stream = dict(record["dynamodb"])
        stream["ApproximateCreationDateTime"] = int(
            stream["ApproximateCreationDateTime"].timestamp()
        )
        for image in ("OldImage", "NewImage"):
            if not stream.get(image):
                stream.pop(image, None)
            else:
                stream[image] = _lambda_wire(stream[image])
        out.append({**record, "dynamodb": stream})
    if after is not None:
        out = [r for r in out if int(r["dynamodb"]["SequenceNumber"]) > int(after)]
    return out


def _lambda_wire(value: Any) -> Any:
    """boto3 decodes stream binary to bytes; Lambda delivers it as base64 text."""
    if isinstance(value, Mapping):
        return {
            k: (
                base64.b64encode(v).decode()
                if k == "B" and isinstance(v, bytes)
                else _lambda_wire(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_lambda_wire(v) for v in value]
    return value


# --- The handlers, as the gateway, the approval URL and the schedules call them ---------


class Harness:
    """One test's view: the handlers, moto resources and the cluster clients."""

    def __init__(
        self,
        handlers: Mapping[str, ModuleType],
        aws: Mapping[str, Any],
        reader: k8s.ApiClient,
        writer: k8s.ApiClient,
        admin: k8s.ApiClient,
        set_env: Callable[[Mapping[str, str]], None],
    ) -> None:
        self.read = handlers["k8s_read"]
        self.write = handlers["k8s_write"]
        self.approver_handler = handlers["approver"]
        self.verifier_handler = handlers["verifier"]
        self.audit_handler = handlers["audit"]
        self.interceptor = handlers["interceptor"]
        self.aws = aws
        self.table = aws["table"]
        self.ssm = aws["ssm"]
        self.s3 = aws["s3"]
        self.reader = reader
        self.writer = writer
        self.apps = k8s.AppsV1Api(admin)
        self.core = k8s.CoreV1Api(admin)
        self._set_env = set_env
        # Seconds added to the write Lambda's clock (Runtime.clock); 0 is the real time.
        self.write_offset = 0
        self.lambda_client = FakeLambda(self.write)
        self.created: list[tuple[str, str]] = []
        self.configure()

    # Configuration

    def configure(self, **overrides: str) -> None:
        """Set the Lambda environment (BASE_ENV plus overrides) and start every handler as a
        fresh execution environment would."""
        self._set_env({**BASE_ENV, "APPROVAL_TOPIC_ARN": self.aws["topic_arn"], **overrides})
        for cached in (self.read._settings, self.read._store, self.read._sns, self.read._ssm):
            cached.cache_clear()
        self.write._runtime = None
        self.approver_handler._runtime = None
        self.verifier_handler._runtime = None
        self.audit_handler._settings = None
        self.audit_handler._s3 = None
        runtime = self.write.get_runtime()
        # The two seams of the write Lambda's runtime: its Kubernetes client, here the
        # impersonated writer (the read Lambda gets the reader through eks.ClientCache),
        # and its clock.
        runtime.apps = lambda: k8s.AppsV1Api(self.writer)
        runtime.clock = lambda: time.time() + self.write_offset

    def write_now(self) -> int:
        return int(time.time() + self.write_offset)

    # Gateway

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        client: str = CLIENT_A,
        interceptor: bool = True,
    ) -> dict[str, Any]:
        """One tools/call through the gateway: the request interceptor signs the caller from
        the token's client_id, and the Lambda gets the tool name in its client context."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        if interceptor:
            event = {
                "mcp": {
                    "gatewayRequest": {
                        "headers": {"Authorization": f"Bearer {fake_jwt(client)}"},
                        "body": body,
                    }
                }
            }
            result = self.interceptor.lambda_handler(event, None)
            body = result["mcp"]["transformedGatewayRequest"]["body"]
        # JSON on the wire between the gateway and the Lambda.
        arguments = json.loads(json.dumps(body["params"]["arguments"]))
        target = tool.split("___", 1)[0]
        handler = {"k8sr": self.read, "k8sw": self.write}[target]
        context = lambda_context(
            {"bedrockAgentCoreToolName": tool, "bedrockAgentCoreAwsRequestId": str(uuid.uuid4())}
        )
        return handler.lambda_handler(arguments, context)

    def plan(self, target: Mapping[str, Any], replicas: int, client: str = CLIENT_A, **extra):
        envelope = self.call(
            "k8sr___plan_scale_deployment", {**target, "replicas": replicas, **extra}, client
        )
        assert envelope["ok"], envelope
        return envelope["data"]["plan_id"]

    def apply(
        self,
        target: Mapping[str, Any],
        plan_id: str,
        replicas: int,
        client: str = CLIENT_A,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call(
            "k8sw___apply_scale_deployment",
            {"plan_id": plan_id, **target, "replicas": replicas, **extra},
            client,
        )

    # Approval URL (Function URL, AWS_IAM auth)

    def approver(
        self,
        method: str,
        caller: str | None = APPROVER,
        plan_id: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        event: dict[str, Any] = {"requestContext": {"http": {"method": method}}}
        if caller is not None:
            # What the Function URL reports for a request signed with SigV4 by `caller`.
            event["requestContext"]["authorizer"] = {"iam": {"userArn": caller}}
        if plan_id is not None and method == "GET":
            event["queryStringParameters"] = {"plan_id": plan_id}
        if body is not None:
            event["body"] = json.dumps(body)
        self.approver_runtime()
        response = self.approver_handler.lambda_handler(event, lambda_context({}))
        return response["statusCode"], json.loads(response["body"])

    def approver_runtime(self) -> Any:
        module = self.approver_handler
        if module._runtime is None:
            settings = module.load_approver_settings()
            module._runtime = module.Runtime(
                settings=settings,
                store=module.PlanStore.from_name(settings.plans_table, settings.region),
                lambda_client=self.lambda_client,
            )
        return module._runtime

    # Verifier (scheduled)

    def run_verifier(self) -> dict[str, Any]:
        module = self.verifier_handler
        if module._runtime is None:
            settings = module.load_verifier_settings()
            module._runtime = module.Runtime(
                settings=settings,
                store=module.PlanStore.from_name(settings.plans_table, settings.region),
                lambda_client=self.lambda_client,
                clock=lambda: time.time() + self.write_offset,
            )
        return module.lambda_handler({}, lambda_context({}))

    def make_due(self, plan_id: str) -> None:
        """Move the plan's verify_at to one second ago, as if its window had passed."""
        self.table.update_item(
            Key={"plan_id": plan_id},
            UpdateExpression="SET verify_at = :t",
            ExpressionAttributeValues={":t": self.write_now() - 1},
        )

    # Audit (DynamoDB stream event source mapping)

    def run_audit(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self.audit_handler.lambda_handler({"Records": records}, lambda_context({}))

    def stream_arn(self) -> str:
        return self.table.latest_stream_arn

    # State

    def record(self, plan_id: str) -> dict[str, Any]:
        return self.table.get_item(Key={"plan_id": plan_id}, ConsistentRead=True).get("Item") or {}

    def set_param(self, name: str, value: str) -> None:
        self.ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)

    def set_freezes(self, *freezes: Freeze) -> None:
        self.set_param(FREEZES, dump_freezes(freezes))

    # Cluster

    def create(self, manifest: dict[str, Any]) -> dict[str, str]:
        name = manifest["metadata"]["name"]
        self.created.append(("deployment", name))
        self.apps.create_namespaced_deployment(NAMESPACE, manifest)
        return {"namespace": NAMESPACE, "name": name}

    def deployment(self, name: str) -> Any:
        return self.apps.read_namespaced_deployment(name, NAMESPACE)

    def annotation(self, name: str, key: str) -> str | None:
        return (self.deployment(name).metadata.annotations or {}).get(key)

    def patch(self, name: str, body: Mapping[str, Any]) -> Any:
        return self.apps.patch_namespaced_deployment(name, NAMESPACE, body)

    def scale_by_hand(self, name: str, replicas: int) -> None:
        self.patch(name, {"spec": {"replicas": replicas}})

    def wait_observed(self, name: str, timeout: float = 60) -> Any:
        def observed():
            d = self.deployment(name)
            return d if (d.status.observed_generation or 0) >= d.metadata.generation else None

        return wait_until(f"{name} to be observed", observed, timeout)

    def wait_available(self, name: str, replicas: int, timeout: float = 120) -> Any:
        def available():
            d = self.deployment(name)
            s = d.status
            done = (
                (s.observed_generation or 0) >= d.metadata.generation
                and (s.available_replicas or 0) == replicas
                and (s.updated_replicas or 0) == replicas
                and (s.replicas or 0) == replicas
            )
            return d if done else None

        return wait_until(f"{name} to have {replicas} available", available, timeout)

    def cleanup(self) -> None:
        for kind, name in reversed(self.created):
            try:
                if kind == "deployment":
                    self.apps.delete_namespaced_deployment(
                        name, NAMESPACE, propagation_policy="Background", grace_period_seconds=0
                    )
                else:
                    kubectl("delete", kind, name, "--ignore-not-found", "--wait=false")
            except ApiException as exc:
                if exc.status != 404:
                    raise


def code(envelope: Mapping[str, Any]) -> str | None:
    return (envelope.get("error") or {}).get("code")


def details(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return (envelope.get("error") or {}).get("details") or {}


def unique(prefix: str) -> str:
    """A fresh Deployment name: a deleted fixture can linger while it terminates."""
    return f"{prefix}-{uuid.uuid4().hex[:6]}"
