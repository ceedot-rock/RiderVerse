# RiderXL — Identity + Permission Core: BUILD LOG

Worker 1 of 4 — lab-only group-chat agent prototype ("group-agent").
Workdir: `~/workspace/group-agent/identity/`

## Naming (LOCKED by Corey, lab owner, 2026-10-05)
The group agent is christened **RiderXL**. Name is LOCKED — used throughout:
code identifiers (`PRODUCT_NAME = "RiderXL"`), default group-seat label,
comments, docstrings, grant/seat schema naming, and this log. No other names
are proposed or used.

## What was built

| File | Contents |
|---|---|
| `core.py` | Single-module import surface (see API.md). Seats, grants, verification, revocation, trust registry, silo gate, append-only audit ledger (`audit.jsonl`). |
| `test_identity.py` | 28 unittest cases, all runnable with `python3 test_identity.py`. |
| `API.md` | Function reference + grant JSON schema for workers 2 and 3. |
| `audit.jsonl` | Runtime audit ledger (append-only; created on first run). |
| `BUILD_LOG.md` | This file. |

## Primitives reused (not reinvented)
- **Rider skill** (`~/workspace/skills/rider/SKILL.md`): identity = Ed25519
  agent identity with distinct seats. The skill's live flow needs network
  (bearer key → JWT → MCP), which this lab-only stream forbids — so the
  crypto layer is stubbed locally with PyNaCl 1.6.2 (already installed; the
  same lib the Chamber module uses). No `cryptography` package present, no
  new installs made.
- **Chamber** (`~/workspace/awlpay/server/chamber.py`): `core.py` imports the
  real module and reuses `sign_attestation`, `verify_attestation`,
  `canonical_json`, `key_id` — real envelope shape, real Ed25519.

## Chamber-seal substitution (documented in code, search `PROD_SWAP`)
The real signing key at `~/.config/slidphi-chamber/signing.key` (600) was
**not** touched and never printed/logged. Grants are Chamber-sealed with an
in-memory ephemeral Ed25519 key (`custody="ephemeral"` — Chamber's own
documented test mode). Production swap is one line:
`_chamber_signing_key()` → `chamber.load_signing_key()` with
`custody="file"` in `_seal_payload()`.

## Key design decisions
1. **Group seat ≠ personal seat**: `kind` field + id prefix (`g-` / `p-`);
   group seats carry `group_id`; RiderXL acts only under its own seat.
2. **Grantor-signed + Chamber-sealed**: every grant carries both the
   grantor's Ed25519 signature over the canonical payload AND a Chamber
   attestation envelope over the same bytes. `verify_grant()` requires both.
3. **Revocation is a signed record, not a flag flip**: `revoke()` requires the
   ORIGINAL grantor's seat signature; the grant_id lands on the revocation
   list that every `verify_grant()` / `check_access()` consults.
4. **Trust defaults to False**: `is_trusted()` is False until explicitly set;
   `link_personal_agent()` and `check_access()` both refuse untrusted users.
5. **Silo gate semantics**: `check_access(group_agent_id, user_id, action,
   scope)` requires permission string `f"{scope}:{action}"` (e.g.
   `action="read"`, `scope="chat"` → `"chat:read"`) to match a grant's scope
   EXACTLY. Unknown scopes are refused, never wildcarded.
6. **Exactness or refuse**: `grant()` raises `ValueError` on unknown scope,
   unknown grantee, non-personal grantor, or non-group grantee;
   `verify_grant()` returns `False` (never raises) on any malformed input.

## Test results — 2026-10-05 ~18:50 EDT
```
Ran 28 tests in 0.183s — OK (28 passed, 0 failed)
```
Coverage map:
- Seat issuance: distinct kinds/prefixes/keys, RiderXL label+group_id (1)
- Grant issuance + refusals (5): valid grant verifies; unknown scope,
  group-as-grantor, unknown grantee, personal-as-grantee all refused
- Verification failures (6): expired, tampered payload, wrong-key forgery,
  tampered Chamber seal, malformed input, unknown grantor
- **Revocation proof (3)**: `test_revoke_lifecycle_grant_allow_revoke_deny`
  proves grant → `check_access`=True → `revoke()` → `verify_grant`=False
  AND `check_access`=False; revocation requires the grantor's signature
  (guest1's and the group's revoke attempts fail, grant untouched)
- Silo enforcement (7): (a) no grant → denied; (b) wrong scope → denied;
  (c) expired → denied; (d) revoked → denied; (e) forged/tampered (incl.
  tampered grant sitting in the store) → denied; (f) user A's data under
  user B's grant → denied; plus trust-withdrawal, unknown ids, and
  unknown action/scope denials
- Trust registry (4): default False, set/link/unlink, per-group isolation,
  non-personal link refused

## No private material persisted
All keypairs are runtime-only (in-memory `SigningKey`s). Nothing was
committed; no key files written. `audit.jsonl` contains only public ids,
timestamps, and event names.

## Open items / handoff notes for workers 2 and 3
- `check_access()` is the single gate — worker 2 (chat plumbing) should call
  it before RiderXL reads/writes on any user's behalf.
- Trust records key on **personal seat agent_id**, not on the human label;
  worker 2 must thread the agent_id through its user model.
- Grant TTL default 3600s; pass `ttl_seconds` explicitly for longer-lived
  grants. Revocations are permanent (no un-revoke; issue a new grant).
- If workers need isolated state for tests, `core.reset()` clears all four
  registries; the classes (`SeatRegistry`, `GrantStore`, `RevocationList`,
  `TrustRegistry`) are importable for per-group instances.
