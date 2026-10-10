# RiderXL group-chat agent prototype — BUILD REPORT

Lab-only prototype (localhost, mock money). Product name **RiderXL** (LOCKED by Corey, 2026-10-05). Workdir: `~/workspace/group-agent/`. Four streams; consolidated 2026-10-05 ~21:15 EDT.

## Stream 1 — Identity + permission core (`identity/`)
- `core.py`: Ed25519 seats (PyNaCl, same lib as Chamber), dual-signed grants (grantor sig over canonical JSON + Chamber attestation envelope with ephemeral in-memory key, `custody="ephemeral"`), signed revocation records, per-user per-group trust registry (default False), `check_access()` silo gate requiring exact `f"{scope}:{action}"` match on a valid/unexpired/unrevoked/sealed grant. Audit ledger `audit.jsonl` (public ids only). No private keys persisted.
- `test_identity.py`: **28/28 passing** (re-verified by Worker 4).
- Design: group seat (`g-`) ≠ personal seat (`p-`); revocations permanent; one-line PROD_SWAP to the real chamber key documented.

## Stream 2 — Group-chat server + agent + frontend (`chat/`)
- `server.py`: stdlib `ThreadingHTTPServer`, in-memory groups with per-group JSON persistence (atomic tmp+fsync+replace). Endpoints: create/list group, join (member/guest), post, poll `?since=`, grant/deny/revoke trust, approve/deny tasks, per-group trust query, lab-only debug endpoints. Permission-first dispatch: RiderXL processes a message only if `check_access(identity_id, "chat:write", group_id)` passes.
- `agent.py`: scripted local responder (no network; documented LLM plug-in point), `@agent` task cards (trip plan, bill split with `Name amount` parsing), anti-Instinct rule — anything spend-like posts an APPROVAL card and waits; approval only posts a confirmation, nothing wired to act.
- `index.html`: single-page vanilla JS; two identities via `?as=` tabs; permission/approval/task cards; trust badge + revoke button; all user-controlled values escaped via `textContent`/`esc()` (XSS-clean).
- 7-day grant TTL (demo choice); adapter falls back to an in-memory stub if `identity.core` can't import (flagged by review).
- Smoke tests: 27/27 checks passed per builder; re-verified by Worker 4 on a fresh instance (port 18088) including 403/409 paths and revoke-cutting-access.

## Stream 3 — Spend governance (`spend/`)
- `mock_awlpay.py`: MOCK quote/execute; fee math imports the REAL `~/workspace/awlpay/server/fees.py` `calculate_fees()` with `_FEE_SOURCE` recorded per quote; every dict carries `mode: "MOCK"`, `network: "none"`, MOCK labels, `mock-tx-` ids.
- `approve.py`: proposal + voting, `majority` (default, with mathematical-elimination death) and `unanimous` policies; rejects non-member/double/post-close/no-grant votes via injected `permissions(member, "spend:approve")` callback (default permissive stub — flagged).
- `seal.py`: Chamber-format envelopes, runtime-generated keys, `custody="lab-local-mock"`; production chamber key never touched.
- `service.py`: propose → vote → threshold → mock-execute → sealed receipt JSON on disk. `execute()` raises `SpendRefused` unless APPROVED and refuses double-execute; `verify_receipt()` checks envelope sig + body/sealed-payload consistency (5 fields — review says expand).
- `tests/`: **55/55 passing** (re-verified by Worker 4). `demo.py` runs all three scenarios; `samples/` holds 3 labeled mock receipts.

## Stream 4 — Kimi security + correctness review (this report)
- Full review at `KIMI_REVIEW.md`. Kimi verdicts: identity NEEDS-FIXES, chat UNSOUND (under malicious-local-member threat model — unauthenticated identity-scoped endpoints), spend NEEDS-FIXES. My independent probes confirmed the revoke-spoofing attack and the XSS-clean frontend; test counts re-verified (28/28, 55/55).
- **Fixed (trivial + safe):** trust-before-grant ordering in `chat/permissions.py`; `?since=` 500→400 in `chat/server.py`. Both re-verified end-to-end.
- **Reported (need design calls):** chat endpoints accept client-supplied `identity_id`/`member_id` with no caller auth; spend `Proposal` mutable (threshold bypassable by direct attribute write); permissions stub fails open silently; approvals ride on `chat:write` not `spend:approve`; missing locks on shared stores; silent fee-fallback; incomplete `verify_receipt` field checks.
- Secrets scan: clean — no private keys/credentials committed; production chamber key untouched. Product naming: RiderXL consistent, no competing names.

## Overall verdict: GO-WITH-CAVEATS for the lab demo
Within the lab envelope (localhost only, Corey operating, mock money, honest MOCK labels everywhere), the prototype is demo-ready: crypto core sound, spend gate enforced on module paths, revocation cuts access, frontend XSS-clean, all tests green. The caveats above are blockers only before any network exposure or real-money wiring.

## Finish pass 2026-10-07 — Kimi review punch list closed

All 8 fix-list items implemented and re-verified. **115/115 tests green**
(identity 29/29, spend 60/60, chat 26/26 — includes 6 new tests added this
pass: duplicate-grant refusal, 5× Proposal immutability; plus the new
`chat/tests/test_auth.py` with 26 caller-auth/adversarial tests).

### Baseline repair (pre-existing drift, not from the review)
- Upstream `~/workspace/awlpay` dropped the `custody` kwarg from
  `chamber.sign_attestation` → 15 identity failures. Fixed by stamping
  custody onto the envelope post-sign in `identity/core.py` and
  `spend/seal.py`. Identity back to 28/28 (+1 new = 29/29).
