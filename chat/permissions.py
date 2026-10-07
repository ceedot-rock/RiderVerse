"""Permissions adapter: chat-friendly API over Worker 1's identity core.

WHAT THIS IS
  The chat server (server.py) talks ONLY to the functions below. When
  ~/workspace/group-agent/identity/core.py is importable (it is -- Worker 1
  finished it), every call below drives the REAL implementation: Ed25519
  seats, Chamber-sealed grants, the revocation list, and the silo gate.
  If the real module cannot be imported (e.g. missing PyNaCl), the adapter
  falls back to an in-memory stub with identical function names so the
  demo still runs.

CHAT-FRIENDLY SIGNATURES (what server.py uses)
    issue_group_seat(group_id) -> Seat-ish
    issue_personal_seat(identity_id, label=None) -> Seat-ish
    grant(identity_id, scopes, group_id) -> dict
    revoke(identity_id, group_id) -> dict
    verify_grant(grant_obj) -> bool
    is_trusted(identity_id, group_id=None) -> bool
    check_access(identity_id, scope, group_id, action="read") -> bool

HOW THEY MAP TO identity/core.py
  * One RiderXL GROUP seat per chat group:
      core.issue_group_seat(label="RiderXL", group_id=<chat group_id>)
    cached in this adapter for the process lifetime.
  * One PERSONAL seat per chat identity_id:
      core.issue_personal_seat(label=<display name>)
    The chat identity_id ("id-...") is the stable key; the seat's agent_id
    ("p-...") is what core calls user_id. Mapping lives in _user_seats.
  * grant(): core.set_trust(user_id, group_id, True), then one
    core.grant(user_seat, riderxl_seat.agent_id, scope) per scope.
    Grant TTL: 7 days (lab-demo choice; core's default is 1 hour -- too
    short for a demo session, and expiry would silently mute the agent).
    Grant ids are recorded so revoke() can find them.
  * revoke(): core.revoke(grant_id, user_seat) for every live grant, then
    core.set_trust(user_id, group_id, False). Access cuts immediately:
    verify_grant() consults the revocation list.
  * is_trusted(): core.is_trusted(user_id, group_id) -- the explicit opt-in
    flag. With group_id=None, True if trusted in ANY known group.
  * check_access(): core.check_access(riderxl_seat_id, user_id, action,
    scope_base) -- the full silo gate (seat kinds, trust flag, signed +
    sealed + fresh + unrevoked grant naming the exact scope). The server
    uses this as the dispatch gate for "chat:write".
  * verify_grant(): core.verify_grant(grant_obj) directly.

SERVER POLICY (documented here so the mapping is explicit)
  * Dispatch gate (does RiderXL process this member's message?):
    check_access(identity_id, "chat:write", group_id). Signed-grant based.
  * Approve/deny gate on approval cards: check_access(identity_id,
    "chat:write", group_id). A production deployment would mint
    "spend:approve" grants for approvers; the demo's permission card only
    asks for chat:read + chat:write, so approval rides on chat:write.
  * Permission-card display logic keys off is_trusted() (the opt-in flag).

LAB CAVEATS
  * core's seat/grant/trust registries are IN-MEMORY. Restarting the chat
    server wipes them: every identity must re-grant. This is Worker 1's
    documented lab posture ("no persisted private keys"); the demo honors
    it rather than working around it.
  * core.audit() appends to identity/audit.jsonl -- public ids only.
"""
from __future__ import annotations

import json
import os
import threading
import time

try:
    from identity import core as _core
    _REAL = True
except Exception:
    _core = None
    _REAL = False

USING_REAL_IDENTITY = _REAL

_GRANT_TTL_SECONDS = 7 * 24 * 3600  # lab-demo choice; core default is 3600

