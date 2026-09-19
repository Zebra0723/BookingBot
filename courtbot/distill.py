"""Turn captured traffic into a Recipe.

Discovery records every request the booking site makes while the user books a
court by hand. Most of it is noise — analytics, fonts, images. This module
picks out the three that matter and replaces the booked date, time and court
with placeholders so the request can be re-aimed at a different slot.

The heuristics are deliberately transparent: every guess is reported with the
evidence behind it, because a wrong guess here surfaces as a failed booking at
08:00 and the user needs to be able to audit and correct the recipe by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Iterable
from urllib.parse import unquote

from .recipe import Recipe, SlotMapping, Step, VOLATILE_HEADERS, extract_path

# --- classification signals -------------------------------------------------

LOGIN_URL_HINTS = re.compile(
    r"(log[-_]?in|sign[-_]?in|auth|oauth|token|session|identity|account/login)", re.I
)
AVAIL_URL_HINTS = re.compile(
    r"(avail|slot|timetable|schedule|court|session|capacity|activit|resource|diary)", re.I
)
BOOK_URL_HINTS = re.compile(
    r"(book|reserve|reservation|basket|cart|checkout|confirm|commit|place)", re.I
)
# Words that rule an endpoint out entirely. "/bookings/cancel" also matches the
# positive "book" hint, so these have to disqualify rather than merely offset —
# otherwise the distiller can wire the bot up to the cancellation endpoint.
BOOK_DISQUALIFY = re.compile(r"(cancel|delete|remove|refund|history|past)", re.I)
# Words that suggest a read rather than a write; penalised, not fatal.
BOOK_SOFT_NEGATIVE = re.compile(r"(list|search|upcoming|summary|availabilit)", re.I)

PASSWORD_KEYS = re.compile(r"(password|passwd|pwd|pin|secret|credential)", re.I)
USERNAME_KEYS = re.compile(r"(username|user|email|login|membership|memberid)", re.I)
# A phone app usually signs in once and then renews a long-lived refresh
# token, so a capture may show a renewal rather than a password exchange.
REFRESH_KEYS = re.compile(r"(refresh[-_]?token|grant[-_]?type)", re.I)

TOKEN_KEYS = re.compile(
    r"^(access[-_]?token|id[-_]?token|auth[-_]?token|jwt|bearer|token|session[-_]?id|sid|api[-_]?key)$",
    re.I,
)

STATIC_SUFFIX = re.compile(
    r"\.(css|js|mjs|png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico|map|mp4|webm)(\?|$)", re.I
)
ANALYTICS_HOST = re.compile(
    r"(google|gstatic|doubleclick|facebook|segment|sentry|hotjar|optimizely|"
    r"newrelic|datadog|cloudflareinsights|clarity\.ms|tiktok|snapchat|bing)", re.I
)

TIME_VALUE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)(?::([0-5]\d))?\s*$")


@dataclass
class Exchange:
    """One recorded request/response pair, normalised across capture sources."""

    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    status: int
    response_text: str
    resource_type: str = ""
    started_at: float = 0.0
    # Set when the capture tool recorded a transport failure rather than a
    # reply — the usual fingerprint of certificate pinning.
    error: str = ""

    @property
    def json_body(self) -> Any:
        return _try_json(self.body)

    @property
    def json_response(self) -> Any:
        return _try_json(self.response_text)

    def is_noise(self) -> bool:
        if STATIC_SUFFIX.search(self.url) or ANALYTICS_HOST.search(self.url):
            return True
        if self.resource_type in {"image", "font", "stylesheet", "media", "script"}:
            return True
        return False


def _try_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _flatten(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Walk decoded JSON yielding (dotted_path, value) for every leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            yield from _flatten(v, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = f"{prefix}[{i}]"
            yield from _flatten(v, path)
    else:
        yield prefix, obj


def date_variants(d: date) -> list[str]:
    """Every spelling of a date we might find in a URL or body.

    Ordered longest-first so that replacing '2026-09-30' never leaves a stray
    fragment behind from a shorter match.
    """
    out = [
        d.isoformat(),
        d.strftime("%d/%m/%Y"), d.strftime("%m/%d/%Y"),
        d.strftime("%d-%m-%Y"), d.strftime("%Y/%m/%d"),
        d.strftime("%Y%m%d"), d.strftime("%d.%m.%Y"),
        d.strftime("%d %B %Y"), d.strftime("%d %b %Y"),
    ]
    return sorted(set(out), key=len, reverse=True)


def time_variants(t: time) -> list[str]:
    out = [
        t.strftime("%H:%M:%S"), t.strftime("%H:%M"),
        t.strftime("%H.%M"), t.strftime("%H%M"),
        f"{t.hour}:{t.minute:02d}",
    ]
    return sorted(set(out), key=len, reverse=True)


def _templatise(text: str | None, subs: list[tuple[list[str], str]]) -> tuple[str | None, list[str]]:
    """Replace known literal values with `{placeholder}`; report what was hit."""
    if not text:
        return text, []
    hits: list[str] = []
    for variants, placeholder in subs:
        for v in variants:
            if v and v in text:
                text = text.replace(v, "{%s}" % placeholder)
                hits.append(f"{placeholder}<-{v}")
                break
    return text, hits


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in VOLATILE_HEADERS and not k.startswith(":")
    }


def _templatise_headers(
    headers: dict[str, str], subs: list[tuple[list[str], str]]
) -> dict[str, str]:
    """Replace captured secrets inside header values (notably Authorization)."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        replaced, _ = _templatise(v, subs)
        out[k] = replaced if replaced is not None else v
    return out


