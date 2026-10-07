"""Group-chat demo backend (stdlib only). Lab-only: binds 127.0.0.1.

Launch:  cd ~/workspace/group-agent && python3 chat/server.py
Then open: http://127.0.0.1:8088/

State: in-memory, with per-group JSON persistence under chat/data/groups/
(atomic tmp+replace writes, same pattern as agent-games' lobby.py).

Identities: a personal identity is {identity_id, display_name}, stored by
the browser in localStorage (per tab) or seeded via ?as=<name>. Joining a
group with a display name creates a member row linked to that identity.
Guests (member_type "guest") are name-only, no platform account, and can
read everything the agent posts.

Permission-first: RiderXL only processes a member's message when the
silo-gate check_access(identity_id, "chat:write", group_id) passes --
i.e. a valid, unexpired, unrevoked signed grant exists (see
chat/permissions.py for the mapping onto identity/core.py). Otherwise the
first @agent mention emits a permission-request card; Grant/Deny wire to
permissions.grant / the local denied flag. The "Revoke trust" button calls
permissions.revoke, which revokes the real grants and clears trust, so the
agent immediately stops acting for that identity (pending approvals the
identity requested are marked revoked).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from chat import permissions as perms  # noqa: E402
from chat import auth as caller_auth  # noqa: E402
from chat.agent import (  # noqa: E402
    AGENT_MEMBER_ID, AGENT_NAME, AGENT_SCOPES, RiderXL,
)

HOST = "127.0.0.1"
PORT = 8088
DATA_DIR = os.path.join(HERE, "data")
GROUPS_DIR = os.path.join(DATA_DIR, "groups")

agent = RiderXL()
groups: dict[str, dict] = {}

# Review fix 2026-10-07: one RLock around the shared groups dict —
# ThreadingHTTPServer serves concurrent requests, and seq/member/task
# mutations must not interleave.
import threading as _threading
_groups_lock = _threading.RLock()

# Review fix 2026-10-07: /api/debug/* is gated behind an explicit env flag,
# default OFF. It leaks grant/permission internals.
_DEBUG_ENABLED = os.environ.get("RIDERXL_DEBUG") == "1"


# ---------- persistence ----------
def _safe_id(raw: str) -> str:
    return "".join(c for c in raw if c.isalnum() or c in ("-", "_"))[:64]


def _group_path(gid: str) -> str:
    return os.path.join(GROUPS_DIR, f"{_safe_id(gid)}.json")


def _save_group(g: dict) -> None:
    os.makedirs(GROUPS_DIR, exist_ok=True)
    tmp = _group_path(g["group_id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(g, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _group_path(g["group_id"]))


def _load_all() -> None:
    os.makedirs(GROUPS_DIR, exist_ok=True)
    with _groups_lock:
        for fname in os.listdir(GROUPS_DIR):
            if fname.endswith(".json"):
                with open(os.path.join(GROUPS_DIR, fname), encoding="utf-8") as f:
                    g = json.load(f)
                groups[g["group_id"]] = g


def _new_group(name: str) -> dict:
    gid = "grp-" + secrets.token_hex(4)
    g = {
        "group_id": gid,
        "name": name,
        "created_ts": time.time(),
        "seq": 0,
        "members": [],        # {member_id, identity_id, display_name, type, joined_ts}
        "messages": [],       # {seq, ts, member_id, display_name, kind, text, payload}
        "tasks": {},          # task_id -> task dict (owned by agent.py)
        "perm_pending": {},   # identity_id -> True (card shown, awaiting decision)
        "perm_denied": {},    # identity_id -> True (don't re-ask)
    }
    with _groups_lock:
        groups[gid] = g
        _save_group(g)
        _append(g, AGENT_MEMBER_ID, AGENT_NAME, "system",
                f"Group '{name}' created. RiderXL is here -- @agent to talk to me.",
                {})
    return g


def _append(g: dict, member_id: str, display_name: str,
            kind: str, text: str, payload: dict) -> dict:
    # Review fix 2026-10-07: seq increment + append + persist are atomic
    # under the groups lock (ThreadingHTTPServer serves concurrent posts).
    with _groups_lock:
        g["seq"] += 1
        msg = {
            "seq": g["seq"],
            "ts": time.time(),
            "member_id": member_id,
            "display_name": display_name,
            "kind": kind,
            "text": text,
            "payload": dict(payload or {}),
        }
        g["messages"].append(msg)
        _save_group(g)
    return msg


def _member(g: dict, member_id: str) -> dict | None:
    return next((m for m in g["members"] if m["member_id"] == member_id), None)


def _public_group(g: dict) -> dict:
    return {
        "group_id": g["group_id"],
        "name": g["name"],
        "created_ts": g["created_ts"],
        "member_count": len(g["members"]),
    }


def _public_state(g: dict, since: int = 0) -> dict:
    return {
        "group_id": g["group_id"],
        "name": g["name"],
        "seq": g["seq"],
        "members": [
            {"member_id": m["member_id"], "display_name": m["display_name"],
             "type": m["type"], "identity_id": m["identity_id"]}
            for m in g["members"]
        ],
        "messages": [m for m in g["messages"] if m["seq"] > since][-400:],
        "tasks": g["tasks"],
        "perm_pending": g["perm_pending"],
        "perm_denied": g["perm_denied"],
    }


# ---------- agent dispatch ----------
def _dispatch_to_agent(g: dict, member: dict, text: str) -> None:
    """Permission-first: only trusted identities reach the agent."""
    identity_id = member["identity_id"]
    if not perms.check_access(identity_id, "chat:write", g["group_id"]):
        if ("@agent" in text.lower()
                and identity_id not in g["perm_pending"]
                and identity_id not in g["perm_denied"]):
            g["perm_pending"][identity_id] = True
            _append(g, AGENT_MEMBER_ID, AGENT_NAME, "permission_request",
                    (f"{AGENT_NAME} wants to link with {member['display_name']}: "
                     f"scopes {AGENT_SCOPES}. Grant / Deny."),
                    {"identity_id": identity_id,
                     "display_name": member["display_name"],
                     "scopes": AGENT_SCOPES})
        return
    for evt in agent.handle_message(g, member, text):
        _append(g, AGENT_MEMBER_ID, AGENT_NAME,
                evt["kind"], evt["text"], evt.get("payload", {}))


class Handler(BaseHTTPRequestHandler):
    server_version = "GroupChatDemo/0.1"

    # -- helpers --
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_raw(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _read_json(self) -> dict:
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _require_sig(self, path: str, raw: bytes) -> str | None:
        """Proof-of-ownership check for trust-mutating endpoints.

        Verifies X-Identity-Sig over METHOD\\nPATH\\nRAW_BODY\\nTS against
        the Ed25519 key bound at join. Sends 401 and returns None on any
        failure; returns the authed identity_id on success.
        """
        ok, err, iid = caller_auth.verify("POST", path, raw, self.headers)
        if not ok:
            self._json({"error": "caller auth failed: " + err}, 401)
            return None
        return iid

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("chat: " + fmt % args + "\n")

    # -- routing --
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/":
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json({"ok": True, "ts": time.time(),
                               "using_real_identity": perms.USING_REAL_IDENTITY})
        if path == "/api/groups":
            return self._json({"groups": [_public_group(g) for g in groups.values()]})

        m = re.fullmatch(r"/api/trust/([\w-]+)", path)
        if m:
            iid = m.group(1)
            gid = (qs.get("group_id") or [None])[0]
            return self._json({"identity_id": iid,
                               "group_id": gid,
                               "trusted": perms.is_trusted(iid, gid)})

        # DEBUG endpoints: gated behind RIDERXL_DEBUG=1 (default off).
        # They leak grant/permission internals — never enable publicly.
        if path.startswith("/api/debug/"):
            if not _DEBUG_ENABLED:
                return self._json({"error": "not found"}, 404)
            m = re.fullmatch(r"/api/debug/grant/([\w-]+)", path)
            if m:
                gobj = perms.stored_grant(m.group(1))
                return self._json({"grant_id": m.group(1),
                                   "found": gobj is not None,
                                   "verify_grant": perms.verify_grant(gobj)
                                   if gobj else False})
            if path == "/api/debug/check":
                iid = (qs.get("identity_id") or [""])[0]
                scope = (qs.get("scope") or ["chat:write"])[0]
                gid = (qs.get("group_id") or [""])[0]
                return self._json({"identity_id": iid, "scope": scope,
                                   "group_id": gid,
                                   "check_access": perms.check_access(iid, scope, gid)
                                   if (iid and gid) else False})
            return self._json({"error": "not found"}, 404)

        m = re.fullmatch(r"/api/groups/([\w-]+)", path)
        if m:
            g = groups.get(m.group(1))
            return self._json(_public_state(g)) if g else self._json(
                {"error": "unknown group"}, 404)

        m = re.fullmatch(r"/api/groups/([\w-]+)/poll", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            try:
                since = int(qs.get("since", ["0"])[0] or 0)
            except (ValueError, TypeError):
                # review fix 2026-10-05: garbage ?since= previously 500'd
                return self._json({"error": "bad since"}, 400)
            return self._json(_public_state(g, since))

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        # raw body is read once: signature verification needs the exact
        # bytes the client signed; JSON parsing must not re-serialize.
        raw = self._read_raw()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}

        if path == "/api/groups":
            name = str(body.get("name", "")).strip()[:80] or "Untitled group"
            g = _new_group(name)
            return self._json({"group_id": g["group_id"], "name": g["name"]})

        m = re.fullmatch(r"/api/groups/([\w-]+)/join", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            display_name = str(body.get("display_name", "")).strip()[:40]
            if not display_name:
                return self._json({"error": "display_name required"}, 400)
            mtype = body.get("member_type", "member")
            if mtype not in ("member", "guest"):
                mtype = "member"
            identity_id = str(body.get("identity_id") or "")
            if not re.fullmatch(r"[\w-]{4,64}", identity_id):
                identity_id = "id-" + secrets.token_hex(6)
            # Review fix 2026-10-07 (BLOCKER): the browser generates an
            # Ed25519 keypair at identity creation and sends the pubkey
            # here. Binding is first-bind-wins; a conflicting re-bind is
            # rejected so nobody can hijack an existing identity_id.
            pubkey_hex = str(body.get("identity_pubkey") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", pubkey_hex):
                return self._json(
                    {"error": "identity_pubkey required "
                              "(Ed25519 raw public key, 64 hex chars)"}, 400)
            if not caller_auth.bind(identity_id, pubkey_hex):
                return self._json(
                    {"error": "identity_id already bound to a different key"},
                    403)
            with _groups_lock:
                # rejoin with the same identity returns the existing seat
                existing = next((x for x in g["members"]
                                 if x["identity_id"] == identity_id), None)
                if existing:
                    return self._json({"member_id": existing["member_id"],
                                       "identity_id": identity_id})
                mid = "m-" + secrets.token_hex(4)
                g["members"].append({
                    "member_id": mid, "identity_id": identity_id,
                    "display_name": display_name, "type": mtype,
                    "joined_ts": time.time(),
                })
                _save_group(g)
                _append(g, mid, display_name, "system",
                        f"{display_name} joined ({mtype}).", {})
            perms.issue_group_seat(g["group_id"])
            perms.issue_personal_seat(identity_id, display_name)
            return self._json({"member_id": mid, "identity_id": identity_id})

        m = re.fullmatch(r"/api/groups/([\w-]+)/messages", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            member = _member(g, str(body.get("member_id", "")))
            text = str(body.get("text", ""))[:2000]
            if not member:
                return self._json({"error": "unknown member"}, 403)
            # post-as protection: the signer must own the member's identity
            iid = self._require_sig(path, raw)
            if iid is None:
                return None
            if member["identity_id"] != iid:
                return self._json({"error": "cannot post as another member"},
                                  403)
            if not text.strip():
                return self._json({"error": "empty message"}, 400)
            msg = _append(g, member["member_id"], member["display_name"],
                          "text", text, {})
            _dispatch_to_agent(g, member, text)
            return self._json({"message": msg, "seq": g["seq"]})

        m = re.fullmatch(r"/api/groups/([\w-]+)/grant", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            # proof of ownership: the signer must BE the identity granting
            iid = self._require_sig(path, raw)
            if iid is None:
                return None
            if str(body.get("identity_id", "")) != iid:
                return self._json(
                    {"error": "cannot grant trust for another identity"}, 403)
            grant_result = perms.grant(iid, AGENT_SCOPES, g["group_id"])
            with _groups_lock:
                g["perm_pending"].pop(iid, None)
                g["perm_denied"].pop(iid, None)
                member = next((x for x in g["members"] if x["identity_id"] == iid),
                              None)
                who = member["display_name"] if member else iid
                _append(g, AGENT_MEMBER_ID, AGENT_NAME, "system",
                        f"{who} granted {AGENT_NAME} {AGENT_SCOPES}. I'm listening.",
                        {"identity_id": iid})
            return self._json({"identity_id": iid, "trusted": True,
                               "scopes": AGENT_SCOPES,
                               "grant_ids": grant_result.get("grant_ids", [])})

        m = re.fullmatch(r"/api/groups/([\w-]+)/deny", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            iid = self._require_sig(path, raw)
            if iid is None:
                return None
            if str(body.get("identity_id", "")) != iid:
                return self._json(
                    {"error": "cannot deny trust for another identity"}, 403)
            with _groups_lock:
                g["perm_pending"].pop(iid, None)
                g["perm_denied"][iid] = True
                _save_group(g)
                _append(g, AGENT_MEMBER_ID, AGENT_NAME, "system",
                        "Permission denied. I won't act for you -- tap Grant on "
                        "the card if you change your mind.", {"identity_id": iid})
            return self._json({"identity_id": iid, "trusted": False})

        m = re.fullmatch(r"/api/groups/([\w-]+)/revoke", path)
        if m:
            g = groups.get(m.group(1))
            if not g:
                return self._json({"error": "unknown group"}, 404)
            # the revoke-spoof this fixes: only the identity itself,
            # proving key ownership, can revoke its own trust
            iid = self._require_sig(path, raw)
            if iid is None:
                return None
            if str(body.get("identity_id", "")) != iid:
                return self._json(
                    {"error": "cannot revoke trust for another identity"},
                    403)
            revoke_result = perms.revoke(iid, g["group_id"])
            with _groups_lock:
                g["perm_pending"].pop(iid, None)
                # any approval the revoked identity requested is frozen
                for t in g["tasks"].values():
                    if (t["kind"] == "approval"
                            and t["status"] == "pending_approval"
                            and t["payload"].get("requested_by_identity") == iid):
                        t["status"] = "revoked"
                _save_group(g)
                _append(g, AGENT_MEMBER_ID, AGENT_NAME, "system",
                        "Trust revoked. I've stopped working for you immediately.",
                        {"identity_id": iid})
            return self._json({"identity_id": iid, "trusted": False,
                               "revoked_grant_ids":
                                   revoke_result.get("revoked_grant_ids", [])})

        m = re.fullmatch(r"/api/groups/([\w-]+)/tasks/([\w-]+)/approve", path)
        if m:
            return self._resolve_task(m.group(1), m.group(2), path, raw,
                                      body, True)

        m = re.fullmatch(r"/api/groups/([\w-]+)/tasks/([\w-]+)/deny", path)
        if m:
            return self._resolve_task(m.group(1), m.group(2), path, raw,
                                      body, False)

        return self._json({"error": "not found"}, 404)

    def _resolve_task(self, gid, tid, path, raw, body, approved):
        g = groups.get(gid)
        if not g:
            return self._json({"error": "unknown group"}, 404)
        task = g["tasks"].get(tid)
        member = _member(g, str(body.get("member_id", "")))
        if not task:
            return self._json({"error": "unknown task"}, 404)
        if not member:
            return self._json({"error": "unknown member"}, 403)
        if task["status"] != "pending_approval":
            return self._json({"error": f"task is {task['status']}"}, 409)
        # proof of ownership: the approver/denier must sign as the member's
        # own identity — no approving as someone else
        iid = self._require_sig(path, raw)
        if iid is None:
            return None
        if member["identity_id"] != iid:
            return self._json({"error": "cannot resolve as another member"},
                              403)
        # Review fix 2026-10-07: approvals ride on a DEDICATED spend:approve
        # scope — not chat:write (privilege conflation). And the requester
        # can never self-approve their own spend-like request.
        if (task.get("kind") == "approval"
                and task.get("payload", {}).get("requested_by_identity")
                == member["identity_id"]):
            return self._json({"error": "cannot approve your own request"}, 403)
        if not perms.check_access(member["identity_id"], "spend:approve",
                                  g["group_id"]):
            return self._json({"error": "approver lacks a live spend:approve "
                                        "grant"}, 403)
        with _groups_lock:
            evt = agent.resolve_approval(g, task, member, approved)
            _append(g, AGENT_MEMBER_ID, AGENT_NAME,
                    evt["kind"], evt["text"], evt.get("payload", {}))
        return self._json({"task_id": tid, "status": task["status"]})

    def _serve_file(self, name, ctype):
        p = os.path.join(HERE, name)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    # Review fix 2026-10-07: fail CLOSED if the real identity module is
    # unavailable. The permissive stub must never serve silently.
    if not perms.USING_REAL_IDENTITY:
        if os.environ.get("RIDERXL_ALLOW_STUB") != "1":
            sys.stderr.write(
                "FATAL: identity.core unavailable and RIDERXL_ALLOW_STUB!=1 "
                "-- refusing to start with the insecure permissions stub.\n")
            sys.exit(2)
        sys.stderr.write(
            "!!! WARNING: running with the INSECURE permissions stub "
            "(RIDERXL_ALLOW_STUB=1). Access control is permissive.\n")
    _load_all()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"group-agent chat demo on http://{HOST}:{PORT}/ "
          f"(real identity module: {perms.USING_REAL_IDENTITY})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
