"""A stand-in for the club's booking API.

Used to exercise the whole pipeline — discover, distill, snipe, book — without
touching David Lloyd's systems. It mimics the parts that make real sniping
hard:

  * slots for a date do not exist until that date's release moment
  * a slot can be booked exactly once
  * optional rivals who grab slots milliseconds after release

Run standalone to rehearse before a real 08:00:

    python tools/mock_club.py --port 8765 --release-in 30
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USERNAME = "member@example.com"
PASSWORD = "correct-horse"
COURTS = ["Court 1", "Court 3", "Court 5"]
OPENING = range(7, 22)  # hourly slots 07:00–21:00


class ClubState:
    """Slot inventory and the release gate, shared across handler threads."""

    def __init__(self, release_at: datetime, advance_days: int = 9,
                 rival_after: float | None = None, rival_takes: list[str] | None = None):
        self.release_at = release_at
        self.advance_days = advance_days
        self.rival_after = rival_after
        self.rival_takes = rival_takes or []
        self.booked: set[str] = set()
        self.tokens: set[str] = set()
        self.lock = threading.Lock()
        self.booking_log: list[dict] = []

    def is_open(self, target: date) -> bool:
        """Whether `target` is inside the released window right now."""
        now = datetime.now(timezone.utc)
        if now < self.release_at:
            # Before release only dates strictly inside the old window exist.
            newest = (self.release_at - timedelta(days=1)).date() + timedelta(days=self.advance_days)
        else:
            newest = self.release_at.date() + timedelta(days=self.advance_days)
        return target <= newest

    def slot_id(self, target: date, hour: int, court: str) -> str:
        return f"{target.isoformat()}-{hour:02d}00-{court.replace(' ', '')}"

    def slots_for(self, target: date) -> list[dict]:
        now = datetime.now(timezone.utc)
        taken = set(self.booked)
        # Rivals only start grabbing slots once the release has happened.
        if self.rival_after is not None and now >= self.release_at + timedelta(seconds=self.rival_after):
            taken |= set(self.rival_takes)
        out = []
        for hour in OPENING:
            for court in COURTS:
                sid = self.slot_id(target, hour, court)
                out.append({
                    "id": sid,
                    "startTime": f"{hour:02d}:00",
                    "endTime": f"{hour + 1:02d}:00",
                    "courtName": court,
                    "available": sid not in taken,
                })
        return out

    def book(self, slot_id: str) -> tuple[bool, str]:
        with self.lock:
            now = datetime.now(timezone.utc)
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-", slot_id or "")
            if not m:
                return False, "unknown slot"
            target = date.fromisoformat(m.group(1))
            if not self.is_open(target):
                return False, "outside booking window"
            if self.rival_after is not None and slot_id in self.rival_takes:
                if now >= self.release_at + timedelta(seconds=self.rival_after):
                    return False, "slot no longer available"
            if slot_id in self.booked:
                return False, "slot no longer available"
            self.booked.add(slot_id)
            ref = f"BK-{uuid.uuid4().hex[:8].upper()}"
            self.booking_log.append({"ref": ref, "slot": slot_id, "at": now.isoformat()})
            return True, ref


class Handler(BaseHTTPRequestHandler):
    state: ClubState

    def log_message(self, fmt, *args):  # quieter test output
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        header = self.headers.get("authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.state.tokens

    def do_GET(self) -> None:  # noqa: N802
        parts = urlparse(self.path)
        if parts.path == "/":
            return self._json(200, {"service": "mock-club"})
        if parts.path == "/api/v2/availability":
            if not self._auth_ok():
                return self._json(401, {"error": "unauthorised"})
            qs = parse_qs(parts.query)
            raw = (qs.get("date") or [""])[0]
            try:
                target = date.fromisoformat(raw)
            except ValueError:
                return self._json(400, {"error": f"bad date {raw!r}"})
            if not self.state.is_open(target):
                return self._json(200, {"data": {"slots": []}})
            return self._json(200, {"data": {"slots": self.state.slots_for(target)}})
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = urlparse(self.path)
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}

        if parts.path == "/api/v2/auth/login":
            if payload.get("username") != USERNAME or payload.get("password") != PASSWORD:
                return self._json(401, {"error": "bad credentials"})
            token = "eyJtb2Nr." + uuid.uuid4().hex + ".sig"
            self.state.tokens.add(token)
            return self._json(200, {"data": {"accessToken": token, "memberId": 9911}})

        if parts.path == "/api/v2/bookings":
            if not self._auth_ok():
                return self._json(401, {"error": "unauthorised"})
            ok, detail = self.state.book(str(payload.get("slotId", "")))
            if ok:
                return self._json(201, {"data": {"bookingRef": detail, "status": "CONFIRMED"}})
            return self._json(409, {"error": detail})

        return self._json(404, {"error": "not found"})


def serve(state: ClubState, port: int = 0, verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock club booking API")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--release-in", type=float, default=30.0,
                    help="seconds from now until the booking window opens")
    ap.add_argument("--advance-days", type=int, default=9)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    release = datetime.now(timezone.utc) + timedelta(seconds=args.release_in)
    state = ClubState(release, advance_days=args.advance_days)
    server = serve(state, args.port, args.verbose)
    print(f"mock club on http://127.0.0.1:{server.server_address[1]}")
    print(f"  credentials: {USERNAME} / {PASSWORD}")
    print(f"  release at:  {release.isoformat()}  (in {args.release_in:.0f}s)")
    print(f"  window:      {args.advance_days} days")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