def _looks_like_login(ex: Exchange) -> int:
    """Score how likely this exchange is the login call."""
    if ex.method.upper() not in {"POST", "PUT"}:
        return 0
    score = 0
    if LOGIN_URL_HINTS.search(ex.url):
        score += 3
    body = ex.json_body
    if isinstance(body, (dict, list)):
        keys = [k for k, _ in _flatten(body)]
        if any(PASSWORD_KEYS.search(k) for k in keys):
            score += 5
        if any(USERNAME_KEYS.search(k) for k in keys):
            score += 2
        if any(REFRESH_KEYS.search(k) for k in keys):
            score += 5
    elif ex.body and PASSWORD_KEYS.search(ex.body):
        score += 4
    elif ex.body and REFRESH_KEYS.search(ex.body):
        score += 4
    if find_token_path(ex.json_response):
        score += 3
    return score


def _looks_like_availability(ex: Exchange, booked: date | None) -> int:
    score = 0
    if AVAIL_URL_HINTS.search(ex.url):
        score += 3
    if booked and any(v in ex.url for v in date_variants(booked)):
        score += 3
    resp = ex.json_response
    lst = find_slot_list(resp)
    if lst:
        score += 4
        if _list_has_times(lst[1]):
            score += 3
    if ex.method.upper() == "GET":
        score += 1
    return score


def _looks_like_booking(ex: Exchange, booked: date | None) -> int:
    if ex.method.upper() not in {"POST", "PUT", "PATCH"}:
        return 0
    if BOOK_DISQUALIFY.search(ex.url):
        return 0
    score = 0
    if BOOK_URL_HINTS.search(ex.url):
        score += 4
    if BOOK_SOFT_NEGATIVE.search(ex.url):
        score -= 3
    haystack = f"{ex.url}\n{ex.body or ''}"
    if booked and any(v in haystack for v in date_variants(booked)):
        score += 4
    if _looks_like_login(ex) >= 5:
        score -= 8
    if 200 <= ex.status < 300:
        score += 1
    return score


def _list_has_times(items: list) -> bool:
    for item in items[:20]:
        if isinstance(item, dict):
            for v in item.values():
                if isinstance(v, str) and TIME_VALUE.match(v):
                    return True
                if isinstance(v, str) and "T" in v and len(v) >= 16:
                    return True
    return False


