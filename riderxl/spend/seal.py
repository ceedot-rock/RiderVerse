"""Chamber-sealed mock receipts — RiderXL lab prototype (lab-only).

SEALING DECISION (documented per task):
  Uses the envelope FORMAT and sign/verify code from
  ~/workspace/awlpay/server/chamber.py (sign_attestation,
  verify_attestation, canonical JSON, Ed25519) — the format fits, so no
  stand-in crypto was invented.

  Does NOT use the production chamber signing key at
  ~/.config/slidphi-chamber/signing.key. Mock receipts are sealed with
  keys generated FRESH AT RUNTIME, stamped custody="lab-local-mock".
  Reason: a MOCK receipt must never be verifiable under the production
  chamber key — that would let a lab toy pass as a real attestation.
  "No secrets committed": test keys live only in process memory.

Envelope shape (chamber-compatible):
  {"alg","kid","custody","payload","sig"} + "verify_key_hex" so any
  holder of the receipt JSON can verify the seal with no key lookup.
"""

from __future__ import annotations

import importlib.util
import os

_CHAMBER_PATH = os.path.expanduser("~/workspace/awlpay/server/chamber.py")

_spec = importlib.util.spec_from_file_location("awlpay_chamber", _CHAMBER_PATH)
_chamber = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_chamber)  # type: ignore[union-attr]

sign_attestation = _chamber.sign_attestation
verify_attestation = _chamber.verify_attestation
key_id = _chamber.key_id
canonical_json = _chamber.canonical_json

from nacl.signing import SigningKey, VerifyKey  # noqa: E402

CUSTODY_LABEL = "lab-local-mock"


class SealAuthority:
    """Holds one runtime-generated Ed25519 key and seals/verifies."""

    def __init__(self, signing_key: SigningKey | None = None):
        self._sk = signing_key or SigningKey.generate()
        self.verify_key: VerifyKey = self._sk.verify_key
        self.kid: str = key_id(self.verify_key)
        self.custody = CUSTODY_LABEL

    @property
    def verify_key_hex(self) -> str:
        return self.verify_key.encode().hex()

    def seal(self, payload: dict) -> dict:
        """Seal a payload dict. Returns the chamber-format envelope plus
        verify_key_hex for standalone verification.

        NOTE 2026-10-07: upstream sign_attestation() no longer takes a
        custody kwarg — custody is stamped onto the envelope after signing.
        """
        env = sign_attestation(payload, self._sk)
        env["custody"] = self.custody
        env["verify_key_hex"] = self.verify_key_hex
        return env

    def verify(self, envelope: dict) -> bool:
        """True iff the envelope verifies under THIS authority's key."""
        return verify_attestation(envelope, self.verify_key)


def verify_with_key_hex(envelope: dict, verify_key_hex: str) -> bool:
    """Verify an envelope against an embedded/explicit verify key hex.
    Never raises — False on any problem.

    TRUST CAVEAT (review fix 2026-10-07, documented loudly per KIMI):
    this verifies the seal against the key EMBEDDED IN THE RECEIPT ITSELF.
    A self-signed forgery therefore verifies True — the signature proves
    the receipt is internally consistent, NOT that it came from a trusted
    sealer. Real trust = key distribution: only treat a receipt as
    authoritative if its verify_key_hex matches a key you obtained
    out-of-band (e.g. this process's SealAuthority.verify_key_hex, or an
    allow-listed kid). For the lab demo the receipts are MOCK anyway
    (custody="lab-local-mock"), so this caveat changes nothing today —
    but any non-mock use MUST check the key against an allow-list first.
    """
    try:
        vk = VerifyKey(bytes.fromhex(verify_key_hex))
    except (ValueError, TypeError):
        return False
    return verify_attestation(envelope, vk)