- Upstream AwLPay fee law changed 1.0%+$0.25 → 0.5% flat (`fees.py`
  2026-10-06) → 9 spend test failures. Test assertions updated to the new
  law (24000¢ → fee 120; 10000¢ overage → fee 50; dust boundary is now
  $0.00 since 0.5%-floored fees can't eat a positive amount). Spend back
  to 55/55 (+5 new = 60/60).

### Fix 1 — BLOCKER: caller auth on chat endpoints (`chat/auth.py` new)
- Every trust-mutating endpoint (grant, deny, revoke, task approve/deny,
  message post-as) now requires `X-Identity-Id` / `X-Identity-Ts` /
  `X-Identity-Sig` headers: Ed25519 signature over
  `METHOD\nPATH\nRAW_BODY\nTIMESTAMP`, verified against the key bound at
  join. Missing/bad/stale (>|5min| skew) → 401; signer ≠ claimed member
  identity → 403.
- Key binding is first-bind-wins, persisted in
  `chat/data/identity_keys.json` (override: `RIDERXL_IDENTITY_KEYS`);
  conflicting re-bind → 403. The browser key is separate from seat keys
  (seat private keys stay server-side).
- Frontend (`index.html`): generates a WebCrypto Ed25519 keypair at
  identity creation (persisted as JWK in localStorage), sends the pubkey
  at join (required; 400 without it), signs via `signedFetch()` on all
  trust-mutating calls; join is idempotent so silent rejoin re-binds.
- Adversarial re-test: revoke-spoof with no sig → 401; wrong key → 401;
  signed-as-self-but-naming-victim → 403; tampered body → 401; stale ts →
  401; valid signature → 200. All in `chat/tests/test_auth.py`.

### Fix 2 — BLOCKER: Proposal immutability (`spend/approve.py`)
- `Proposal.__setattr__` guard: any external attribute write after
  construction raises `AttributeError`. The only mutation paths are
  `_record_vote` / `_set_status` / `_mark_executed` (via
  `object.__setattr__`); `ProposalStore` and `service.execute()` updated
  to use them. Added `executed_at` (set by `_mark_executed`, in
  `to_dict()`); direct status/policy/votes/executed writes verified to
  raise (5 new tests).

### Fix 3 — MAJOR: permissions stub fails closed (`chat/permissions.py`, `chat/server.py`)
- Stub import prints a loud stderr warning; `server.py main()` refuses
  to start when `identity.core` is unavailable unless
  `RIDERXL_ALLOW_STUB=1` is set (explicit acknowledgement + warning).

### Fix 4 — MAJOR: locks on shared stores
- `identity/core.py`: module-level `RLock` guarding all store mutations
  (seats, grants, revocations, trust).
- `chat/permissions.py`: `_lock` upgraded to `RLock`; read paths
  (`_real_check_access`, `_real_is_trusted`, `stored_grant`) and stub
  writes now take it.
- `chat/server.py`: `_groups_lock = RLock` around group load, join,
  grant/deny/revoke, task resolution, and `_append` (seq+append+persist
  atomic).

### Fix 5 — MAJOR: approvals on `spend:approve`, no self-approve
- Task approve/deny now requires a live `spend:approve` grant (was
  `chat:write`); `AGENT_SCOPES` (permission card) extended to
  `["chat:read","chat:write","spend:propose","spend:approve"]`.
- Requester cannot approve/deny their own spend-like request → 403.
  Covered by `test_approve_scope_and_self_approve` and the end-to-end
  demo-flow test.

### Fix 6 — MINORs (`identity/core.py`)
- `verify_grant` asserts seat kinds (grantor personal, grantee group).
- IDs are 128-bit (`token_hex(16)`); `GrantStore.put` refuses duplicate
  ids instead of overwriting (tamper test updated to mutate the store
  dict directly, matching a real in-process attacker; new
  duplicate-refusal test added).
- `audit()` OSError → stderr warning (still never breaks the path).
- `reset()` clears the ephemeral `_CHAMBER_SK`.
- Negative `ttl_seconds` rejected unless `RIDERXL_ALLOW_NEGATIVE_TTL=1`
  (set in `test_identity.py`).

### Fix 7 — `verify_receipt` expansion (`spend/service.py`)
- Body-vs-sealed-payload checks expanded from 5 to 13 fields: adds
  group_id, description, policy, rail, executed_at, approvals, denials,
  tier. Seal payload's `executed_at` now comes from the proposal (single
  timestamp source) so the check is exact.

### Fix 8 — `/api/debug/*` gated
- Behind `RIDERXL_DEBUG=1` (default off → 404). Verified live: 404
  default, 200 with the flag.

### Deliberately left out (review-noted, out of the task's fix list)
- `ProposalStore` permissive-stub default, `mock_awlpay` broad-except
  import fallback, `_is_spend_like` keyword weakness, sequential task
  ids, seal key-embedded trust — flagged in KIMI_REVIEW.md but explicitly
  out of scope for this pass; no scope creep.

### Re-verified 2026-10-07
- `python3 -m pytest identity/test_identity.py spend/tests chat/tests` →
  **115 passed**. Server binary smoke: starts clean, debug 404 by default /
  200 with flag, frontend JS `node --check` clean.
- Lab-only envelope kept: localhost bind, mock money, no network calls,
  no real chamber keys.