def find_token_path(resp: Any) -> str | None:
    """Locate an auth token in a login response, by key name then by shape."""
    if not isinstance(resp, (dict, list)):
        return None
    candidates = [
        (path, val) for path, val in _flatten(resp)
        if isinstance(val, str) and len(val) >= 16
    ]
    for path, val in candidates:
        leaf = path.split(".")[-1].split("[")[0]
        if TOKEN_KEYS.match(leaf):
            return path
    # Fall back to anything shaped like a JWT.
    for path, val in candidates:
        if val.count(".") == 2 and val.startswith("ey"):
            return path
    return None


def find_slot_list(resp: Any) -> tuple[str, list] | None:
    """Find the longest list of dicts in a response — the slot collection."""
    if not isinstance(resp, (dict, list)):
        return None
    best: tuple[str, list] | None = None

    def walk(node: Any, path: str) -> None:
        nonlocal best
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                if best is None or len(node) > len(best[1]):
                    best = (path or "", node)
            for i, v in enumerate(node[:5]):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)

    walk(resp, "")
    return best


def guess_slot_mapping(items: list[dict]) -> tuple[SlotMapping, list[str]]:
    """Guess which fields hold the time, court and slot id."""
    notes: list[str] = []
    keys = sorted({k for item in items[:30] if isinstance(item, dict) for k in item})

    def pick(patterns: list[str], exclude: str = "") -> str:
        for pat in patterns:
            for k in keys:
                if re.search(pat, k, re.I) and (not exclude or not re.search(exclude, k, re.I)):
                    return k
        return ""

    time_field = pick([r"^start", r"start.*(time|date)", r"^time$", r"time"], exclude=r"end|stop")
    if not time_field:
        for k in keys:
            vals = [i.get(k) for i in items[:10] if isinstance(i, dict)]
            if any(isinstance(v, str) and TIME_VALUE.match(v) for v in vals):
                time_field = k
                break
    court_field = pick([r"court.*name", r"^court", r"resource.*name", r"^resource", r"^name$", r"label"])
    id_field = pick([r"^id$", r"slot.*id", r"booking.*id", r"^ref", r"uid", r"guid", r"key"])
    avail_field = pick([r"available", r"bookable", r"isfree", r"^free$", r"canbook"])

    mapping = SlotMapping(
        list_path="",  # filled by caller
        time_field=time_field or "startTime",
        court_field=court_field or "",
        id_field=id_field or "",
        available_field=avail_field or "",
    )
    if not time_field:
        notes.append("could not identify the slot start-time field — set slots.time_field by hand")
    if not id_field:
        notes.append("no slot id field found; booking may need court+time instead")
    notes.append(f"slot fields seen: {', '.join(keys[:25])}")
    return mapping, notes


def slot_identifier_values(items: list[dict], mapping: SlotMapping) -> dict[str, list[str]]:
    """Identifier values seen in the availability response, by placeholder.

    The booking request captured during discovery refers to *one specific* slot.
    Left as a literal it would make the bot rebook that same dead slot forever,
    so these values are matched against the booking request and swapped for
    placeholders the sniper fills from live availability.
    """
    out: dict[str, list[str]] = {"slot_id": [], "court_id": []}
    for item in items:
        if not isinstance(item, dict):
            continue
        if mapping.id_field and item.get(mapping.id_field) not in (None, ""):
            out["slot_id"].append(str(item[mapping.id_field]))
        if mapping.court_field and item.get(mapping.court_field) not in (None, ""):
            out["court_id"].append(str(item[mapping.court_field]))
    # Longest first so a short id cannot partially match inside a longer one.
    for k in out:
        out[k] = sorted(set(out[k]), key=len, reverse=True)
    return out


def placeholder_name(path: str, taken: set[str]) -> str:
    """A readable, unique placeholder name derived from a JSON path.

    `data.basketId` becomes `basket_id`. Reserved runtime names are never
    reused, since shadowing `date` or `slot_id` would silently misdirect the
    booking request.
    """
    leaf = path.split(".")[-1].split("[")[0] or "value"
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", leaf).lower()
    snake = re.sub(r"[^a-z0-9_]", "_", snake).strip("_") or "value"
    if snake[0].isdigit():
        snake = f"v_{snake}"
    base, n = snake, 2
    while snake in taken or snake in RESERVED_PLACEHOLDERS:
        snake = f"{base}_{n}"
        n += 1
    return snake


