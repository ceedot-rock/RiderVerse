# RiderXL — Findings Ledger

**Standing rule (Corey, 2026-10-07):** anything that is a downfall, a
detriment, or marked incorrect goes in here. We work items off the top
(severity order) until the ledger is empty or the remainder is accepted
risk with Corey's explicit call.

Source: KIMI security+correctness review 2026-10-05 (`KIMI_REVIEW.md`),
plus fixes landed since. Status as of 2026-10-07 ~19:45 EDT.

## Fixed (verified)

- [x] **BLOCKER** chat: no caller auth on trust-mutating endpoints → Ed25519
  proof-of-ownership (`chat/auth.py`, `_require_sig` on /messages, /grant,
  /deny, /revoke, task approve/deny). Adversarial probe: unsigned → 401,
  cross-key → 401, owner-signed → 200. (2026-10-07)
- [x] **BLOCKER** spend: `Proposal` mutable, direct `status="APPROVED"` bypass
  → frozen after construction (`__setattr__` guard); `verify_receipt()`
  compares full audit trail. (2026-10-07)
- [x] **MAJOR** chat: trust-before-grant ordering in `_real_grant()` → trust
  set only after all grants mint. (2026-10-05)
- [x] **MINOR** chat: `?since=` garbage → 500 → now 400. (2026-10-05)

## Open — BLOCKER

(none — both KIMI blockers fixed and adversarially verified 2026-10-07)

- [x] **permissions stub fails open silently** → fail-closed: server refuses
  to start unless `RIDERXL_ALLOW_STUB=1`; loud stderr warning on every
  stub import. (Already landed 2026-10-07; verified in code.)

## Open — MAJOR

(none — all fixed and verified 2026-10-07)

- [x] **Approvals rode on `chat:write`** → dedicated `spend:approve` scope;
  requester can never self-approve. Scope separation verified live:
  `chat:write` grant does NOT confer `spend:approve`.
- [x] **`ProposalStore` permissive default** → permissions callback is now
  REQUIRED; `ProposalStore()` without one raises. Demo/tests pass explicit
  callbacks (visible opt-in, no silent default).
- [x] **No locks on shared stores** → `threading.Lock` on `ProposalStore`
  (propose/get/vote check-then-act) and on `GroupSpendService.execute()`
  (guard-check → execute → mark); permissions singletons already under
  `RLock`; chat `groups` dict already under `_groups_lock`.
- [x] **`mock_awlpay` broad `except Exception`** → catches only
  `ImportError`/`ModuleNotFoundError`/`FileNotFoundError`; fallback now
  logs loudly to stderr. A real fees.py bug can no longer drift fee math
  silently.
- [x] **`_is_spend_like` substring matching** → word-boundary regexes
  ("bookkeeper" no longer triggers). Documented limit: keyword lists can't
  stop deliberate obfuscation — the hard guarantee is nothing acts without
  approval.

## Open — MINOR

- [x] identity: `verify_grant()` seat-kind assertions (grantor personal /
  grantee group).
- [x] identity: 128-bit ids; `GrantStore.put()` refuses on collision.
- [x] identity: `audit()` OSError → stderr (permission path never breaks).
- [x] identity: `reset()` clears ephemeral Chamber key.
- [x] identity: negative `ttl_seconds` rejected without
  `RIDERXL_ALLOW_NEGATIVE_TTL=1`.
- [x] permissions: ignored `scopes`/`member_id` params dropped from
  `issue_group_seat`/`issue_personal_seat` (scopes live on grants).
- [x] permissions: `_stub_save()` takes the lock.
- [x] server: `/api/debug/*` gated behind `RIDERXL_DEBUG=1` (default off,
  404 otherwise).
- [x] server: random task ids (`task-` + 16 hex chars).
- [x] service: atomic receipt write (tmp+fsync+replace).
- [x] service: embedded-key trust caveat documented loudly on
  `verify_with_key_hex` (self-signed forgery verifies True; trust =
  out-of-band key distribution).
- [ ] frontend hardening: server stores/serves raw text over JSON — the
  shipped frontend escapes everything, but any future API client must
  escape before rendering. Left open as a documented hardening note;
  no stored-XSS path in the shipped client.

## Accepted risk (lab-demo envelope only)

- Replay within the 5-minute timestamp skew window on signed chat requests
  (no nonces). Bounded, documented in `chat/auth.py`.
- `REVOKED` in-memory store: revocation signatures not re-verified on read.
- `client.execute()` runs before `p.executed = True` (retryable mock tx on
  seal/write failure).
