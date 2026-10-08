"""awLPay attestation envelope: signed receipts (REAL Ed25519).

=====================================================================
KEY CUSTODY — READ THIS BEFORE PRODUCTION
---------------------------------------------------------------------
v1 key handling is STUBBED. load_relayer_keys() takes the relayer
signing seed from the AWL_RELAYER_KEY env var (64 hex chars = 32-byte
seed). If unset, an EPHEMERAL test key is generated per process — fine
for local dev, meaningless for trust.

PRODUCTION TODO: integrate Chamber HSM custody — the seed must never
exist as a process env var; signing must happen inside the Chamber with
key_id references only. Nothing here authorizes real custody.
=====================================================================

Envelope shape:
    {"alg": "ed25519",
     "kid": key_id,
     "payload": canonical_json,   # json.dumps(sort_keys=True, separators=(",",":"))
     "sig": hex_signature}

verify_attestation(env, verify_key) -> bool: False on ANY tampering,
missing field, or bad signature — never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

ALG = "ed25519"


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def key_id(verify_key: VerifyKey) -> str:
    """Stable key id: first 16 hex chars of sha256(raw public key)."""
    return hashlib.sha256(bytes(verify_key)).hexdigest()[:16]


def sign_attestation(payload: dict, signing_key: SigningKey) -> dict:
    """Sign a payload dict; returns the full envelope dict."""
    verify_key = signing_key.verify_key
    canon = canonical_json(payload)
    sig = signing_key.sign(canon.encode("utf-8")).signature
    return {
        "alg": ALG,
        "kid": key_id(verify_key),
        "payload": canon,
        "sig": sig.hex(),
    }


def verify_attestation(env: dict, verify_key: VerifyKey) -> bool:
    """True iff the envelope is well-formed, kid matches, and the
    signature verifies over the payload bytes. False for everything
    else — never raises."""
    try:
        if not isinstance(env, dict):
            return False
        if env.get("alg") != ALG:
            return False
        if env.get("kid") != key_id(verify_key):
            return False
        payload = env.get("payload")
        sig_hex = env.get("sig")
        if not isinstance(payload, str) or not isinstance(sig_hex, str):
            return False
        verify_key.verify(payload.encode("utf-8"), bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def load_relayer_keys() -> tuple[SigningKey, VerifyKey, str]:
    """(signing_key, verify_key, source) where source is 'env' or
    'ephemeral'. See the custody warning above."""
    seed_hex = os.environ.get("AWL_RELAYER_KEY", "").strip()
    if seed_hex:
        seed = bytes.fromhex(seed_hex)  # ValueError -> loud crash, by design
        if len(seed) != 32:
            raise SystemExit("AWL_RELAYER_KEY must be 64 hex chars (32-byte seed)")
        sk = SigningKey(seed)
        return sk, sk.verify_key, "env"
    sk = SigningKey.generate()
    print("chamber: AWL_RELAYER_KEY unset — EPHEMERAL test key in use "
          "(no production trust)", file=sys.stderr, flush=True)
    return sk, sk.verify_key, "ephemeral"
