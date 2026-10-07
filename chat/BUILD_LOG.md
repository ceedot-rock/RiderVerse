# RiderXL group-chat demo — BUILD_LOG.md (Worker 2)

Date: 2026-10-05. Lab-only prototype. Localhost only, no deploys, no real
network calls, no secrets in code.

Product name LOCKED by Corey (lab owner): **RiderXL**. Used everywhere:
bot display name, permission cards, page title, this log, email-adapter doc.

## What was built

`~/workspace/group-agent/chat/` — a minimal working group chat where
multiple members (separate identities) chat and a shared agent, RiderXL,
participates as a first-class thread member.

| File | What it is |
|---|---|
| `server.py` | Stdlib-only backend (`http.server` + `ThreadingHTTPServer`). In-memory group threads `{group_id, members[], messages[]}` with per-group JSON persistence under `chat/data/groups/` (atomic tmp+replace writes, same pattern as agent-games' `lobby.py`). Endpoints: create/list group, join (member or name-only guest), post message, poll `?since=` (simple polling), grant/deny/revoke trust, approve/deny tasks, per-group trust query, lab-only debug endpoints (see below). Serves `index.html` at `/`. |
| `agent.py` | RiderXL the participant. (a) Answers from a local knowledge base / pattern rules — **no external LLM calls**. The `ScriptedResponder.respond(prompt, context)` hook is the documented plug-in point for a real model later (same shape as agent-games' StubLLM → OpenAICompatibleLLM split). (b) Task commands: `@agent plan the trip` → trip-plan card, `@agent split the bill` → bill-split card that parses follow-up `Name 24.50` replies. (c) **Check-in / anti-Instinct rule:** anything spend-like (`book/buy/pay/send money/order/...`) posts an APPROVAL card and the agent WAITS — it never acts first; even after approval the demo only posts a confirmation (no real action is wired to anything). |
| `permissions.py` | Adapter over Worker 1's `identity/core.py` (see "Permissions adapter" below). |
| `index.html` | Single-page vanilla JS, dark, mobile-ok. Group list → join (name + member/guest) → chat. Supports two identities side-by-side: open a second tab with `?as=OtherName`, or use Switch identity. Renders permission-request cards (Grant/Deny), task cards, approval cards (Approve/Deny), trust badge, per-user "Revoke trust" button. Polls every 1.5 s. |
| `EMAIL_ADAPTER_NEXT.md` | Inbound-email → thread design sketch (doc only, not built — email is an authority surface; permission model must be airtight first). |

## Launch

```bash
cd ~/workspace/group-agent && python3 chat/server.py
# open http://127.0.0.1:8088/
```

Server prints whether the real identity module loaded
(`real identity module: True/False`). A demo server is currently running
from this build session on 127.0.0.1:8088.

## Verification (2026-10-05, curl end-to-end, script at /tmp/riderxl_test.py)

**27/27 checks passed** against the live server, including the real
Worker-1 crypto path:

- Health + frontend served, RiderXL-branded, no "GroupAgent" strings.
- Create group; Alice joins as member; untrusted `@agent help` → permission-request card emitted, **agent stays silent** (permission-first).
- Grant → 2 signed grant ids (`chat:read`, `chat:write`); `verify_grant` **True** server-side on a minted grant; `check_access(chat:write)` **True**.
- After grant: `@agent help` answered; `@agent plan the trip` → task card; `@agent split the bill` → card, `Alice 24.50` reply parsed into the split.
- `@agent book the flights for $350` → approval card, task `pending_approval`, **no action taken** before approval.
- Bob (ungranted) approving → **403**; Bob grants → approves → task `approved`, agent confirms "no real action is wired"; second approve → **409**.
- Alice revokes → grants hit the revocation list: `check_access` **False**, `verify_grant` **False** on the previously-valid grant; her next `@agent` → permission card again, agent silent. `identity/audit.jsonl` shows `grant.revoked` + `trust.set False`.
- Guest joins name-only (non-member) and reads the full thread including agent cards.
- Trust is per-group: Bob trusted in group 1, untrusted in a new group 2.

## Permissions adapter — notes for Worker 1's real module

Worker 1's module (`identity/core.py`) turned out to be **already finished**
when this worker ran, with different signatures than the brief guessed —
so `permissions.py` is a **real adapter**, not a stub. Mapping:

- One RiderXL **group seat** per chat group: `core.issue_group_seat(label="RiderXL", group_id=<chat group_id>)`, cached per process.
- One **personal seat** per chat identity_id: `core.issue_personal_seat(label=<display name>)`; chat `identity_id` ("id-…") ↔ core `user_id` (seat `agent_id`, "p-…") mapping in the adapter.
- `grant(identity_id, scopes, group_id)` → `set_trust(user, group, True)` + one `core.grant(...)` per scope. **Grant TTL = 7 days** (lab-demo choice; core's default is 3600 s — too short for a demo, expiry would silently mute the agent).
- `revoke(identity_id, group_id)` → `core.revoke(grant_id, user_seat)` for every live grant + `set_trust(..., False)`. Verified: access cuts immediately via the revocation list.
- **Dispatch gate** (does RiderXL process a message?): `check_access(identity_id, "chat:write", group_id)` — the full silo gate (seat kinds, trust flag, signed + Chamber-sealed + fresh + unrevoked grant naming the exact scope).
- **Approve/deny gate** on approval cards: `check_access(identity_id, "chat:write", group_id)`. A production deployment should mint `spend:approve` grants for approvers; the demo's permission card only asks for chat scopes, so approval rides on `chat:write`.
- `is_trusted(identity_id, group_id)` → the explicit opt-in flag (drives permission-card display; `group_id=None` = trusted in any known group).
- `verify_grant(grant_obj)` → straight through to `core.verify_grant`.
- If `identity.core` ever fails to import, the adapter falls back to an in-memory stub with the same function names (persisted to `chat/data/trust.json`) — no code changes needed in `server.py`.

**No-swap-needed note:** the adapter already prefers the real module; nothing in `server.py` needs a one-line change. If Worker 1 renames functions, only `permissions.py` needs updating.

Lab-only debug endpoints (localhost; strip before any public deploy):
`GET /api/debug/grant/<grant_id>` → `{found, verify_grant}`;
`GET /api/debug/check?identity_id=&scope=&group_id=` → `{check_access}`.

## Known limitations / honest caveats

- `identity/core.py` registries (seats, grants, trust) are **in-memory**: restarting the chat server wipes them — every identity must re-grant. This is Worker 1's documented lab posture (no persisted private keys); the demo honors it.
- Agent answers are scripted patterns; the LLM hook (`ScriptedResponder` → real backend) is documented in `agent.py` but not wired (lab rule: no network calls).
- Bill-split parsing is a simple `Name amount` regex; trip-plan fill-in replies are not yet parsed back into the card (card lists the expected reply formats).
- Polling is simple 1.5 s HTTP polling (long-poll/WebSocket deferred; agent-games has a WS spectator pattern to copy if needed).
- The permission card reappears after revoke (by design — re-grant is one tap); Deny suppresses re-asking until Grant is tapped.