RESERVED_PLACEHOLDERS = {
    "date", "date_iso", "time", "end_time", "slot_id", "court_id",
    "club_id", "activity_id", "duration", "token", "username", "password",
}


def wire_chain(pairs: list[tuple[Step, Exchange]]) -> list[str]:
    """Connect a multi-step booking flow.

    Each step after the first may carry a literal — a basket id, a reservation
    token — that an earlier step's *response* produced. Replayed as captured
    those literals are stale, so they are replaced with placeholders and the
    earlier step gains an `extract` that regenerates them at run time.

    Mutates the steps in place; returns notes for the report.
    """
    notes: list[str] = []
    taken: set[str] = set()
    for i in range(1, len(pairs)):
        step, _ = pairs[i]
        for j in range(i):
            prev_step, prev_ex = pairs[j]
            resp = prev_ex.json_response
            if not isinstance(resp, (dict, list)):
                continue
            for path, value in _flatten(resp):
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    continue
                literal = str(value)
                # Short values match by coincidence ("1", "60"); ignore them.
                if len(literal) < 6:
                    continue
                haystack = step.url + (step.body or "")
                if literal not in haystack:
                    continue
                existing = next(
                    (n for n, p in prev_step.extract.items() if p == path), None
                )
                name = existing or placeholder_name(path, taken)
                taken.add(name)
                prev_step.extract[name] = path
                step.url = step.url.replace(literal, "{%s}" % name)
                if step.body:
                    step.body = step.body.replace(literal, "{%s}" % name)
                notes.append(
                    f"wired {{{name}}} in `{step.name}` from `{prev_step.name}`.{path}"
                )
    return notes


@dataclass
class Distillation:
    recipe: Recipe
    report: list[str]
    considered: int
    kept: int
    # Captured secrets that cannot be retyped from memory, kept out of the
    # recipe and handed to the caller to store safely.
    secrets: dict[str, str] = field(default_factory=dict)