# Review fix 2026-10-07: RLock (re-entrant) guarding every store access,
# including the read paths below that previously ran unlocked.
_lock = threading.RLock()
_riderxl_seats: dict[str, object] = {}   # chat group_id -> core Seat (group)
_user_seats: dict[str, object] = {}       # chat identity_id -> core Seat (personal)
_user_labels: dict[str, str] = {}         # chat identity_id -> display name
_grant_ids: dict[tuple[str, str], list[str]] = {}  # (user_id, riderxl_id) -> [grant_id]
_stored_grants: dict[str, dict] = {}     # grant_id -> grant obj (for verify_grant demo)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------------------------------------------------------------------
# real implementation
# ---------------------------------------------------------------------------
def _riderxl_seat(group_id: str):
    seat = _riderxl_seats.get(group_id)
    if seat is None:
        seat = _core.issue_group_seat(label="RiderXL", group_id=group_id)
        _riderxl_seats[group_id] = seat
    return seat


def _user_seat(identity_id: str, label: str | None = None):
    seat = _user_seats.get(identity_id)
    if seat is None:
        seat = _core.issue_personal_seat(label or identity_id)
        _user_seats[identity_id] = seat
        if label:
            _user_labels[identity_id] = label
    elif label and _user_labels.get(identity_id) != label:
        _user_labels[identity_id] = label
    return seat


def _real_issue_group_seat(group_id, member_id=None, scopes=None):
    with _lock:
        return _riderxl_seat(group_id)


def _real_issue_personal_seat(identity_id, label=None, scopes=None):
    with _lock:
        return _user_seat(identity_id, label)


def _real_grant(identity_id, scopes, group_id):
    with _lock:
        user_seat = _user_seat(identity_id, _user_labels.get(identity_id))
        rx_seat = _riderxl_seat(group_id)
        # NOTE (review fix 2026-10-05): trust is set ONLY after every grant
        # minted successfully. Setting trust first left trust=True with no
        # backing grant if core.grant() raised mid-loop.
        grant_ids = []
        for scope in scopes:
            grant_obj = _core.grant(user_seat, rx_seat.agent_id, scope,
                                    ttl_seconds=_GRANT_TTL_SECONDS)
            gid = grant_obj["grant"]["grant_id"]
            grant_ids.append(gid)
            _stored_grants[gid] = grant_obj
        _core.set_trust(user_seat.agent_id, group_id, True)
        key = (user_seat.agent_id, rx_seat.agent_id)
        _grant_ids.setdefault(key, []).extend(grant_ids)
        return {"identity_id": identity_id, "user_id": user_seat.agent_id,
                "group_id": group_id, "scopes": list(scopes),
                "grant_ids": grant_ids, "grant_ttl_seconds": _GRANT_TTL_SECONDS}


def _real_revoke(identity_id, group_id):
    with _lock:
        user_seat = _user_seats.get(identity_id)
        rx_seat = _riderxl_seats.get(group_id)
        revoked = []
        if user_seat is not None and rx_seat is not None:
            key = (user_seat.agent_id, rx_seat.agent_id)
            for gid in _grant_ids.get(key, []):
                if _core.revoke(gid, user_seat):
                    revoked.append(gid)
            _grant_ids[key] = []
            _core.set_trust(user_seat.agent_id, group_id, False)
        return {"identity_id": identity_id, "group_id": group_id,
                "revoked_grant_ids": revoked}


def _real_is_trusted(identity_id, group_id=None):
    with _lock:
        user_seat = _user_seats.get(identity_id)
        if user_seat is None:
            return False
        if group_id is not None:
            return _core.is_trusted(user_seat.agent_id, group_id)
        return any(_core.is_trusted(user_seat.agent_id, gid)
                   for gid in _riderxl_seats)


def _real_check_access(identity_id, scope, group_id, action="read"):
    with _lock:
        user_seat = _user_seats.get(identity_id)
        rx_seat = _riderxl_seats.get(group_id)
        if user_seat is None or rx_seat is None:
            return False
        scope_base, _, _act = scope.partition(":")
        act = _act or action
        return _core.check_access(rx_seat.agent_id, user_seat.agent_id, act,
                                  scope_base)


