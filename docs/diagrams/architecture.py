"""Draws docs/images/architecture.png with the diagrams library (https://diagrams.mingrammer.com).

pip install -r docs/diagrams/requirements.txt   # also needs Graphviz
python docs/diagrams/architecture.py
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import DynamodbTable
from diagrams.aws.general import Client, User
from diagrams.aws.management import Cloudwatch, ParameterStore
from diagrams.aws.ml import Bedrock
from diagrams.aws.security import Cognito
from diagrams.aws.storage import S3
from diagrams.k8s.clusterconfig import HPA
from diagrams.k8s.compute import Deployment
from diagrams.k8s.controlplane import APIServer

OUT = Path(__file__).resolve().parents[1] / "images" / "architecture"

GRAPH = {
    "pad": "0.5",
    "nodesep": "0.7",
    "ranksep": "1.1",
    "splines": "spline",
    "fontname": "Helvetica",
    "fontsize": "24",
    "labelloc": "t",
}
NODE = {"fontname": "Helvetica", "fontsize": "13"}
CLUSTER = {"fontname": "Helvetica-Bold", "fontsize": "14", "margin": "20", "style": "rounded"}

MCP = {"color": "#2563eb", "penwidth": "2"}
WRITE = {"color": "#dc2626", "penwidth": "2.2"}
CONTROL = {"color": "#6b7280", "style": "dashed"}
AUDIT = {"color": "#059669", "penwidth": "1.6"}

with Diagram(
    "kube-handrail: plan, approve, apply, verify, audit",
    filename=str(OUT),
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH,
    node_attr=NODE,
):
    agent = Client("AWS DevOps Agent\nor any MCP client")
    human = User("Approver")

    with Cluster("AWS account", graph_attr=CLUSTER):
        with Cluster("AgentCore Gateway", graph_attr=CLUSTER):
            cognito = Cognito("Cognito OAuth")
            gateway = Bedrock("MCP endpoint")
            interceptor = Lambda("interceptor\nsigns the agent")

        with Cluster("Tools", graph_attr=CLUSTER):
            read = Lambda("k8s-read\nREAD_ONLY")
            write = Lambda("k8s-write\nMUTATIVE")

        approver = Lambda("approver\nIAM-signed URL")

        with Cluster("Plans and audit", graph_attr=CLUSTER):
            plans = DynamodbTable("single-use plans")
            audit = Lambda("audit")
            bucket = S3("write-once record\nObject Lock")

        with Cluster("Controls", graph_attr=CLUSTER):
            switches = ParameterStore("kill switches\nand freezes")
            verifier = Lambda("verifier\nevery minute")
            sentinel = Lambda("drift sentinel\nevery 5 minutes")
            watch = Cloudwatch("dashboard\nand alarms")

    with Cluster("Amazon EKS", graph_attr=CLUSTER):
        api = APIServer("API server\nRBAC + admission policy")
        deploy = Deployment("Deployment")
        hpa = HPA("HPA")

    agent >> Edge(label="MCP", **MCP) >> gateway
    agent >> Edge(label="token", **CONTROL) >> cognito
    gateway >> Edge(**MCP) >> interceptor
    interceptor >> Edge(**MCP) >> read
    interceptor >> Edge(**WRITE) >> write
    read >> Edge(**MCP) >> plans
    write >> Edge(**WRITE) >> api
    api >> Edge(**WRITE) >> deploy
    api >> Edge(**WRITE) >> hpa
    write >> Edge(**WRITE) >> plans
    human >> Edge(label="approve", **CONTROL) >> approver >> Edge(**CONTROL) >> plans
    plans >> Edge(label="stream", **AUDIT) >> audit >> Edge(**AUDIT) >> bucket
    switches >> Edge(**CONTROL) >> write
    sentinel >> Edge(**CONTROL) >> switches
    verifier >> Edge(**CONTROL) >> api
    sentinel >> Edge(**CONTROL) >> watch
