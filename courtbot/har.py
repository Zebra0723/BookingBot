"""Read a HAR file into Exchanges.

Playwright records discovery as a HAR. Keeping the parsing separate from the
browser means the analysis path is testable without launching anything, and a
HAR exported by hand from Safari or Chrome DevTools works just as well — useful
because the iPad app's traffic can be captured that way too.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from pathlib import Path

from .distill import Exchange


def _headers(entries: list[dict] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in entries or []:
        name = str(h.get("name", ""))
        if name:
            # Later values win; HAR may repeat a header.
            out[name] = str(h.get("value", ""))
    return out


def _content_text(content: dict | None) -> str:
    if not content:
        return ""
    text = content.get("text") or ""
    if content.get("encoding") == "base64" and text:
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return ""
    return text


def _started(entry: dict) -> float:
    raw = entry.get("startedDateTime")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse(path: str | Path) -> list[Exchange]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no HAR at {path}")
    try:
        data = json.loads(path.read_text(errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not a valid HAR file: {exc}") from exc

    entries = (data.get("log") or {}).get("entries") or []
    out: list[Exchange] = []
    for entry in entries:
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        post = req.get("postData") or {}
        out.append(
            Exchange(
                method=str(req.get("method", "GET")),
                url=str(req.get("url", "")),
                headers=_headers(req.get("headers")),
                body=post.get("text") or None,
                status=int(resp.get("status") or 0),
                response_text=_content_text(resp.get("content")),
                resource_type=str(entry.get("_resourceType", "")),
                started_at=_started(entry),
                error=str(entry.get("_error") or resp.get("_error") or ""),
            )
        )
    out.sort(key=lambda e: e.started_at)
    return out
