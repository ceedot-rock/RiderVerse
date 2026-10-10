# RiderXL — KIMI Security + Correctness Review (Worker 4 of 4)

Date: 2026-10-05. Product name **RiderXL** (LOCKED by Corey) — verified used consistently in code identifiers, receipt fields, labels, and UI; no competing product names found. ("group-agent" appears only as the directory name and as prose, e.g. "group-agent identity" / "group-agent deployment" — not a product name.)

Scope: read-only review of `identity/`, `chat/`, `spend/` with Kimi (kimi-k2.7-code via `~/workspace/skills/moonshot/bin/kimi.py`), plus independent re-runs of all test suites and a fresh-server adversarial smoke test. Three large Kimi prompts (identity+permissions, chat server+agent+frontend, spend module). One call hit HTTP 429 and was retried after cooldown — batching guidance: ≥25s spacing was NOT always enough; allow ≥60s after a long call.

## Test counts (re-run by me, 2026-10-05 ~21:05 EDT)

- `identity/test_identity.py`: **28 passed, 0 failed** (matches the claimed 28).
- `spend/tests` (`python3 -m pytest spend/tests`): **55 passed, 0 failed** (matches the claimed 55). Note: plain `python3 -m pytest` fails in this environment because `pygments` is missing; installed via `pip install --user --break-system-packages pygments` (the env-documented pattern). Not a code issue.
- Chat: no unit test file exists; I ran a fresh end-to-end smoke on a patched server (port 18088): health+frontend OK, permission-first dispatch, grant→signed grants, `@agent book flights` → approval card, ungranted approve → 403, double approve → 409, revoke → agent silent + permission card reappears, bad `?since=` → 400 (post-fix).

## What I fixed (trivial + safe only)

1. **`chat/permissions.py` — trust-before-grant ordering (Kimi batch 1, MAJOR).** `_real_grant()` called `set_trust(..., True)` BEFORE minting grants; a `core.grant()` raise mid-loop would leave trust=True with no backing grant. Fixed: trust is set only after all grants mint successfully. Re-verified: grant → access True; revoke → access False, trust False.
2. **`chat/server.py` — `?since=` 500 on garbage (Kimi batch 2, MINOR).** `int(qs.get("since", ["0"])[0])` raised ValueError → 500. Fixed: returns 400 `{"error": "bad since"}`. Re-verified: `?since=garbage` → 400.

Everything else below is REPORTED, not fixed (needs design decisions or wider refactors).

## Kimi's raw verdicts

- **Batch 1 (identity/core.py + chat/permissions.py):** "NEEDS-FIXES: items 1–4 (silent stub fallback, trust-before-grant ordering, unprotected shared-state concurrency, wildcard stub access), plus 6–9, 11–12. The Ed25519/Chamber design in identity/core.py is fundamentally sound for its lab scope."
- **Batch 2 (chat/server.py + agent.py + index.html):** "UNSOUND. The implementation correctly places the permission gate on the agent dispatch path, but the authorization model collapses because every sensitive endpoint accepts client-supplied identity_id / member_id without proving ownership. Under the stated threat model (a malicious group member on localhost), any member can post, grant, revoke, and approve as any other member."
- **Batch 3 (spend/approve.py + service.py + seal.py + mock_awlpay.py):** "NEEDS-FIXES. The anti-Instinct rule is internally enforced, but the mutable Proposal object lets an external caller bypass it."

## Per-file findings (consolidated, deduped; Kimi + my own probes)

### identity/core.py
- **MINOR** — `verify_grant()` checks the grantor is a known seat but does not assert `grantor.kind == "personal"` / grantee `kind == "group"`; callers of `verify_grant()` alone can be misled. Fix: add seat-kind assertions.
- **MINOR** — `_new_id()` is 64-bit (`token_hex(8)`) and `GrantStore.put()` overwrites on collision. Fix: 128-bit ids, refuse `put()` on existing id.
- **MINOR** — `audit()` swallows `OSError`; disk-full failures are invisible. Fix: log to stderr.
- **MINOR** — `reset()` does not reset the ephemeral Chamber key `_CHAMBER_SK`. Fix: set to None in `reset()`.
- **MINOR** — `grant()` allows negative `ttl_seconds` without a test-mode guard. Fix: reject unless an explicit testing flag.
- **NOTE** — canonical-JSON determinism delegated to `chamber.canonical_json()`; confirmed it uses `sort_keys=True` (deterministic) — Kimi's worry is unfounded.
- **NOTE** — revocation-signature not verified on read; acceptable only while `REVOKED` is a trusted in-memory store.
- SOUND: no grant forgery without grantor private key (dual grantor-sig + Chamber seal over re-canonicalized payload); revocation is a signed record and cuts access (empirically verified); silo gate exact-matches `f"{scope}:{action}"` against known scopes; no private key material persisted (audit.jsonl holds public ids only); `check_access` argument splitting in the adapter (`"chat:write"` → scope `chat`, action `write`) is correct.

