# RiderXL — Identity + Permission Core: API Summary

For workers 2 and 3. Import from the single module:

```python
import sys
sys.path.insert(0, "/home/hatch/workspace/group-agent/identity")  # adjust to your layout
import core
```

Product name (LOCKED): **RiderXL** — `core.PRODUCT_NAME`. The RiderXL group
agent acts only under its own group seat.

## Functions

| Function | Signature | Returns |
|---|---|---|
| `issue_group_seat` | `(label="RiderXL", group_id="riderxl") -> Seat` | RiderXL group seat (own keypair, `kind="group"`, id `g-…`) |
| `issue_personal_seat` | `(label) -> Seat` | personal seat stand-in (test users: `"corey"`, `"guest1"`, `"guest2"`); `kind="personal"`, id `p-…` |
| `get_seat` | `(agent_id) -> Seat \| None` | seat lookup |
| `grant` | `(grantor_seat, grantee_agent_id, scope, ttl_seconds=3600) -> dict` | signed + Chamber-sealed grant (deep copy). Raises `ValueError` on unknown scope / unknown grantee / non-personal grantor / non-group grantee |
| `verify_grant` | `(grant) -> bool` | True iff authentic + sealed + fresh + unrevoked; **never raises** |
| `revoke` | `(grant_id, grantor_seat) -> bool` | signed revocation, added to the revocation list. Only the original grantor's seat succeeds |
| `set_trust` | `(user_id, group_id, trusted) -> None` | record per-user per-group trust (default False) |
| `is_trusted` | `(user_id, group_id) -> bool` | explicit-trust check |
| `link_personal_agent` | `(group_id, user_seat) -> bool` | True only if trusted; RiderXL calls this before touching user data |
| `check_access` | `(group_agent_id, user_id, action, scope) -> bool` | **the silo gate** — True only if a valid, unexpired, unrevoked grant covers `(user_id, action, scope)` |
| `known_scopes` | `() -> list` | sorted list of scopes |
| `reset` | `() -> None` | clear all registries (tests) |

`user_id` everywhere means the **personal seat's `agent_id`** (stable), not
the human label. `group_agent_id` is the RiderXL seat's `agent_id`.

## Scopes

`{"chat:read", "chat:write", "spend:propose", "spend:approve"}`

`check_access(group_agent_id, user_id, action, scope)` builds the required
permission as `f"{scope}:{action}"` — e.g. `check_access(g, u, "read", "chat")`
requires a grant with scope `"chat:read"`. Match is exact; unknown scopes are
refused. Trust (`is_trusted`) is checked inside `check_access` too.

## Grant JSON schema

```jsonc
{
  "grant": {                       // signed payload (canonical JSON)
    "grant_id":  "gr-<16 hex>",    // unique; the revocation handle
    "grantor":   "p-<16 hex>",     // personal seat agent_id (signer)
    "grantee":   "g-<16 hex>",     // RiderXL group seat agent_id
    "scope":     "chat:read",      // one of known_scopes()
    "issued_at": "2026-10-05T18:50:00.123456+00:00",  // ISO-8601 UTC
    "expires_at":"2026-10-05T19:50:00.123456+00:00",  // issued_at + ttl
    "nonce":     "<32 hex>"
  },
  "grantor_sig": "<128 hex>",      // Ed25519 sig of canonical payload by grantor
  "seal": {                        // Chamber attestation envelope
    "alg": "ed25519",
    "kid": "<16 hex>",             // seal key id
    "custody": "ephemeral",        // lab-only; "file" in production
    "payload": "<canonical JSON of grant payload>",
    "sig": "<128 hex>"
  }
}
```

## Revocation record schema

```jsonc
{
  "grant_id":   "gr-<16 hex>",
  "revoked_at": "2026-10-05T18:51:00.123456+00:00",
  "grantor":    "p-<16 hex>",
  "grantor_sig":"<128 hex>"        // Ed25519 sig over {"grant_id","revoked_at","grantor"}
}
```

## Trust record

`{user_id: {group_id: bool}}` — unset means False. Managed via
`set_trust()` / `is_trusted()`.

## Worked example

```python
import core

# 1. Seats
riderxl = core.issue_group_seat()            # label "RiderXL", group_id "riderxl"
corey   = core.issue_personal_seat("corey")

# 2. Trust (Corey opts in)
core.set_trust(corey.agent_id, "riderxl", True)
assert core.link_personal_agent("riderxl", corey)

# 3. Permission grant (Corey's key signs; never stored/logged)
g = core.grant(corey, riderxl.agent_id, "chat:read", ttl_seconds=3600)

# 4. Silo gate — the only check before touching Corey's data
assert core.check_access(riderxl.agent_id, corey.agent_id, "read", "chat")
assert not core.check_access(riderxl.agent_id, corey.agent_id, "write", "chat")

# 5. Revocation cuts access immediately
core.revoke(g["grant"]["grant_id"], corey)
assert not core.verify_grant(g)
assert not core.check_access(riderxl.agent_id, corey.agent_id, "read", "chat")
```

## Audit ledger

`core.audit(event, detail)` appends JSONL to `identity/audit.jsonl`.
Events: `seat.issued`, `grant.issued`, `grant.revoked`, `revoke.failed`,
`trust.set`, `link.ok`, `link.refused`, `access.allowed`, `access.denied`.
Public ids and timestamps only — no private material.
