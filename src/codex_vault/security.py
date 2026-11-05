from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from .models import Snapshot

_KEY = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|(?:access|refresh|session|id)[_-]?token|authorization|cookie|private[_-]?key)",
    re.I,
)
_PATTERNS = [
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(
        r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(
        r"(?im)\b[\w-]*(?:password|passwd|secret|api[_-]?key|(?:access|refresh|session|id)[_-]?token)[\w-]*[\"']?\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"(?i)(?:https?|postgres(?:ql)?|mysql)://[^\s/@:]+:[^\s/@]+@"),
]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _KEY.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in _PATTERNS:
            value = pattern.sub("[REDACTED]", value)
    return value


def sanitize(snapshot: Snapshot) -> Snapshot:
    data = snapshot.model_dump()
    data["header"]["repository"] = canonical_repo(snapshot.header.repository)
    return Snapshot.model_validate(redact(data))


def canonical_repo(remote: str | None) -> str | None:
    """Only a host/path identity is retained; never userinfo or query credentials."""
    if not remote:
        return None
    remote = remote.strip()
    if "://" not in remote:
        match = re.fullmatch(r"(?:[^/@:]+@)?([\w.-]+)[:/](.+)", remote)
        if not match:
            return None
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return None
        if parsed.scheme not in {"https", "http", "ssh", "git"} or not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path
    path = path.strip("/").removesuffix(".git")
    if not path or ".." in path.split("/") or "?" in path or "#" in path:
        return None
    return f"{host.lower()}/{path}"


def token() -> str:
    return secrets.token_urlsafe(32)