### chat/permissions.py
- **BLOCKER** — silent insecure stub fallback: if `identity.core` fails to import, `verify_grant()` returns `bool(grant_obj)` and `check_access()` honors a `"chat:*"` wildcard, bypassing all access control with no loud warning. Fix: fail closed, or gate the stub behind an explicit env flag + loud startup warning. *(Reported; not fixed — design decision.)*
- **MAJOR (FIXED by me)** — trust-before-grant ordering, see above.
- **MAJOR** — shared singleton dicts read/iterated by `verify_grant`/`check_access`/`is_trusted` while `grant`/`revoke` mutate them; `_lock` covers only adapter write paths. Fix: RLock around core stores or immutable snapshots.
- **MINOR** — `issue_group_seat()`/`issue_personal_seat()` accept and ignore a `scopes` argument.
- **MINOR** — `_stub_save()` writes without `_lock`.

### chat/server.py
- **BLOCKER (empirically confirmed by me)** — no caller authentication on identity-scoped endpoints. All trust-mutating endpoints take `identity_id`/`member_id` from the request body; `identity_id` is public in group state. I proved: read victim `identity_id` from `GET /api/groups/<gid>` → `POST /revoke` with it → victim's grants revoked, trust cleared, pending approvals frozen. Same vector works for `/grant` (mint trust for any identity), `/deny` (suppress re-ask), `/tasks/<tid>/approve` (approve as a trusted member), and `/messages` (post as any member_id — if the victim is trusted, the agent acts on their behalf, incl. spend-like commands). Kimi rated this class UNSOUND under a malicious-local-member threat model.
- **MAJOR** — approvals ride on `chat:write` instead of a dedicated `spend:approve` scope (privilege conflation; the adapter doc admits this). Also the requester can self-approve their own spend-like request.
- **MAJOR** — `ThreadingHTTPServer` with unprotected shared `groups` dict: concurrent messages can duplicate/lose `seq` or updates.
- **MINOR (FIXED by me)** — `?since=` 500 → now 400.
- **MINOR** — `/api/debug/grant`, `/api/debug/check` leak grant/permission internals (documented lab-only; strip before any wider exposure).
- **MINOR** — task ids sequential (`task-001`); fine while gated, random would be cleaner.
- SOUND: dispatch gate placement is correct — `_dispatch_to_agent` checks `check_access(identity_id, "chat:write", group_id)` before ANY agent processing, including the bill-split reply path; untrusted `@agent` yields only a permission card; `resolve_approval` posts text only, no action wired; `_save_group` is atomic (tmp+fsync+replace); `?as=` only prefills the join name (no direct impersonation — impersonation requires copying `identity_id`, which the public state exposes).

### chat/agent.py
- **MAJOR** — `_is_spend_like` is substring keyword matching: trivially bypassed (`"b o o k"`, synonyms, other languages) and over-triggers (`"book"` in `"bookkeeper"`). The anti-Instinct control is weak as a classifier; impact is limited today only because approval posts text with no wired action.
- **NOTE** — bill-split regex is bounded (no ReDoS); `float()` is safe post-regex; fair-share display has minor float rounding.

### chat/index.html
- **Clean on XSS.** Kimi checked every render path; I confirmed: all user-controlled values (`display_name`, message text, card titles, scopes, task items, bill entries, approval text, group names) go through `textContent` or the `esc()` helper (textContent→innerHTML) before DOM insertion. **No stored-XSS path in this frontend.** Caveat (MINOR): the server stores text raw and serves it raw over JSON, so any other API client that renders without escaping would be vulnerable; server-side sanitization would harden this.

