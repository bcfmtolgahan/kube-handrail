"""Gateway request interceptor: passes the verified caller to the tool Lambdas.

On by default (mcp/CONTRACT.md, Agent identity): the tool Lambdas identify the agent by
this client_id. The gateway runs it after it has validated the inbound JWT, so the token's
claims are trusted here without verifying the signature again. For tools/call it overwrites
the reserved argument CALLER_ARGUMENT with the token's client_id, signed with CALLER_KEY,
whatever the client sent under that name. The tool Lambdas trust only a value with a valid
signature, so a gateway that lost this interceptor cannot pass a client's own value off as a
verified caller. It keeps no state, so the gateway may retry it. The token itself is never
logged or forwarded.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
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


def signed(value: str, key: str) -> str:
    """`<value>.<hex HMAC-SHA256>`, as handrail_common.dispatch.pop_caller verifies it. Without
    a key the value goes unsigned, and the tool Lambdas refuse every write."""
    if not key:
        return value
    return f"{value}.{hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()}"


def transform(body: Any, headers: Mapping[str, Any], key: str = "") -> Any:
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return body
    params = body.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    # Unconditional overwrite: a value the client sent under this name never survives.
    arguments[CALLER_ARGUMENT] = signed(caller(headers), key)
    return {**body, "params": {**params, "arguments": arguments}}


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    request = (
        (event.get("mcp") or {}).get("gatewayRequest") or {} if isinstance(event, dict) else {}
    )
    headers = request.get("headers") or {}
    headers = headers if isinstance(headers, Mapping) else {}
    body = transform(request.get("body"), headers, os.environ.get("CALLER_KEY", ""))
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}
