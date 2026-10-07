# BUILD_LOG — RiderXL spend governance (Worker 3/4)

Product name: **RiderXL** (locked by Corey, 2026-10-05). Used in all
receipt fields, labels, log lines, samples, and this log.

Lab-only. MOCK ONLY. Zero real money paths — verified by grep (no
`http`/`requests`/`socket`/`urllib` imports or calls anywhere in this
module). Every quote/receipt/log line carries `mode: MOCK` /
`network: none` / `product: RiderXL`.

## Files (all under `~/workspace/group-agent/spend/`)

| File | Purpose |
|---|---|
| `mock_awlpay.py` | `MockAwlPayClient`: mock `quote()` / `execute()`. Fee math uses the REAL `~/workspace/awlpay/server/fees.py` `calculate_fees()` verbatim via importlib (documented dependency); locked Free-tier formula (1.0% + $0.25, integer cents) as fallback if awlpay is ever absent. Dust/negative/unknown-tier refusals mirror the real server. |
| `approve.py` | `ProposalStore`: propose/approve/deny, `majority` (default) and `unanimous` policies. Majority dies on mathematical elimination; unanimous dies on any veto. Permissions via injected callback `permissions(member_id, action) -> bool` with `action="spend:approve"`; default permissive local stub. Vote rejections: non-member, double-vote, post-close, missing/revoked grant. |
| `seal.py` | `SealAuthority`: Chamber envelope FORMAT + `sign_attestation`/`verify_attestation` code from `~/workspace/awlpay/server/chamber.py` (importlib; format fits, no stand-in crypto). **Sealing decision (documented):** mock receipts are sealed with keys generated FRESH AT RUNTIME, stamped `custody="lab-local-mock"`. The production chamber key (`~/.config/slidphi-chamber/signing.key`) is deliberately NEVER used for mock receipts — a lab toy must not verify as a real attestation. |
| `service.py` | `GroupSpendService`: propose → vote → threshold → mock-execute → sealed receipt JSON on disk. **ANTI-INSTINCT:** `execute()` raises `SpendRefused` unless proposal status is `APPROVED`; also refuses double-execute (one spend per approval). `verify_receipt()` checks envelope signature AND body-vs-sealed-payload consistency. |
| `__init__.py` | Public exports. |
| `demo.py` | Runnable demo of all three scenarios (importlib bootstrap; parent dir is hyphenated `group-agent`). |
| `tests/` | 55 tests (see below). |
| `samples/` | 3 sample JSON files (see below). |
| `receipts/` | Runtime dir where `GroupSpendService` writes executed receipts (created on demand). |

## Identity integration (Worker 1)

`identity/core.py` does not exist yet — NOT hard-depended on. Swap in the
real module by passing a callback at construction:

```python
svc = GroupSpendService(permissions=lambda member, action: identity.check_access(member, action))
```

`action` is always `"spend:approve"` (`ACTION_SPEND_APPROVE`).

## Test results — EXACT: 55 passed, 0 failed

Run: `cd ~/workspace/group-agent && python3 -m pytest spend/tests`

- `tests/test_mock_awlpay.py` — 23 tests: free/pro/l33t/pro-overage fee parity checked directly against the real `fees.py` for $1 / $19.99 / $240 / $10,000; exact $240 → 265¢ fee / 23735¢ net; negative/dust/unknown-tier/empty-rail refusals; rail normalization; mock receipt labels (`mode=MOCK`, `network=none`, `product=RiderXL`, `mock-tx-` id); non-mock quote rejected; exact Decimal USD→cents.
- `tests/test_approve.py` — 19 tests: propose validation (bad policy, empty/dup members, unknown id); unanimous approve/veto/single-member; majority 2-of-3, 3-of-5, mathematical-elimination death, denial-without-elimination stays open; non-member vote, double-vote, post-close vote, revoked-grant vote (stub callback), grant checked with `spend:approve` action, default stub permissive, proposer-without-grant cannot vote.
- `tests/test_service.py` — 13 tests, incl. the ANTI-INSTINCT battery: execute on OPEN (0 votes), partially-approved, DEAD, and unknown proposals all raise `SpendRefused`; full majority + unanimous chains produce sealed receipts that verify; double-execute refused; top-level tamper, envelope-payload tamper, and wrong-key all fail verification; standalone `verify_with_key_hex`; dust proposal never created; end-to-end non-member vote rejection.

One real bug was found and fixed during the run: the first `verify_receipt` only checked the envelope signature, so forging a top-level field passed. `verify_receipt` now also requires body↔sealed-payload consistency (added as a test first, then fixed — 55/55 after).

## Demo scenarios (output)

Run: `python3 ~/workspace/group-agent/spend/demo.py`

- (a) **Book the cabin $240 split 3 ways, unanimous** → corey/alice/bob approve → `APPROVED (unanimous approval (3/3))` → sealed RiderXL mock receipt, seal verifies `True`.
- (b) **Same spend, alice vetoes** → `DEAD (unanimous veto: alice denied)` → no receipt, `receipt_issued=false`.
- (c) **Majority, 2-of-3 with dissent** → corey approve, bob deny, alice approve → `APPROVED (majority approval (2/3, needed 2))` → sealed receipt recording `deny: ["bob"]`, seal verifies `True`.

## Sample files

- `samples/receipt_cabin_unanimous.json` — full proposal→votes→quote→sealed receipt chain (unanimous).
- `samples/receipt_cabin_majority_dissent.json` — same chain under majority with a recorded deny vote.
- `samples/veto_dead_proposal.json` — dead proposal record, `receipt_issued: false`, no receipt.

## Verification notes

- `grep -rnE "import (http|requests|socket|urllib)|from (http|requests|socket|urllib)"` → no matches: no network paths in this module.
- No secrets committed: signing keys are `SigningKey.generate()` at runtime only; grep for key material → clean.
- `pynacl` was installed (`pip install --user --break-system-packages pynacl`) because `chamber.py` needs it; environment note for anyone re-running tests.
- Fee-source used at runtime: `real:~/workspace/awlpay/server/fees.py` (recorded in every mock quote as `fee_source`).