### spend/approve.py
- **MAJOR** — `ProposalStore` defaults `permissions` to a permissive stub returning True for everyone. Any caller that forgets to inject the real callback gets vote-anyone semantics. Fix: require the argument, or warn loudly per vote. *(Reported.)*
- **MAJOR** — no locks: `_vote()` check-then-set races under threads (double vote), as does execute's double-execute guard. Fix: `threading.Lock`.
- Edge cases I verified myself: majority elimination `n_deny > n - needed` correct for n=1 (1 deny kills), n=2 (1 deny kills), n=3 (2 deny kill); unanimous single-member approves (mathematically consistent). All vote rejections (non-member, double, post-close, no-grant) confirmed by the 55-test suite.

### spend/service.py
- **BLOCKER (design-level; reported, not fixed)** — `Proposal.status` / `Proposal.executed` are public mutable attributes and the store returns live instances; a caller can set `p.status = "APPROVED"` directly and `execute()` will spend. The anti-Instinct rule is enforced against the module's own paths (OPEN/DEAD/double-execute all raise `SpendRefused` — confirmed by tests) but not against direct mutation. Fix: return deep copies from the store or make `Proposal` read-only.
- **MAJOR** — `verify_receipt()` compares only 5 fields (mock_tx_id, proposal_id, amount/fee/net cents) against the sealed payload; `approvals`, `denials`, `rail`, `tier`, `description`, `group_id`, `policy`, `executed_at` are NOT compared — a signed receipt can misrepresent consensus while verifying True. Fix: compare full canonical payload.
- **MINOR** — receipt write uses non-atomic `open(path, "w")`.
- **MINOR** — seal verifies against the key EMBEDDED in the receipt, so a self-signed forgery verifies True (trust = key distribution). Document loudly; optionally allow-list `kid`.
- **NOTE** — `client.execute()` runs before `p.executed = True`; a seal/write failure leaves a generated mock tx id retryable.

### spend/seal.py
- **Clean.** Imports only sign/verify/format from `chamber.py`; never loads the production signing key; runtime-generated keys stamped `custody="lab-local-mock"`. Confirmed by grep: `load_signing_key` appears nowhere in the spend module.

### spend/mock_awlpay.py
- **MAJOR** — broad `except Exception` around the real `fees.py` import silently falls back to a hardcoded formula; an import/signature error would drift fee math silently. Fix: catch only ImportError/ModuleNotFoundError, log fallback.
- **Honest labeling: PASS.** Every quote/execute/receipt carries `mode: "MOCK"`, `network: "none"`, explicit MOCK labels, `mock-tx-` ids. I checked all three sample JSONs: 8/8/3 MOCK mentions with "no real money" labels present. No real-network imports anywhere in the module (grep clean). Fee math imports the real `fees.py` and records `_FEE_SOURCE` per quote; `Decimal(str(amount))` conversion is exact; negative/dust/unknown-tier refusals verified.

### Secrets scan
- No private keys, seeds, bearer tokens, or credentials committed anywhere in the tree. The 64-hex strings present are public Ed25519 verify keys and signatures inside sealed mock receipts (by design, for standalone verification). The real chamber key at `~/.config/slidphi-chamber/signing.key` is never touched (600, not in repo).

## Product-name check
RiderXL is used consistently (code identifiers, receipt fields, UI, logs). "GroupAgent" appears only as prose ("group-agent identity", "group-agent deployment") and the directory name — no competing product name. PASS.

## Final verdict: GO-WITH-CAVEATS for the lab demo

**GO** because, within the stated lab-demo envelope (localhost only, Corey as operator, no real money, mock labels everywhere): the crypto core is sound (no grant forgery, revocation cuts access, silo gate exact); spend's anti-Instinct rule holds on all module paths; receipts are honestly labeled MOCK with lab-local keys; XSS is clean in the shipped frontend; test counts hold (28/28 identity, 55/55 spend); and I fixed the two trivially-safe bugs and re-verified them end-to-end.

**CAVEATS (blockers before anything leaves this VM / localhost):**
1. Chat endpoints have no caller authentication — any local actor can post/grant/deny/revoke/approve as any member (empirically demonstrated revoke-DoS). Add signed session tokens or proof-of-identity before any network exposure.
2. Spend `Proposal` objects are mutable — external callers can bypass the approval threshold by direct attribute write; `verify_receipt()` under-checks sealed fields.
3. The permissions stub fallback fails open silently; make it fail closed.
4. Spend approval rides on `chat:write`, not a dedicated `spend:approve` scope.
5. Add locks around shared stores (chat groups, ProposalStore) for any concurrent use.

None of the caveats is exploitable by accident in the localhost demo flow; all require a deliberately adversarial local actor or a programmer misusing the library APIs.
