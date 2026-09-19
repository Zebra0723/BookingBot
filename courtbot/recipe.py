"""The captured booking flow, as data.

The bot cannot know David Lloyd's endpoints ahead of time — they are private,
undocumented and change without notice. Rather than hardcode guesses, discovery
watches one real booking and writes a *recipe*: the three requests that matter
(log in, list availability, book a slot) with the volatile parts replaced by
placeholders.

Replaying a recipe is what makes the sniper fast. A browser needs seconds to
render the booking page; a recipe is three HTTP calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# Request headers that must not be replayed: they describe the captured
# exchange, not ours, and sending them stale breaks the request.
VOLATILE_HEADERS = {
    "content-length", "host", "connection", "cookie", "date",
    "if-none-match", "if-modified-since", ":authority", ":method",
    ":path", ":scheme", "accept-encoding",
}


class RecipeError(RuntimeError):
    """Raised when a recipe is missing or cannot be applied. User-facing."""


def extract_path(data: Any, path: str) -> Any:
    """Read `path` out of decoded JSON.

    Supports dotted keys, integer indices and a `[*]` wildcard that maps over a
    list, e.g. `data.slots[*].startTime`. Returns None when the path does not
    resolve, so a recipe that drifts degrades to "not found" rather than an
    exception mid-race.
    """
    cur = data
    for token in _tokenise(path):
        if cur is None:
            return None
        if token == "*":
            # Not a step of its own: it asserts we are at a list and lets the
            # following key map over it. A path ending in [*] yields the list.
            if not isinstance(cur, list):
                return None
            continue
        if isinstance(token, int):
            if not isinstance(cur, (list, tuple)) or not -len(cur) <= token < len(cur):
                return None
            cur = cur[token]
        else:
            if isinstance(cur, list):
                # Mapping a key over a wildcarded list.
                mapped = [x.get(token) if isinstance(x, dict) else None for x in cur]
                cur = mapped
                continue
            if not isinstance(cur, dict):
                return None
            cur = cur.get(token)
    return cur


def _tokenise(path: str) -> list[Any]:
    tokens: list[Any] = []
    for part in path.split("."):
        if not part:
            continue
        name, *brackets = re.split(r"\[(.*?)\]", part)
        if name:
            tokens.append(name)
        for b in brackets:
            if b == "":
                continue
            if b == "*":
                tokens.append("*")
            else:
                try:
                    tokens.append(int(b))
                except ValueError:
                    tokens.append(b.strip("'\""))
    return tokens


def render(template: str, values: dict[str, Any], *, url_encode: bool = False) -> str:
    """Substitute `{name}` placeholders, failing loudly on a missing one.

    A silently unsubstituted placeholder would be sent literally to the club's
    API and look like a mysterious server-side rejection at 08:00:00, so this
    raises instead.
    """
    missing = [m for m in PLACEHOLDER_RE.findall(template) if m not in values]
    if missing:
        raise RecipeError(
            f"recipe placeholder(s) {missing} have no value. Known values: "
            f"{sorted(values)}. Re-run `courtbot discover` or edit the recipe."
        )

    def sub(match: re.Match[str]) -> str:
        raw = values[match.group(1)]
        text = "" if raw is None else str(raw)
        return quote(text, safe="") if url_encode else text

    return PLACEHOLDER_RE.sub(sub, template)


@dataclass
class Step:
    """One captured request, with the parts we vary turned into placeholders."""

    name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    content_type: str = "application/json"
    # name -> JSON path, pulled out of the response for later steps.
    extract: dict[str, str] = field(default_factory=dict)
    # Response codes that mean "worked". Empty means any 2xx.
    success_status: tuple[int, ...] = ()
    notes: str = ""

    def placeholders(self) -> set[str]:
        found = set(PLACEHOLDER_RE.findall(self.url))
        if self.body:
            found |= set(PLACEHOLDER_RE.findall(self.body))
        for v in self.headers.values():
            found |= set(PLACEHOLDER_RE.findall(v))
        return found

    def render(self, values: dict[str, Any]) -> tuple[str, dict[str, str], str | None]:
        url = render(self.url, values)
        headers = {k: render(v, values) for k, v in self.headers.items()}
        body = render(self.body, values) if self.body else None
        return url, headers, body

    def ok(self, status: int) -> bool:
        if self.success_status:
            return status in self.success_status
        return 200 <= status < 300


@dataclass
class SlotMapping:
    """How to read the availability response.

    Discovery guesses these from the captured JSON; the user corrects them if
    the guess is wrong, which is far less work than writing a parser.
    """

    list_path: str = "slots"
    time_field: str = "startTime"
    court_field: str = "courtName"
    id_field: str = "id"
    available_field: str = ""      # optional boolean field
    available_value: Any = True


@dataclass
class Recipe:
    base_url: str = ""
    login: Step | None = None
    availability: Step | None = None
    book: Step | None = None
    # Multi-step booking (add to basket -> checkout -> confirm). When present
    # this replaces `book`; each step's `extract` feeds the next, which is how
    # a basket id created in step one reaches the confirmation in step three.
    book_chain: list[Step] = field(default_factory=list)
    slots: SlotMapping = field(default_factory=SlotMapping)
    # Extra steps replayed between login and availability (CSRF warm-ups etc).
    preflight: list[Step] = field(default_factory=list)
    captured_at: str = ""
    source: str = ""

    def booking_steps(self) -> list[Step]:
        """The booking requests to run, in order."""
        if self.book_chain:
            return list(self.book_chain)
        return [self.book] if self.book else []

    def validate(self) -> list[str]:
        """Return human-readable problems; empty means ready to run."""
        problems: list[str] = []
        if not self.availability:
            problems.append("no availability step — discovery did not see a slot list")

        steps = self.booking_steps()
        if not steps:
            problems.append(
                "no booking step — discovery must observe one *completed* booking, "
                "not just browsing"
            )

        # Values the runtime always supplies, plus anything an earlier step
        # extracts. Walking the chain in order means a step may rely on a value
        # produced by any step before it, but not by one after it.
        known = {
            "date", "date_iso", "time", "end_time", "court_id", "slot_id",
            "club_id", "activity_id", "duration", "token", "username", "password",
        }
        if self.login:
            known |= set(self.login.extract)
        if self.availability:
            known |= set(self.availability.extract)
        for s in self.preflight:
            known |= set(s.extract)

        for i, step in enumerate(steps):
            unknown = step.placeholders() - known
            if unknown:
                where = f"booking step {i + 1} ({step.name})" if len(steps) > 1 else "booking step"
                problems.append(
                    f"{where} needs value(s) {sorted(unknown)} that nothing "
                    f"produces — add them to an earlier step's `extract`"
                )
            known |= set(step.extract)
        return problems

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "captured_at": self.captured_at,
            "source": self.source,
            "login": asdict(self.login) if self.login else None,
            "preflight": [asdict(s) for s in self.preflight],
            "availability": asdict(self.availability) if self.availability else None,
            "book": asdict(self.book) if self.book else None,
            "book_chain": [asdict(s) for s in self.book_chain],
            "slots": asdict(self.slots),
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False))
        return path

    @staticmethod
    def from_dict(raw: dict) -> "Recipe":
        def step(d: dict | None) -> Step | None:
            if not d:
                return None
            d = dict(d)
            d["success_status"] = tuple(d.get("success_status") or ())
            return Step(**d)

        return Recipe(
            base_url=raw.get("base_url", ""),
            login=step(raw.get("login")),
            preflight=[s for s in (step(x) for x in raw.get("preflight") or []) if s],
            availability=step(raw.get("availability")),
            book=step(raw.get("book")),
            book_chain=[s for s in (step(x) for x in raw.get("book_chain") or []) if s],
            slots=SlotMapping(**(raw.get("slots") or {})),
            captured_at=raw.get("captured_at", ""),
            source=raw.get("source", ""),
        )

    @staticmethod
    def load(path: str | Path) -> "Recipe":
        path = Path(path)
        if not path.exists():
            raise RecipeError(
                f"No recipe at {path}. Run `courtbot discover` first — it watches "
                f"you make one real booking and writes the recipe from that."
            )
        try:
            return Recipe.from_dict(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            raise RecipeError(f"{path} is not valid JSON: {exc}") from exc
