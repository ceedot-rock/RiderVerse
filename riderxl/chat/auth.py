"""chat/auth.py — proof-of-ownership caller auth for trust-mutating endpoints.

Review fix 2026-10-07 (BLOCKER): identity-scoped chat endpoints accepted a
client-supplied identity_id / member_id with NO proof of ownership, so a
revoke-spoof (POST /revoke with someone else's identity_id) was possible.

Every trust-mutating request must now carry three headers:

  X-Identity-Id  — the identity_id claiming to act
  X-Identity-Ts  — unix seconds; |now - ts| <= MAX_SKEW_S required
  X-Identity-Sig — base64 Ed25519 signature over the canonical message
                   METHOD + "\\n" + PATH + "\\n" + RAW_BODY + "\\n" + TS

verified against the Ed25519 public key the browser bound at join.
Key binding is FIRST-BIND-WINS and persisted in
chat/data/identity_keys.json (override with RIDERXL_IDENTITY_KEYS).

The browser key is NOT the seat key: seat private keys stay server-side in
identity/core.py. The browser generates its own Ed25519 pair via WebCrypto
at identity creation (see index.html), sends the pubkey at join, and signs
trust-mutating requests with the private key it holds.

Lab-only (localhost, mock money). No network calls.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
_DEFAULT_KEYS_PATH = os.path.join(DATA_DIR, "identity_keys.json")

MAX_SKEW_S = 300  # 5 minutes of clock skew allowed

_ID_RE = re.compile(r"[\w-]{4,64}")
_PUBKEY_RE = re.compile(r"[0-9a-fA-F]{64}")  # raw 32-byte Ed25519 key, hex


def _keys_path() -> str:
    return os.environ.get("RIDERXL_IDENTITY_KEYS", _DEFAULT_KEYS_PATH)


# identity_id -> {"pubkey_hex": str, "bound_ts": float}
_bound: dict[str, dict] = {}
_loaded_path: str | None = None


def _load() -> None:
    """(Re)load the key registry from disk. Idempotent per path."""
    global _loaded_path
    path = _keys_path()
    if _loaded_path == path:
        return
    _bound.clear()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for iid, rec in data.items():
                if (isinstance(rec, dict) and _ID_RE.fullmatch(iid)
                        and _PUBKEY_RE.fullmatch(rec.get("pubkey_hex", ""))):
                    _bound[iid] = rec
    except (OSError, ValueError):
        pass  # no registry yet — first bind creates it
    _loaded_path = path


def _save() -> None:
    os.makedirs(os.path.dirname(_keys_path()), exist_ok=True)
    tmp = _keys_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_bound, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _keys_path())


def bind(identity_id: str, pubkey_hex: str) -> bool:
    """Bind an Ed25519 pubkey to an identity_id (first-bind-wins).

    Returns True when the key is now bound to this identity (fresh bind,
    or the presented key matches the stored one). Returns False on a
    conflicting re-bind — the caller must reject the request (403).
    """
    _load()
    if not _ID_RE.fullmatch(identity_id or ""):
        return False
    if not _PUBKEY_RE.fullmatch(pubkey_hex or ""):
        return False
    pubkey_hex = pubkey_hex.lower()
    rec = _bound.get(identity_id)
    if rec is None:
        _bound[identity_id] = {"pubkey_hex": pubkey_hex,
                               "bound_ts": time.time()}
        _save()
        return True
    return rec["pubkey_hex"] == pubkey_hex


def bound_pubkey(identity_id: str) -> str | None:
    """Return the bound pubkey hex for an identity, or None if unbound."""
    _load()
    rec = _bound.get(identity_id)
    return rec["pubkey_hex"] if rec else None


def canonical_message(method: str, path: str, raw_body: bytes,
                      ts: str) -> bytes:
    return (method + "\n" + path + "\n" + raw_body.decode("utf-8")
            + "\n" + ts).encode("utf-8")


def verify(method: str, path: str, raw_body: bytes,
           headers) -> tuple[bool, str, str | None]:
    """Verify a signed request.

    headers: case-insensitive mapping (e.g. self.headers).
    Returns (ok, error, identity_id). error is "" when ok.
    """
    iid = (headers.get("X-Identity-Id") or "").strip()
    ts = (headers.get("X-Identity-Ts") or "").strip()
    sig_b64 = (headers.get("X-Identity-Sig") or "").strip()
    if not iid or not ts or not sig_b64:
        return False, "missing signature headers", None
    if not _ID_RE.fullmatch(iid):
        return False, "bad identity_id", None
    try:
        ts_i = int(ts)
    except ValueError:
        return False, "bad timestamp", None
    if abs(time.time() - ts_i) > MAX_SKEW_S:
        return False, "stale timestamp", None
    pubkey_hex = bound_pubkey(iid)
    if pubkey_hex is None:
        return False, "unknown identity (no bound key)", None
    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except (ValueError, base64.binascii.Error):
        return False, "bad signature encoding", None
    if len(sig) != 64:
        return False, "bad signature length", None
    msg = canonical_message(method, path, raw_body, ts)
    try:
        VerifyKey(bytes.fromhex(pubkey_hex)).verify(sig + msg)
    except BadSignatureError:
        return False, "bad signature", None
    return True, "", iid


def reset() -> None:
    """Clear in-memory registry (tests only; does not delete the file)."""
    global _loaded_path
    _bound.clear()
    _loaded_path = None
