"""Untrusted text in tool output (mcp/CONTRACT.md, Untrusted output).

Text that cluster writers control is cleaned and wrapped before an agent sees it. The
wrapper is a hint to the model, not a security boundary: the boundary is that read tools
change nothing and every change needs an approval pinned to its arguments.
"""

import json
import re
import unicodedata
from typing import Any

FIELD_MAX_CHARS = 1024
RESPONSE_MAX_BYTES = 32768
OPEN = "<untrusted>"
CLOSE = "</untrusted>"

_KEPT_CONTROLS = frozenset("\n\t")
# Cc: C0 and C1 controls. Cf: format characters (bidi controls, zero-width characters,
# BOM, soft hyphen, tag characters). Cs: lone surrogates, which JSON escapes can produce.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

_CREDENTIAL_KEY = r"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key)"
# (pattern, replacement), applied in order. Replacements never match any pattern again,
# except the credential value, which is replaced by the same marker: redaction is idempotent.
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # An unterminated block (for example cut by the writer) is redacted to the end.
        re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[redacted:private_key]",
    ),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[redacted:jwt]"),
    (
        re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        r"\1 [redacted:bearer]",
    ),
    (re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"), "[redacted:aws_access_key_id]"),
    (
        re.compile(
            r"((?i:aws_?secret_?(?:access_?)?key|secret_?access_?key)\s*[\"']?\s*[:=]\s*[\"']?)"
            r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"
        ),
        r"\1[redacted:aws_secret_access_key]",
    ),
    (
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        "[redacted:github_token]",
    ),
    (re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"), "[redacted:slack_token]"),
    (
        # `key: value` only without a space after the colon, so prose such as
        # "failed to get token: context deadline exceeded" keeps its meaning.
        re.compile(
            rf"(?<![A-Za-z0-9_.-])({_CREDENTIAL_KEY})(?![A-Za-z0-9_.-])"
            r"(\s*=\s*|:(?=[^\s:])|[\"']\s*:\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)",
            re.IGNORECASE,
        ),
        r"\1\2[redacted:credential]",
    ),
)

_DELIMITER = re.compile(r"<(?=\s*/?\s*untrusted\b)", re.IGNORECASE)


def strip_controls(text: str) -> str:
    """Remove control, format and surrogate characters; keep newlines and tabs."""
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _REMOVED_CATEGORIES
    )


def redact_secrets(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def neutralize_delimiters(text: str) -> str:
    """Escape anything that reads as an untrusted tag, so text cannot close its wrapper."""
    return _DELIMITER.sub("&lt;", text)


def truncate(text: str, limit: int = FIELD_MAX_CHARS) -> tuple[str, bool]:
    """At most `limit` characters, marker included, so truncating again changes nothing.
    `limit` must be longer than a marker (about 30 characters)."""
    if len(text) <= limit:
        return text, False
    # The marker for the most characters that can be removed is at least as long as the
    # one actually used, so the result never exceeds the limit.
    keep = max(0, limit - len(_marker(len(text))))
    return text[:keep] + _marker(len(text) - keep), True


def _marker(removed: int) -> str:
    return f"[truncated {removed} chars]"


def clean(text: str, limit: int = FIELD_MAX_CHARS) -> tuple[str, bool]:
    """Steps 1-4 of the contract: controls, secrets, delimiters, field limit. Idempotent.

    Controls go first so a zero-width character cannot split a secret; secrets before the
    limit so a secret cut in half is still recognised.
    """
    return truncate(neutralize_delimiters(redact_secrets(strip_controls(text))), limit)


def untrusted_text(value: Any, limit: int = FIELD_MAX_CHARS) -> tuple[str | None, bool]:
    """Cleaned and wrapped text, and whether it was truncated. None stays None."""
    if value is None:
        return None, False
    text, truncated = clean(value if isinstance(value, str) else str(value), limit)
    return f"{OPEN}{text}{CLOSE}", truncated


def serialized_size(data: Any) -> int:
    # json.dumps escapes non-ASCII characters, so this is never less than the UTF-8 size.
    return len(json.dumps(data).encode())


def fit_list(
    data: dict[str, Any], key: str, max_bytes: int = RESPONSE_MAX_BYTES
) -> tuple[dict[str, Any], bool]:
    """Drop entries from the end of `data[key]` until `data` serializes within `max_bytes`."""
    items = list(data[key])
    while items and serialized_size({**data, key: items}) > max_bytes:
        items.pop()
    return {**data, key: items}, len(items) < len(data[key])