# ---------------------------------------------------------------------------
# stub fallback (used only if identity.core cannot be imported)
# ---------------------------------------------------------------------------
def _stub_path():
    return os.path.join(_DATA_DIR, "trust.json")


def _stub_load() -> dict:
    try:
        with open(_stub_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _stub_save(reg: dict) -> None:
    # Review fix 2026-10-07: stub writes take the lock like everything else.
    with _lock:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _stub_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _stub_path())


# ---------------------------------------------------------------------------
# public surface: real when available, stub otherwise
# ---------------------------------------------------------------------------
if _REAL:
    def issue_group_seat(group_id, member_id=None, scopes=None):
        """Return (creating if needed) the RiderXL group seat for a chat group."""
        return _real_issue_group_seat(group_id, member_id, scopes)

    def issue_personal_seat(identity_id, label=None, scopes=None):
        """Return (creating if needed) the personal seat for a chat identity."""
        return _real_issue_personal_seat(identity_id, label)

    def grant(identity_id, scopes, group_id):
        """Trust + signed per-scope grants from the user to this group's RiderXL."""
        return _real_grant(identity_id, scopes, group_id)

    def revoke(identity_id, group_id):
        """Revoke the user's grants to this group's RiderXL and clear trust."""
        return _real_revoke(identity_id, group_id)

    def verify_grant(grant_obj) -> bool:
        """True iff the grant is authentic, sealed, fresh, and unrevoked."""
        return _core.verify_grant(grant_obj)

    def is_trusted(identity_id, group_id=None) -> bool:
        """Explicit opt-in flag (per group; any group if group_id is None)."""
        return _real_is_trusted(identity_id, group_id)

    def check_access(identity_id, scope, group_id, action="read") -> bool:
        """Full silo gate for one scope like 'chat:write' in a group."""
        return _real_check_access(identity_id, scope, group_id, action)

    def stored_grant(grant_id):
        """Lab helper: fetch a grant object this adapter minted (for verify_grant demos)."""
        with _lock:
            return _stored_grants.get(grant_id)

else:
    # Review fix 2026-10-07: the insecure stub fails LOUD. server.py
    # refuses to start at all unless RIDERXL_ALLOW_STUB=1 is set; this
    # warning fires on every import so silent fallback is impossible.
    import sys as _sys
    _sys.stderr.write(
        "!!! RiderXL PERMISSIONS STUB ACTIVE — identity.core unavailable.\n"
        "!!! Access control is a permissive in-memory stub. Set\n"
        "!!! RIDERXL_ALLOW_STUB=1 to acknowledge, or fix the import.\n")

    def issue_group_seat(group_id, member_id=None, scopes=None):
        return {"seat": "group", "group_id": group_id, "stub": True,
                "issued_ts": time.time()}

    def issue_personal_seat(identity_id, label=None, scopes=None):
        return {"seat": "personal", "identity_id": identity_id, "stub": True,
                "issued_ts": time.time()}

    def grant(identity_id, scopes, group_id=None):
        reg = _stub_load()
        entry = reg.get(identity_id, {"scopes": []})
        entry["scopes"] = sorted(set(entry.get("scopes", [])) | set(scopes))
        entry["granted_ts"] = time.time()
        reg[identity_id] = entry
        _stub_save(reg)
        return {"identity_id": identity_id, "scopes": entry["scopes"], "stub": True}

    def revoke(identity_id, group_id=None):
        reg = _stub_load()
        reg.pop(identity_id, None)
        _stub_save(reg)
        return {"identity_id": identity_id, "revoked": True, "stub": True}

    def verify_grant(grant_obj) -> bool:
        return bool(grant_obj)

    def is_trusted(identity_id, group_id=None) -> bool:
        return identity_id in _stub_load()

    def check_access(identity_id, scope, group_id=None, action="read") -> bool:
        scopes = _stub_load().get(identity_id, {}).get("scopes", [])
        return scope in scopes or "chat:*" in scopes

    def stored_grant(grant_id):
        return None