def distill(
    exchanges: list[Exchange],
    *,
    booked_date: date | None = None,
    booked_time: time | None = None,
    base_url: str = "",
    source: str = "",
) -> Distillation:
    """Build a Recipe from recorded traffic."""
    report: list[str] = []
    candidates = [e for e in exchanges if not e.is_noise()]
    report.append(f"{len(exchanges)} requests recorded, {len(candidates)} after dropping assets/analytics")

    scored_login = sorted(
        ((_looks_like_login(e), e) for e in candidates), key=lambda p: -p[0]
    )
    scored_avail = sorted(
        ((_looks_like_availability(e, booked_date), e) for e in candidates), key=lambda p: -p[0]
    )
    scored_book = sorted(
        ((_looks_like_booking(e, booked_date), e) for e in candidates), key=lambda p: -p[0]
    )

    subs: list[tuple[list[str], str]] = []
    if booked_date:
        subs.append((date_variants(booked_date), "date"))
    if booked_time:
        subs.append((time_variants(booked_time), "time"))

    secrets: dict[str, str] = {}
    slot_ids: dict[str, list[str]] = {"slot_id": [], "court_id": []}
    # The captured Authorization header carries the token from the *recording*
    # session. Replayed as-is it is long expired, so the literal value is
    # swapped for {token} wherever it reappears downstream.
    auth_subs: list[tuple[list[str], str]] = []
    recipe = Recipe(base_url=base_url, source=source,
                    captured_at=datetime.now().astimezone().isoformat(timespec="seconds"))

    # --- login ---
    if scored_login and scored_login[0][0] >= 5:
        score, ex = scored_login[0]
        body, _ = _templatise(ex.body, [])
        # Re-template the credentials themselves so they come from the env.
        body, secrets = _replace_credentials(body)
        step = Step(
            name="login", method=ex.method.upper(), url=ex.url,
            headers=_clean_headers(ex.headers), body=body,
            content_type=ex.headers.get("content-type", "application/json"),
            notes=f"auto-detected (score {score})",
        )
        token_path = find_token_path(ex.json_response)
        if token_path:
            step.extract = {"token": token_path}
            token_value = extract_path(ex.json_response, token_path)
            if isinstance(token_value, str) and len(token_value) >= 16:
                auth_subs.append(([token_value], "token"))
            report.append(f"login: {ex.method} {_short(ex.url)} — token at `{token_path}`")
        else:
            report.append(
                f"login: {ex.method} {_short(ex.url)} — no token field found in the "
                f"response; auth is probably cookie-based (that is fine, cookies are kept)"
            )
        recipe.login = step
        if "refresh_token" in secrets:
            report.append(
                "  this is a refresh-token renewal, not a password sign-in — "
                "the token has been kept out of the recipe"
            )
    else:
        report.append(
            "login: NOT FOUND. If you were already signed in when discovery started, "
            "log out first and re-run so the login request is captured."
        )

    # --- availability ---
    if scored_avail and scored_avail[0][0] >= 4:
        score, ex = scored_avail[0]
        url, url_hits = _templatise(ex.url, auth_subs + subs)
        body, body_hits = _templatise(ex.body, auth_subs + subs)
        step = Step(
            name="availability", method=ex.method.upper(), url=url,
            headers=_templatise_headers(_clean_headers(ex.headers), auth_subs),
            body=body,
            content_type=ex.headers.get("content-type", "application/json"),
            notes=f"auto-detected (score {score}); templated {url_hits + body_hits}",
        )
        recipe.availability = step
        found = find_slot_list(ex.json_response)
        if found:
            path, items = found
            mapping, notes = guess_slot_mapping(items)
            mapping.list_path = path
            recipe.slots = mapping
            slot_ids = slot_identifier_values(items, mapping)
            report.append(
                f"availability: {ex.method} {_short(url)} — {len(items)} slots at `{path}`, "
                f"time field `{mapping.time_field}`"
            )
            report.extend(f"  note: {n}" for n in notes)
        else:
            report.append(
                f"availability: {ex.method} {_short(url)} — response is not JSON with a "
                f"slot list; set slots.* by hand or the page may render server-side"
            )
        if not url_hits and not body_hits and booked_date:
            report.append(
                "  WARNING: the booked date does not appear in this request, so the bot "
                "cannot re-aim it at another day. Check `availability.url` by hand."
            )
    else:
        report.append("availability: NOT FOUND — no response looked like a slot list.")

    # --- booking ---
    # A booking flow may be one request or several (add to basket, checkout,
    # confirm). Take every candidate that scores, in the order it happened, and
    # keep only those after the availability lookup — a POST before the user had
    # even seen the slots is not part of booking.
    avail_ex = scored_avail[0][1] if (scored_avail and scored_avail[0][0] >= 4) else None
    avail_pos = candidates.index(avail_ex) if avail_ex in candidates else -1
    chain_exchanges = [
        e for pos, e in enumerate(candidates)
        if pos > avail_pos and _looks_like_booking(e, booked_date) >= 5
    ]

    if len(chain_exchanges) > 1:
        book_subs = [
            (slot_ids["slot_id"], "slot_id"),
            (slot_ids["court_id"], "court_id"),
        ] + subs
        pairs: list[tuple[Step, Exchange]] = []
        all_hits: list[str] = []
        for n, ex in enumerate(chain_exchanges):
            url, uh = _templatise(ex.url, auth_subs + book_subs)
            body, bh = _templatise(ex.body, auth_subs + book_subs)
            all_hits += uh + bh
            pairs.append((
                Step(
                    name=f"book_{n + 1}", method=ex.method.upper(), url=url,
                    headers=_templatise_headers(_clean_headers(ex.headers), auth_subs),
                    body=body,
                    content_type=ex.headers.get("content-type", "application/json"),
                    notes="part of a multi-step booking flow",
                ),
                ex,
            ))
        wiring = wire_chain(pairs)
        recipe.book_chain = [s for s, _ in pairs]
        report.append(
            f"booking: {len(pairs)}-step flow — " +
            " -> ".join(f"{s.method} {_short(s.url, 40)}" for s, _ in pairs)
        )
        report.extend(f"  {w}" for w in wiring)
        if not all_hits:
            report.append(
                "  WARNING: neither the date nor the time appears anywhere in the "
                "booking flow. Check the chain by hand before relying on it."
            )
    elif scored_book and scored_book[0][0] >= 5:
        score, ex = scored_book[0]
        # Slot/court identifiers must be templated before date and time: an id
        # such as "2026-09-30-10:00-C3" embeds them, and substituting the date
        # first would corrupt the id beyond recognition.
        book_subs = [
            (slot_ids["slot_id"], "slot_id"),
            (slot_ids["court_id"], "court_id"),
        ] + subs
        url, url_hits = _templatise(ex.url, auth_subs + book_subs)
        body, body_hits = _templatise(ex.body, auth_subs + book_subs)
        recipe.book = Step(
            name="book", method=ex.method.upper(), url=url,
            headers=_templatise_headers(_clean_headers(ex.headers), auth_subs),
            body=body,
            content_type=ex.headers.get("content-type", "application/json"),
            success_status=(ex.status,) if ex.status not in range(200, 300) else (),
            notes=f"auto-detected (score {score}); templated {url_hits + body_hits}",
        )
        report.append(
            f"booking: {ex.method} {_short(url)} -> {ex.status}; "
            f"templated {url_hits + body_hits or '(nothing — check by hand)'}"
        )
        if not (url_hits or body_hits):
            report.append(
                "  WARNING: neither the date nor the time appears in the booking request. "
                "It probably references a slot id from the availability response — map it "
                "via availability.extract and a {slot_id} placeholder."
            )
    else:
        report.append(
            "booking: NOT FOUND. Discovery must observe a booking you actually "
            "confirm — browsing availability alone is not enough."
        )

    return Distillation(recipe=recipe, report=report,
                        considered=len(exchanges), kept=len(candidates),
                        secrets=secrets)


