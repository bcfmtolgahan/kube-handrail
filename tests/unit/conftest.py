import boto3
import pytest

from handrail_common.plans import OBJECT_HISTORY_INDEX


def create_plans_table(name: str, region: str):
    """The plan table as defined in terraform/main.tf, including the history index."""
    return boto3.resource("dynamodb", region_name=region).create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "plan_id", "AttributeType": "S"},
            {"AttributeName": "object_key", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": OBJECT_HISTORY_INDEX,
                "KeySchema": [
                    {"AttributeName": "object_key", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def plans_table():
    return create_plans_table
