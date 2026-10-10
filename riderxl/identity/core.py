"""identity/core.py — RiderXL identity + permission core (lab-only).

RiderXL is the lab's group agent: its OWN Rider-style identity,
permission-first linking, and revocation that actually cuts access. This
inverts the perpetual-license model: user-owned data + explicit consent,
checked on every touch.

Primitive layer (from the lab, reused not reinvented):
  * Ed25519 seats via PyNaCl (same lib as awlpay/server/chamber.py).
  * Chamber attestation envelopes: canonical JSON + Ed25519, imported from
    ~/workspace/awlpay/server/chamber.py (sign_attestation /
    verify_attestation / key_id / canonical_json).

Chamber-seal substitution (documented): production seals with the real key
at ~/.config/slidphi-chamber/signing.key via chamber.load_signing_key().
This lab prototype seals with an IN-MEMORY ephemeral key
(custody="ephemeral" — chamber's own documented test mode), so no real key
material is touched and no test keys are persisted. One line to swap for
the production path; search for PROD_SWAP below.

Single-module import surface for workers 2 and 3:
    issue_group_seat(), issue_personal_seat(), grant(), revoke(),
    verify_grant(), set_trust(), is_trusted(), link_personal_agent(),
    check_access()

No network. No persisted private keys. Nothing leaves this module's memory
except verify (public) keys.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from nacl.signing import SigningKey
from nacl.exceptions import BadSignatureError

# ---------------------------------------------------------------------------
# Chamber envelope primitives (reused from the lab's sealed module)
# ---------------------------------------------------------------------------

# Vendored from the lab's public awlpay repo (server/chamber.py) — pure
# envelope primitives (canonical_json / sign_attestation / verify_attestation).
# No keys, no custody: RiderXL mints its own ephemeral in-process seal key.
# If upstream changes the envelope format, re-vendor and re-run the suite.
_CHAMBER_SRC = os.environ.get(
    "RIDERXL_CHAMBER_SRC",
    os.path.expanduser("~/workspace/awlpay/server/chamber.py"),
)


def _load_chamber():
    spec = importlib.util.spec_from_file_location("chamber", _CHAMBER_SRC)
    if spec is None or spec.loader is None:
        raise SystemExit(f"identity: cannot load chamber module at {_CHAMBER_SRC}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except OSError as e:
        raise SystemExit(f"identity: cannot load chamber module at {_CHAMBER_SRC}: {e}")
    return mod


chamber = _load_chamber()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Product name, LOCKED by Corey (lab owner), 2026-10-05.
PRODUCT_NAME = "RiderXL"

#: Known permission scopes. grant() refuses anything outside this set.
SCOPES = frozenset({"chat:read", "chat:write", "spend:propose", "spend:approve"})

DEFAULT_GRANT_TTL_SECONDS = 3600  # 1 hour
DEFAULT_REVOKED_TTL_SECONDS = 0   # revocations are permanent

_AUDIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.jsonl")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Chamber seal key (ephemeral, in-memory; see module docstring for PROD_SWAP)
# ---------------------------------------------------------------------------

_CHAMBER_SK: SigningKey | None = None


def _chamber_signing_key() -> SigningKey:
    """Ephemeral in-process seal key.

    PROD_SWAP: replace with:
        sk, vk, custody = chamber.load_signing_key()  # real file key
    and pass custody="file" in grant(). Everything else stays identical.
    """
    global _CHAMBER_SK
    if _CHAMBER_SK is None:
        _CHAMBER_SK = SigningKey.generate()
    return _CHAMBER_SK


def _seal_payload(payload: dict) -> dict:
    """Chamber-seal a grant payload (envelope over canonical JSON).

    NOTE 2026-10-07: upstream chamber.sign_attestation() no longer takes a
    custody kwarg — custody is stamped onto the envelope after signing.
    verify_attestation() ignores extra envelope keys, so this stays valid.
    """
    sk = _chamber_signing_key()
    env = chamber.sign_attestation(payload, sk)
    env["custody"] = "ephemeral"
    return env


def _seal_valid(seal: dict, payload: dict) -> bool:
    """True iff the seal is a well-formed chamber envelope for this payload."""
    sk = _chamber_signing_key()
    vk = sk.verify_key
    canon = chamber.canonical_json(payload)
    if not isinstance(seal, dict) or seal.get("payload") != canon:
        return False
    return chamber.verify_attestation(seal, vk)


# ---------------------------------------------------------------------------
# Seats: group-agent identity vs personal seats
# ---------------------------------------------------------------------------

@dataclass
class Seat:
    agent_id: str          # "g-<hex>" group seat | "p-<hex>" personal seat
    kind: str              # "group" | "personal"
    label: str             # human handle, e.g. "riderxl" / "corey"
    group_id: str | None   # only set on group seats
    signing_key: SigningKey

    @property
    def verify_key(self):
        return self.signing_key.verify_key

    @property
    def verify_key_hex(self) -> str:
        return bytes(self.signing_key.verify_key).hex()

    def public(self) -> dict:
        """The publishable seat record — public key only, never the seed."""
        return {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "label": self.label,
            "group_id": self.group_id,
            "verify_key_hex": self.verify_key_hex,
        }


# Review fix 2026-10-07: one RLock guarding every core store mutation,
# so concurrent grant/revoke/trust paths can't interleave mid-write.
_STORE_LOCK = threading.RLock()


class SeatRegistry:
    def __init__(self):
        self._seats: dict[str, Seat] = {}

    def add(self, seat: Seat) -> None:
        with _STORE_LOCK:
            self._seats[seat.agent_id] = seat

    def get(self, agent_id: str) -> Seat | None:
        return self._seats.get(agent_id)

    def clear(self) -> None:
        with _STORE_LOCK:
            self._seats.clear()


# ---------------------------------------------------------------------------
# Grant store + revocation list + trust registry
# ---------------------------------------------------------------------------

class GrantStore:
    """grant_id -> grant dict. Grants are sealed data; the store is dumb."""

    def __init__(self):
        self._grants: dict[str, dict] = {}

    def put(self, grant: dict) -> None:
        # Review fix 2026-10-07: refuse on collision instead of silently
        # overwriting (ids are 128-bit, so this should never trigger).
        gid = grant["grant"]["grant_id"]
        with _STORE_LOCK:
            if gid in self._grants:
                raise ValueError("identity: duplicate grant_id %r" % (gid,))
            self._grants[gid] = grant

    def get(self, grant_id: str) -> dict | None:
        return self._grants.get(grant_id)

    def for_grantee(self, grantor_id: str, grantee_id: str):
        for g in self._grants.values():
            p = g.get("grant", {})
            if p.get("grantor") == grantor_id and p.get("grantee") == grantee_id:
                yield g

    def clear(self) -> None:
        with _STORE_LOCK:
            self._grants.clear()


class RevocationList:
    """grant_id -> revocation record. Every access check consults this."""

    def __init__(self):
        self._revoked: dict[str, dict] = {}

    def add(self, record: dict) -> None:
        with _STORE_LOCK:
            self._revoked[record["grant_id"]] = record

    def is_revoked(self, grant_id: str) -> bool:
        return grant_id in self._revoked

    def record(self, grant_id: str) -> dict | None:
        return self._revoked.get(grant_id)

    def clear(self) -> None:
        with _STORE_LOCK:
            self._revoked.clear()


class TrustRegistry:
    """Per-user per-group trust: {user_id: {group_id: bool}}.

    Default is False (distrust). The group agent must see trusted=True
    before linking a user's personal agent.
    """

    def __init__(self):
        self._trust: dict[str, dict[str, bool]] = {}

    def set(self, user_id: str, group_id: str, trusted: bool) -> None:
        with _STORE_LOCK:
            self._trust.setdefault(user_id, {})[group_id] = bool(trusted)

    def get(self, user_id: str, group_id: str) -> bool:
        return bool(self._trust.get(user_id, {}).get(group_id, False))

    def clear(self) -> None:
        with _STORE_LOCK:
            self._trust.clear()


# Module singletons (the simple import surface hangs off these; workers can
# instantiate their own registries for isolation if needed).
SEATS = SeatRegistry()
GRANTS = GrantStore()
REVOKED = RevocationList()
TRUST = TrustRegistry()


def reset() -> None:
    """Clear all identity state. Test-only."""
    # Review fix 2026-10-07: also drop the ephemeral Chamber key, so a
    # reset truly returns to a blank slate (no grant sealed before the
    # reset verifies after it).
    global _CHAMBER_SK
    SEATS.clear()
    GRANTS.clear()
    REVOKED.clear()
    TRUST.clear()
    _CHAMBER_SK = None


# ---------------------------------------------------------------------------
# Audit ledger (append-only JSONL; lab bookkeeping, not consensus)
# ---------------------------------------------------------------------------

def audit(event: str, detail: dict) -> None:
    try:
        with open(_AUDIT_PATH, "a") as f:
            f.write(json.dumps({"ts": _ts(), "event": event, "detail": detail}) + "\n")
    except OSError as e:
        # Review fix 2026-10-07: auditing still never breaks the permission
        # path, but disk-full failures are no longer invisible.
        sys.stderr.write("identity: audit write failed: %s\n" % (e,))


# ---------------------------------------------------------------------------
# Seat issuance
# ---------------------------------------------------------------------------

def _new_id(prefix: str) -> str:
    # Review fix 2026-10-07: 128-bit ids (was 64-bit).
    return f"{prefix}-{secrets.token_hex(16)}"


def issue_group_seat(label: str = PRODUCT_NAME, group_id: str = "riderxl") -> Seat:
    """Mint a distinct RiderXL (group-agent) identity: own keypair, own agent_id.

    A RiderXL seat is never a personal seat: kind="group", id prefix "g-",
    and it carries a group_id. RiderXL acts ONLY under this seat.
    """
    seat = Seat(agent_id=_new_id("g"), kind="group", label=label,
                group_id=group_id, signing_key=SigningKey.generate())
    SEATS.add(seat)
    audit("seat.issued", {"kind": "group", "agent_id": seat.agent_id,
                          "label": label, "group_id": group_id})
    return seat


def issue_personal_seat(label: str) -> Seat:
    """Mint a lightweight personal seat stand-in for a test user.

    kind="personal", id prefix "p-", no group_id. This is the identity that
    signs permission grants to RiderXL; the user's private key never leaves
    this Seat.
    """
    seat = Seat(agent_id=_new_id("p"), kind="personal", label=label,
                group_id=None, signing_key=SigningKey.generate())
    SEATS.add(seat)
    audit("seat.issued", {"kind": "personal", "agent_id": seat.agent_id,
                          "label": label})
    return seat


def get_seat(agent_id: str) -> Seat | None:
    return SEATS.get(agent_id)


# ---------------------------------------------------------------------------
# Permission grants: personal agent -> group agent
# ---------------------------------------------------------------------------

def grant(grantor_seat: Seat, grantee_agent_id: str, scope: str,
          ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS) -> dict:
    """Issue a signed, Chamber-sealed permission grant.

    grantor_seat : the PERSONAL seat signing (user's private key used here,
                   never stored or logged — only the signature leaves).
    grantee_agent_id : the group agent's seat id.
    scope : one of SCOPES, e.g. "chat:read".
    ttl_seconds : grant lifetime. Negative allowed ONLY when the
    RIDERXL_ALLOW_NEGATIVE_TTL=1 test flag is set.

    Returns the grant dict (see API.md for the JSON schema).
    Refuses: unknown scope, unknown grantee, non-personal grantor,
    non-group grantee, negative ttl without the test flag.
    """
    # Review fix 2026-10-07: negative TTL is a test-only escape hatch.
    if ttl_seconds < 0 and os.environ.get("RIDERXL_ALLOW_NEGATIVE_TTL") != "1":
        raise ValueError("identity: negative ttl_seconds requires "
                         "RIDERXL_ALLOW_NEGATIVE_TTL=1 (tests only)")
    if scope not in SCOPES:
        raise ValueError(f"identity: unknown scope {scope!r}; known: {sorted(SCOPES)}")
    if grantor_seat.kind != "personal":
        raise ValueError("identity: only a personal seat can grant")
    grantee = SEATS.get(grantee_agent_id)
    if grantee is None:
        raise ValueError(f"identity: unknown grantee {grantee_agent_id!r}")
    if grantee.kind != "group":
        raise ValueError("identity: grants may only name a group seat as grantee")

    issued = _now()
    expires = datetime.fromtimestamp(issued.timestamp() + ttl_seconds, tz=timezone.utc)
    payload = {
        "grant_id": _new_id("gr"),
        "grantor": grantor_seat.agent_id,
        "grantee": grantee_agent_id,
        "scope": scope,
        "issued_at": _ts(issued),
        "expires_at": _ts(expires),
        "nonce": secrets.token_hex(16),
    }
    canon = chamber.canonical_json(payload)
    grantor_sig = grantor_seat.signing_key.sign(canon.encode("utf-8")).signature.hex()
    grant_obj = {
        "grant": payload,
        "grantor_sig": grantor_sig,
        "seal": _seal_payload(payload),
    }
    GRANTS.put(grant_obj)
    audit("grant.issued", {"grant_id": payload["grant_id"],
                           "grantor": payload["grantor"],
                           "grantee": grantee_agent_id, "scope": scope})
    return json.loads(json.dumps(grant_obj))  # deep copy out


def _grant_structure_ok(grant_obj: dict) -> dict | None:
    """Return the payload dict if the grant is well-formed, else None."""
    if not isinstance(grant_obj, dict):
        return None
    payload = grant_obj.get("grant")
    sig = grant_obj.get("grantor_sig")
    seal = grant_obj.get("seal")
    if not isinstance(payload, dict) or not isinstance(sig, str) \
            or not isinstance(seal, dict):
        return None
    for f in ("grant_id", "grantor", "grantee", "scope",
              "issued_at", "expires_at", "nonce"):
        if f not in payload:
            return None
    return payload


def verify_grant(grant_obj: dict) -> bool:
    """True iff the grant is authentic, fresh, sealed, and unrevoked.

    Checks, in order: well-formed structure; grantor is a known seat;
    grantor Ed25519 signature verifies over the canonical payload;
    Chamber seal verifies over the same payload; issued_at <= now <=
    expires_at; grant_id not on the revocation list. False on ANY failure,
    never raises.
    """
    try:
        payload = _grant_structure_ok(grant_obj)
        if payload is None:
            return False
        grantor = SEATS.get(payload["grantor"])
        # Review fix 2026-10-07: assert seat kinds, so callers using
        # verify_grant() alone can't be misled by a well-formed grant
        # between two group seats or two personal seats.
        if grantor is None or grantor.kind != "personal":
            return False
        grantee = SEATS.get(payload["grantee"])
        if grantee is None or grantee.kind != "group":
            return False
        # 1. grantor signature
        canon = chamber.canonical_json(payload).encode("utf-8")
        try:
            grantor.verify_key.verify(canon, bytes.fromhex(grant_obj["grantor_sig"]))
        except (BadSignatureError, ValueError):
            return False
        # 2. Chamber seal over the same payload
        if not _seal_valid(grant_obj["seal"], payload):
            return False
        # 3. freshness window
        now = _now()
        if not (_parse_ts(payload["issued_at"]) <= now <= _parse_ts(payload["expires_at"])):
            return False
        # 4. revocation
        if REVOKED.is_revoked(payload["grant_id"]):
            return False
        return True
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def revoke(grant_id: str, grantor_seat: Seat) -> bool:
    """Revoke a grant. Only the ORIGINAL grantor's seat can revoke it.

    The revocation is itself signed by the grantor and added to the
    revocation list; every access check consults it, so revocation cuts
    access immediately. Returns True on success, False if the grant is
    unknown or the signer is not the grant's grantor.
    """
    grant_obj = GRANTS.get(grant_id)
    if grant_obj is None:
        audit("revoke.failed", {"grant_id": grant_id, "reason": "unknown-grant"})
        return False
    payload = grant_obj["grant"]
    if grantor_seat.agent_id != payload["grantor"]:
        audit("revoke.failed", {"grant_id": grant_id, "reason": "not-grantor",
                                "signer": grantor_seat.agent_id})
        return False
    record_payload = {"grant_id": grant_id, "revoked_at": _ts(),
                      "grantor": grantor_seat.agent_id}
    canon = chamber.canonical_json(record_payload).encode("utf-8")
    sig = grantor_seat.signing_key.sign(canon).signature.hex()
    # verify-before-store: never trust our own signing path blindly
    try:
        grantor_seat.verify_key.verify(canon, bytes.fromhex(sig))
    except (BadSignatureError, ValueError):
        return False
    REVOKED.add({"grant_id": grant_id, "revoked_at": record_payload["revoked_at"],
                 "grantor": grantor_seat.agent_id, "grantor_sig": sig})
    audit("grant.revoked", {"grant_id": grant_id, "grantor": grantor_seat.agent_id})
    return True


# ---------------------------------------------------------------------------
# Trust registry: per-user per-group trust, checked before linking
# ---------------------------------------------------------------------------

def set_trust(user_id: str, group_id: str, trusted: bool) -> None:
    """Record whether a user trusts a group. Default (unset) is False."""
    TRUST.set(user_id, group_id, trusted)
    audit("trust.set", {"user_id": user_id, "group_id": group_id,
                        "trusted": bool(trusted)})


def is_trusted(user_id: str, group_id: str) -> bool:
    """True iff the user has explicitly marked this group trusted."""
    return TRUST.get(user_id, group_id)


def link_personal_agent(group_id: str, user_seat: Seat) -> bool:
    """Link a user's personal agent to RiderXL's group — only if trusted.

    RiderXL calls this before it ever touches the user's data.
    Returns True and records the link; False refuses (untrusted).
    """
    if user_seat.kind != "personal":
        return False
    if not is_trusted(user_seat.agent_id, group_id):
        audit("link.refused", {"user_id": user_seat.agent_id, "group_id": group_id,
                               "reason": "untrusted"})
        return False
    audit("link.ok", {"user_id": user_seat.agent_id, "group_id": group_id})
    return True


# ---------------------------------------------------------------------------
# Silo enforcement: the single gate before touching personal data
# ---------------------------------------------------------------------------

def check_access(group_agent_id: str, user_id: str, action: str, scope: str) -> bool:
    """Silo gate — the single check RiderXL runs before touching personal data.

    True ONLY if a valid, unexpired, unrevoked grant covers
    (user_id, action, scope) for this RiderXL agent.

    required permission = f"{scope}:{action}", e.g. action="read",
    scope="chat" -> "chat:read". Checks: group seat exists and is a group;
    user seat exists and is personal; user trusts the group; at least one
    stored grant from this user to this group agent passes verify_grant()
    (signature + seal + freshness + revocation) and names the required
    scope exactly.
    """
    group = SEATS.get(group_agent_id)
    user = SEATS.get(user_id)
    if group is None or group.kind != "group":
        return False
    if user is None or user.kind != "personal":
        return False
    if group.group_id is None or not is_trusted(user_id, group.group_id):
        return False
    required = f"{scope}:{action}"
    if required not in SCOPES:
        return False
    for g in GRANTS.for_grantee(user_id, group_agent_id):
        if not verify_grant(g):
            continue
        if g["grant"]["scope"] == required:
            audit("access.allowed", {"group": group_agent_id, "user": user_id,
                                     "required": required})
            return True
    audit("access.denied", {"group": group_agent_id, "user": user_id,
                            "required": required})
    return False


def known_scopes() -> list:
    return sorted(SCOPES)