def _replace_credentials(body: str | None) -> tuple[str | None, dict[str, str]]:
    """Swap captured credentials for placeholders, and hand back what was found.

    The recipe is committed and read by humans, so a captured password or
    refresh token must never reach it. Each is replaced by a placeholder the
    sniper fills from the environment.

    The captured *values* are returned separately rather than discarded: a
    refresh token cannot be typed from memory, so the caller writes it to a
    protected file instead of making the user dig through the HAR by hand. A
    password is deliberately not returned — the user already knows it.
    """
    if not body:
        return body, {}
    found: dict[str, str] = {}

    def classify(key: str) -> str | None:
        # Order matters: "refresh_token" also matches the generic token rule.
        if REFRESH_KEYS.search(key) and "grant" not in key.lower():
            return "refresh_token"
        if PASSWORD_KEYS.search(key):
            return "password"
        if USERNAME_KEYS.search(key):
            return "username"
        return None

    parsed = _try_json(body)
    if isinstance(parsed, dict):
        out = dict(parsed)
        for k in list(out):
            kind = classify(k)
            if kind is None or not isinstance(out[k], str):
                continue
            if kind == "refresh_token":
                found["refresh_token"] = out[k]
            out[k] = "{%s}" % kind
        return json.dumps(out), found

    # Form-encoded fallback (common for OAuth token endpoints).
    def sub(m: re.Match[str]) -> str:
        key, value = m.group(1), m.group(2)
        kind = classify(key)
        if kind is None:
            return m.group(0)
        if kind == "refresh_token":
            found["refresh_token"] = unquote(value)
        return f"{key}={{{kind}}}"

    return re.sub(r"([A-Za-z0-9_\-\[\]]+)=([^&]*)", sub, body), found


def _short(url: str, limit: int = 78) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"
