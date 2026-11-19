"""Secret redaction and untrusted remote text hygiene."""

import re
import unicodedata
from typing import Any

_SECRETS: set[str] = set()

_MASKED_NAMES = (
    r"x-api-key|api_?key|eiq_api_key|everest_api_key|webhook_secret"
    r"|cf[-_]access[-_]client[-_]secret|http_bearer_token|authorization|password|secret"
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\beiq_[A-Za-z0-9_-]{8,}\b"), "eiq_***"),
    (re.compile(r"\bwhsec_[A-Za-z0-9_-]{8,}\b"), "whsec_***"),
    (
        re.compile(rf"(?i)(['\"]?(?:{_MASKED_NAMES})['\"]?\s*[:=]\s*(['\"]))(?:(?!\2).)+"),
        r"\1***",
    ),
    (
        re.compile(
            rf"(?i)(['\"]?(?:{_MASKED_NAMES})['\"]?\s*[:=]\s*(?:(?:bearer|basic)\s+)?)"
            + r"[^'\"\s,}]+"
        ),
        r"\1***",
    ),
    (re.compile(r"(?i)(\bbearer\s+)[\w+/=.-]+"), r"\1***"),
)
_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]")


def register_secret(value: str | None) -> None:
    """Remember a literal secret so it is masked wherever it appears in logs or errors."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    """Mask registered secrets and common API key / auth header patterns."""
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean_text(text: str | None, limit: int | None = None) -> str | None:
    """Strip control and bidirectional override characters from untrusted remote strings."""
    if text is None:
        return None
    cleaned = _CONTROL.sub("", unicodedata.normalize("NFC", text)).strip()
    if limit is not None and len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def clean_label(text: Any, limit: int = 200) -> str | None:
    """Clean one untrusted line for human-facing confirmation prompts."""
    if text is None:
        return None
    cleaned = clean_text(str(text), limit)
    return " ".join(cleaned.split()) if cleaned else cleaned
