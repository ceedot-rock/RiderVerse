"""chat/tests/test_auth.py — caller-auth (proof-of-ownership) tests.

Review fix 2026-10-07 (BLOCKER): trust-mutating chat endpoints accepted a
client-supplied identity_id / member_id with NO proof of ownership, so a
revoke-spoof (POST /revoke with someone else's identity_id) succeeded.

Now every trust-mutating endpoint (grant, deny, revoke, approve, post-as)
requires X-Identity-Id / X-Identity-Ts / X-Identity-Sig headers, verified
against the Ed25519 key bound at join (first-bind-wins).

Run:  cd ~/workspace/group-agent && python3 -m pytest chat/tests -q
Lab-only (localhost, mock money).
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from nacl.signing import SigningKey

HERE = os.path.dirname(os.path.abspath(__file__))          # chat/tests
CHAT = os.path.dirname(HERE)                               # chat
PARENT = os.path.dirname(CHAT)                             # group-agent
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from chat import auth
from chat import server
from chat import permissions as perms


# Every test (unit and live) gets a scratch key registry — the real
# chat/data/identity_keys.json is never touched by tests.
@pytest.fixture(autouse=True)
def _scratch_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("RIDERXL_IDENTITY_KEYS", str(tmp_path / "keys.json"))
    auth.reset()
    yield
    auth.reset()


# ---------------------------------------------------------------- unit

def _hdrs(iid="id-alice", ts=None, sig="AAAA"):
    return {"X-Identity-Id": iid,
            "X-Identity-Ts": str(ts if ts is not None else int(time.time())),
            "X-Identity-Sig": sig}


class TestBind:
    def setup_method(self):
        auth.reset()

    def test_first_bind_wins(self):
        assert auth.bind("id-a", "aa" * 32) is True
        assert auth.bound_pubkey("id-a") == "aa" * 32

    def test_same_key_rebind_ok(self):
        auth.bind("id-a", "aa" * 32)
        assert auth.bind("id-a", "aa" * 32) is True

    def test_conflicting_key_rejected(self):
        auth.bind("id-a", "aa" * 32)
        assert auth.bind("id-a", "bb" * 32) is False
        assert auth.bound_pubkey("id-a") == "aa" * 32  # unchanged

    def test_bad_inputs_rejected(self):
        assert auth.bind("id-a", "not-hex") is False
        assert auth.bind("id-a", "aa" * 31) is False          # short
        assert auth.bind("!!!", "aa" * 32) is False          # bad id
        assert auth.bound_pubkey("id-a") is None

    def test_unknown_identity_verify_fails(self):
        ok, err, iid = auth.verify("POST", "/x", b"{}", _hdrs("id-nobody"))
        assert ok is False and "unknown identity" in err


class TestVerify:
    def setup_method(self):
        auth.reset()
        self.sk = SigningKey.generate()
        self.pub = self.sk.verify_key.encode().hex()
        assert auth.bind("id-alice", self.pub) is True

    def _sign(self, method, path, body, iid="id-alice", ts=None):
        ts = str(ts if ts is not None else int(time.time()))
        msg = auth.canonical_message(method, path, body, ts)
        sig = base64.b64encode(self.sk.sign(msg).signature).decode()
        return {"X-Identity-Id": iid, "X-Identity-Ts": ts,
                "X-Identity-Sig": sig}

    def test_valid_signature(self):
        h = self._sign("POST", "/p", b'{"a":1}')
        ok, err, iid = auth.verify("POST", "/p", b'{"a":1}', h)
        assert (ok, iid) == (True, "id-alice"), err

    def test_missing_headers(self):
        ok, err, _ = auth.verify("POST", "/p", b"{}", {})
        assert ok is False and "missing" in err

    def test_tampered_body(self):
        h = self._sign("POST", "/p", b'{"a":1}')
        ok, err, _ = auth.verify("POST", "/p", b'{"a":2}', h)
        assert ok is False and "bad signature" in err

    def test_tampered_path(self):
        h = self._sign("POST", "/p", b'{}')
        ok, err, _ = auth.verify("POST", "/q", b'{}', h)
        assert ok is False and "bad signature" in err

    def test_wrong_key(self):
        other = SigningKey.generate()
        ts = str(int(time.time()))
        msg = auth.canonical_message("POST", "/p", b"{}", ts)
        sig = base64.b64encode(other.sign(msg).signature).decode()
        h = {"X-Identity-Id": "id-alice", "X-Identity-Ts": ts,
             "X-Identity-Sig": sig}
        ok, err, _ = auth.verify("POST", "/p", b"{}", h)
        assert ok is False and "bad signature" in err

    def test_stale_timestamp(self):
        h = self._sign("POST", "/p", b"{}", ts=int(time.time()) - 600)
        ok, err, _ = auth.verify("POST", "/p", b"{}", h)
        assert ok is False and "stale" in err

    def test_future_timestamp(self):
        h = self._sign("POST", "/p", b"{}", ts=int(time.time()) + 600)
        ok, err, _ = auth.verify("POST", "/p", b"{}", h)
        assert ok is False and "stale" in err

    def test_bad_timestamp_format(self):
        h = self._sign("POST", "/p", b"{}")
        h["X-Identity-Ts"] = "yesterday"
        ok, err, _ = auth.verify("POST", "/p", b"{}", h)
        assert ok is False and "timestamp" in err


# ------------------------------------------------------- live server

def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _signed_headers(sk: SigningKey, iid: str, path: str,
                    body: str, ts=None) -> dict:
    ts = str(ts if ts is not None else int(time.time()))
    msg = ("POST\n" + path + "\n" + body + "\n" + ts).encode("utf-8")
    sig = base64.b64encode(sk.sign(msg).signature).decode()
    return {"Content-Type": "application/json",
            "X-Identity-Id": iid, "X-Identity-Ts": ts,
            "X-Identity-Sig": sig}


def _post(base: str, path: str, obj: dict, headers: dict | None = None):
    body = _compact(obj)
    req = urllib.request.Request(base + path, data=body.encode("utf-8"),
                                 headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """Run the real chat server on an ephemeral port with scratch dirs."""
    keys = str(tmp_path / "identity_keys.json")
    monkeypatch.setenv("RIDERXL_IDENTITY_KEYS", keys)
    auth.reset()
    server.groups.clear()
    server.GROUPS_DIR = str(tmp_path / "groups")
    server._load_all()
    # fresh permission layer state too
    perms._user_seats.clear()
    perms._riderxl_seats.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield base
    srv.shutdown()
    t.join(timeout=5)
    srv.server_close()
    server.groups.clear()
    auth.reset()


def _new_group(live) -> str:
    status, g = _post(live, "/api/groups", {"name": "t"})
    assert status == 200, g
    return g["group_id"]


def _join_group(live, gid, iid, display, sk: SigningKey):
    path = "/api/groups/%s/join" % gid
    status, j = _post(live, path,
                      {"display_name": display, "member_type": "member",
                       "identity_id": iid,
                       "identity_pubkey": sk.verify_key.encode().hex()})
    assert status == 200, j
    return j["member_id"]


def _join(live, iid, display, sk: SigningKey):
    gid = _new_group(live)
    return gid, _join_group(live, gid, iid, display, sk)


class TestAdversarial:
    """The revoke-spoof from the review must now fail."""

    def test_join_requires_pubkey(self, live):
        status, g = _post(live, "/api/groups", {"name": "t"})
        gid = g["group_id"]
        status, j = _post(live, "/api/groups/%s/join" % gid,
                          {"display_name": "x", "identity_id": "id-x"})
        assert status == 400
        assert "identity_pubkey" in j["error"]

    def test_join_conflicting_key_rejected(self, live):
        sk1, sk2 = SigningKey.generate(), SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk1)
        status, j = _post(live, "/api/groups/%s/join" % gid,
                          {"display_name": "victim", "identity_id": "id-victim",
                           "identity_pubkey": sk2.verify_key.encode().hex()})
        assert status == 403

    def test_revoke_spoof_no_signature_fails(self, live):
        sk = SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk)
        # attacker posts victim's identity_id with NO signature headers
        status, j = _post(live, "/api/groups/%s/revoke" % gid,
                          {"identity_id": "id-victim"},
                          {"Content-Type": "application/json"})
        assert status == 401, j

    def test_revoke_spoof_wrong_key_fails(self, live):
        vsk, ask = SigningKey.generate(), SigningKey.generate()
        gid = _new_group(live)
        _join_group(live, gid, "id-victim", "victim", vsk)
        _join_group(live, gid, "id-attacker", "attacker", ask)
        path = "/api/groups/%s/revoke" % gid
        body = _compact({"identity_id": "id-victim"})
        # attacker claims victim's identity but signs with attacker's key
        h = _signed_headers(ask, "id-victim", path, body)
        status, j = _post(live, path, {"identity_id": "id-victim"}, h)
        assert status == 401, j

    def test_revoke_spoof_identity_mismatch_fails(self, live):
        vsk, ask = SigningKey.generate(), SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", vsk)
        _join(live, "id-attacker", "attacker", ask)
        path = "/api/groups/%s/revoke" % gid
        body = _compact({"identity_id": "id-victim"})
        # attacker signs as THEMSELF but names the victim in the body
        h = _signed_headers(ask, "id-attacker", path, body)
        status, j = _post(live, path, {"identity_id": "id-victim"}, h)
        assert status == 403, j

    def test_revoke_with_valid_signature_succeeds(self, live):
        sk = SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk)
        path = "/api/groups/%s/revoke" % gid
        body = _compact({"identity_id": "id-victim"})
        h = _signed_headers(sk, "id-victim", path, body)
        status, j = _post(live, path, {"identity_id": "id-victim"}, h)
        assert status == 200, j
        assert j["trusted"] is False

    def test_revoke_tampered_body_fails(self, live):
        sk = SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk)
        path = "/api/groups/%s/revoke" % gid
        signed_body = _compact({"identity_id": "id-victim"})
        h = _signed_headers(sk, "id-victim", path, signed_body)
        # signature is over {"identity_id":"id-victim"}; send something else
        status, j = _post(live, path, {"identity_id": "id-victim",
                                       "extra": "tampered"}, h)
        assert status == 401, j

    def test_revoke_stale_timestamp_fails(self, live):
        sk = SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk)
        path = "/api/groups/%s/revoke" % gid
        body = _compact({"identity_id": "id-victim"})
        h = _signed_headers(sk, "id-victim", path, body,
                            ts=int(time.time()) - 600)
        status, j = _post(live, path, {"identity_id": "id-victim"}, h)
        assert status == 401, j

    def test_post_as_another_member_fails(self, live):
        vsk, ask = SigningKey.generate(), SigningKey.generate()
        gid = _new_group(live)
        victim_mid = _join_group(live, gid, "id-victim", "victim", vsk)
        attacker_mid = _join_group(live, gid, "id-attacker", "attacker", ask)
        assert victim_mid != attacker_mid
        path = "/api/groups/%s/messages" % gid
        body = _compact({"member_id": victim_mid, "text": "spoofed!"})
        # attacker signs as self but posts with the victim's member_id
        h = _signed_headers(ask, "id-attacker", path, body)
        status, j = _post(live, path,
                          {"member_id": victim_mid, "text": "spoofed!"}, h)
        assert status == 403, j

    def test_post_as_self_succeeds(self, live):
        sk = SigningKey.generate()
        gid, mid = _join(live, "id-victim", "victim", sk)
        path = "/api/groups/%s/messages" % gid
        body = _compact({"member_id": mid, "text": "hello"})
        h = _signed_headers(sk, "id-victim", path, body)
        status, j = _post(live, path, {"member_id": mid, "text": "hello"}, h)
        assert status == 200, j

    def test_grant_requires_signature(self, live):
        sk = SigningKey.generate()
        gid, _ = _join(live, "id-victim", "victim", sk)
        path = "/api/groups/%s/grant" % gid
        status, j = _post(live, path, {"identity_id": "id-victim"},
                          {"Content-Type": "application/json"})
        assert status == 401, j
        body = _compact({"identity_id": "id-victim"})
        h = _signed_headers(sk, "id-victim", path, body)
        status, j = _post(live, path, {"identity_id": "id-victim"}, h)
        assert status == 200, j
        assert j["trusted"] is True

    def test_approve_scope_and_self_approve(self, live):
        """Approvals need spend:approve (not chat:write); no self-approve."""
        rsk, ask = SigningKey.generate(), SigningKey.generate()
        gid = _new_group(live)
        _join_group(live, gid, "id-req", "requester", rsk)
        amid = _join_group(live, gid, "id-appr", "approver", ask)
        # inject a pending approval task requested by id-req
        tid = "t-selftest"
        server.groups[gid]["tasks"][tid] = {
            "task_id": tid, "kind": "approval",
            "status": "pending_approval",
            "payload": {"requested_by_identity": "id-req",
                        "requested_by": "requester",
                        "request": "buy a lab cable", "amount_usd": 5},
        }
        path = "/api/groups/%s/tasks/%s/approve" % (gid, tid)

        # 1. no signature at all -> 401
        status, j = _post(live, path, {"member_id": amid},
                          {"Content-Type": "application/json"})
        assert status == 401, j

        # 2. requester self-approves with a VALID signature -> 403
        rmid = next(m["member_id"] for m in server.groups[gid]["members"]
                    if m["identity_id"] == "id-req")
        body = _compact({"member_id": rmid})
        h = _signed_headers(rsk, "id-req", path, body)
        status, j = _post(live, path, {"member_id": rmid}, h)
        assert status == 403, j
        assert "own request" in j["error"]

        # 3. approver signs but holds no spend:approve grant -> 403
        body = _compact({"member_id": amid})
        h = _signed_headers(ask, "id-appr", path, body)
        status, j = _post(live, path, {"member_id": amid}, h)
        assert status == 403, j
        assert "spend:approve" in j["error"]

        # 4. with a live spend:approve grant -> 200
        perms.grant("id-appr", ["spend:approve"], gid)
        body = _compact({"member_id": amid})
        h = _signed_headers(ask, "id-appr", path, body)
        status, j = _post(live, path, {"member_id": amid}, h)
        assert status == 200, j
        assert j["status"] == "approved"

    def test_full_demo_flow_end_to_end(self, live):
        """Join -> grant -> spend-like post -> approval card ->
        self-approve blocked -> second member approves. All signed."""
        import urllib.parse
        rsk, ask = SigningKey.generate(), SigningKey.generate()
        gid = _new_group(live)
        rmid = _join_group(live, gid, "id-req", "requester", rsk)
        amid = _join_group(live, gid, "id-appr", "approver", ask)

        def signed(path, obj, sk, iid):
            body = _compact(obj)
            h = _signed_headers(sk, iid, path, body)
            return _post(live, path, obj, h)

        # both grant RiderXL all scopes (permission card)
        for sk, iid in ((rsk, "id-req"), (ask, "id-appr")):
            p = "/api/groups/%s/grant" % gid
            st, j = signed(p, {"identity_id": iid}, sk, iid)
            assert st == 200, j

        # requester posts a spend-like request
        p = "/api/groups/%s/messages" % gid
        st, j = signed(p, {"member_id": rmid,
                           "text": "@agent buy a lab cable for $5"},
                      rsk, "id-req")
        assert st == 200, j

        # poll for the approval card
        with urllib.request.urlopen(
                live + "/api/groups/%s/poll?since=0" % gid,
                timeout=10) as r:
            state = json.loads(r.read().decode())
        cards = [m for m in state["messages"]
                 if m["kind"] == "approval_card"]
        assert cards, "agent should have posted an approval card"
        tid = cards[0]["payload"]["task_id"]

        # requester cannot self-approve (valid signature, own request)
        p = "/api/groups/%s/tasks/%s/approve" % (gid, tid)
        st, j = signed(p, {"member_id": rmid}, rsk, "id-req")
        assert st == 403, j

        # approver (holds spend:approve from the card grant) approves
        st, j = signed(p, {"member_id": amid}, ask, "id-appr")
        assert st == 200, j
        assert j["status"] == "approved"
