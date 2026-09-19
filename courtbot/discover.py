"""Turn a captured session into a recipe.

Court booking at Harbour Club Chelsea is app-only — there is no web booking
page to drive — so captures come from the iOS app's traffic rather than from a
browser. Any HAR works: Proxyman, Charles, mitmproxy, or a hand-exported one.

The analysis lives here; reading the file is in `har`, and the heuristics are
in `distill`. Keeping them apart means the whole path is testable without a
proxy, a device, or a network.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from urllib.parse import urlsplit

from . import har as har_reader
from .distill import Exchange, distill
from .recipe import Recipe

log = logging.getLogger(__name__)


class DiscoveryError(RuntimeError):
    """User-facing discovery failure."""


@dataclass
class DiscoveryResult:
    har_path: Path
    recipe_path: Path
    report: list[str]
    recipe: Recipe
    secrets: dict[str, str] = field(default_factory=dict)
    secrets_path: Path | None = None


def analyse(
    har_path: Path,
    *,
    out_dir: Path,
    booked_date: date | None,
    booked_time: time | None,
    base_url: str = "",
) -> DiscoveryResult:
    """Distill a captured session into a recipe on disk."""
    exchanges = har_reader.parse(har_path)
    if not exchanges:
        raise DiscoveryError(
            f"{har_path} contains no requests. The capture recorded nothing — "
            f"check the device was actually routed through the proxy."
        )
    result = distill(
        exchanges,
        booked_date=booked_date,
        booked_time=booked_time,
        base_url=base_url or infer_base(exchanges),
        source=str(har_path),
    )
    recipe_path = result.recipe.save(out_dir / "recipe.json")
    secrets_path = save_secrets(result.secrets, out_dir) if result.secrets else None
    return DiscoveryResult(
        har_path=har_path,
        recipe_path=recipe_path,
        report=result.report,
        recipe=result.recipe,
        secrets=result.secrets,
        secrets_path=secrets_path,
    )


def save_secrets(secrets: dict[str, str], out_dir: Path) -> Path:
    """Write captured secrets to a protected file, not to the recipe.

    A refresh token cannot be retyped from memory, so discarding it would leave
    the user picking through a HAR by hand. It is written mode 0600 into the
    gitignored capture directory, in a form that can be sourced directly.
    """
    path = out_dir / "secrets.env"
    lines = [
        "# Written by `courtbot discover` from the captured session.",
        "# Secrets, and deliberately NOT in the recipe. Load with:  source secrets.env",
        "",
    ]
    lines += [f"export DL_{name.upper()}='{value}'" for name, value in sorted(secrets.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def infer_base(exchanges: list[Exchange]) -> str:
    """The origin the app talked to most, ignoring assets and analytics."""
    counts: Counter[str] = Counter()
    for e in exchanges:
        if e.is_noise():
            continue
        parts = urlsplit(e.url)
        if parts.scheme and parts.netloc:
            counts[f"{parts.scheme}://{parts.netloc}"] += 1
    return counts.most_common(1)[0][0] if counts else ""
