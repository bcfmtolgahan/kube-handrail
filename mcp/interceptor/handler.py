"""Gateway request interceptor: passes the verified caller to the tool Lambdas.

Experimental (Milestone 1, item 6). The gateway runs it after it has validated the inbound
JWT, so the token's claims are trusted here without verifying the signature again. For
tools/call it overwrites the reserved argument CALLER_ARGUMENT with the token's client_id,
whatever the client sent under that name. The token itself is never logged or forwarded.
"""

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Must match CALLER_ARGUMENT in handrail_common.dispatch.
CALLER_ARGUMENT = "_kube_handrail_caller"
UNKNOWN_CALLER = "unknown"


def _claims(headers: Mapping[str, Any]) -> dict[str, Any]:
    authorization = next(
        (v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"), ""
    )
    token = authorization.split(" ")[-1] if isinstance(authorization, str) else ""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def caller(headers: Mapping[str, Any]) -> str:
    """The OAuth client of the request; Cognito client-credentials tokens carry client_id."""
    value = _claims(headers).get("client_id")
    return value if isinstance(value, str) and value else UNKNOWN_CALLER


def transform(body: Any, headers: Mapping[str, Any]) -> Any:
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return body
    params = body.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    # Unconditional overwrite: a value the client sent under this name never survives.
    arguments[CALLER_ARGUMENT] = caller(headers)
    return {**body, "params": {**params, "arguments": arguments}}


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    request = (event.get("mcp") or {}).get("gatewayRequest") or {} if isinstance(event, dict) else {}
    headers = request.get("headers") or {}
    body = transform(request.get("body"), headers if isinstance(headers, Mapping) else {})
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}
