"""Issue keys of queue items and jobs: the shape check and the link template (`[keys] url`)."""

from __future__ import annotations

import re
from urllib.parse import quote

KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+")
KEY_MAX = 64
KEY_ENV = "LOADGOV_KEY"


def key_error(key) -> str | None:
    """Why `key` is not a valid issue key (None = valid). The key is chosen by the client, never derived."""
    if not isinstance(key, str):
        return "key must be a string"
    if len(key) > KEY_MAX or not KEY_RE.fullmatch(key):
        return f"key must look like PROJ-123 (letters, digits, underscore, a dash, digits; max {KEY_MAX})"
    return None


def key_url(template: str, key: str | None) -> str | None:
    """The link for `key`: the template with `{key}` replaced by the URL-quoted key; None without a
    template or a key."""
    if not template or not key:
        return None
    return template.replace("{key}", quote(key, safe=""))
