"""identity/test_identity.py — lab-only tests for the RiderXL permission core.

Run:  python3 test_identity.py      (or python3 -m unittest test_identity -v)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Review fix 2026-10-07: negative ttl_seconds is test-only behind this flag.
os.environ["RIDERXL_ALLOW_NEGATIVE_TTL"] = "1"

import core
from core import (
    issue_group_seat, issue_personal_seat, grant, revoke, verify_grant,
    set_trust, is_trusted, link_personal_agent, check_access,
    GRANTS, REVOKED, TRUST, SEATS, reset, known_scopes,
)
from nacl.signing import SigningKey


class PermCoreTest(unittest.TestCase):
    def setUp(self):
        reset()
        self.group = issue_group_seat()  # RiderXL: label="RiderXL", group_id="riderxl"
        self.corey = issue_personal_seat("corey")
        self.guest1 = issue_personal_seat("guest1")
        self.guest2 = issue_personal_seat("guest2")
        set_trust(self.corey.agent_id, "riderxl", True)
        set_trust(self.guest1.agent_id, "riderxl", True)
        set_trust(self.guest2.agent_id, "riderxl", True)

    # ---- 1. seat issuance -------------------------------------------------
    def test_seats_distinct(self):
        self.assertEqual(self.group.kind, "group")
        self.assertEqual(self.corey.kind, "personal")
        self.assertEqual(self.group.label, core.PRODUCT_NAME)  # "RiderXL", LOCKED
        self.assertEqual(core.PRODUCT_NAME, "RiderXL")
        self.assertTrue(self.group.agent_id.startswith("g-"))
        self.assertTrue(self.corey.agent_id.startswith("p-"))
        self.assertEqual(self.group.group_id, "riderxl")
        self.assertIsNone(self.corey.group_id)
        ids = {self.group.agent_id, self.corey.agent_id,
               self.guest1.agent_id, self.guest2.agent_id}
        self.assertEqual(len(ids), 4)
        keys = {self.group.verify_key_hex, self.corey.verify_key_hex,
                self.guest1.verify_key_hex, self.guest2.verify_key_hex}
        self.assertEqual(len(keys), 4)
        # public record carries no private material
        self.assertNotIn("signing_key", self.group.public())

    # ---- 2. grants: happy path + issuance refusals ------------------------
    def test_grant_verify_valid(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        self.assertTrue(verify_grant(g))
        for f in ("grant_id", "grantor", "grantee", "scope",
                  "issued_at", "expires_at", "nonce"):
            self.assertIn(f, g["grant"])
        self.assertEqual(g["grant"]["grantor"], self.corey.agent_id)
        self.assertEqual(g["grant"]["grantee"], self.group.agent_id)
        self.assertEqual(g["grant"]["scope"], "chat:read")
        self.assertIn("grantor_sig", g)
        self.assertIn("seal", g)

    def test_grant_unknown_scope_refused(self):
        with self.assertRaises(ValueError):
            grant(self.corey, self.group.agent_id, "root:everything")

    def test_grant_group_cannot_grant(self):
        with self.assertRaises(ValueError):
            grant(self.group, self.group.agent_id, "chat:read")

    def test_grant_unknown_grantee_refused(self):
        with self.assertRaises(ValueError):
            grant(self.corey, "g-deadbeef", "chat:read")

    def test_grant_personal_cannot_be_grantee(self):
        with self.assertRaises(ValueError):
            grant(self.corey, self.guest1.agent_id, "chat:read")

    # ---- 3. verification failures ------------------------------------------
    def test_verify_expired_grant(self):
        g = grant(self.corey, self.group.agent_id, "chat:read", ttl_seconds=-60)
        self.assertFalse(verify_grant(g))

    def test_verify_tampered_payload(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        g["grant"]["scope"] = "spend:approve"  # escalate, keep signatures
        self.assertFalse(verify_grant(g))

    def test_verify_wrong_key_forgery(self):
        # attacker signs corey's payload with THEIR key
        g = grant(self.corey, self.group.agent_id, "chat:read")
        canon = core.chamber.canonical_json(g["grant"]).encode("utf-8")
        forged_sig = self.guest2.signing_key.sign(canon).signature.hex()
        g["grantor_sig"] = forged_sig
        self.assertFalse(verify_grant(g))

    def test_verify_tampered_seal(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        sig = g["seal"]["sig"]
        g["seal"]["sig"] = ("0" if sig[0] != "0" else "1") + sig[1:]
        self.assertFalse(verify_grant(g))

    def test_verify_malformed(self):
        self.assertFalse(verify_grant({}))
        self.assertFalse(verify_grant(None))
        self.assertFalse(verify_grant("not-a-grant"))
        self.assertFalse(verify_grant({"grant": {"grant_id": "x"}}))

    def test_verify_unknown_grantor(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        g["grant"]["grantor"] = "p-unknown"
        self.assertFalse(verify_grant(g))

    # ---- 4. revocation: the prove-it lifecycle ------------------------------
    def test_revoke_lifecycle_grant_allow_revoke_deny(self):
        """PROOF: grant -> access allowed -> revoke -> access denied."""
        g = grant(self.corey, self.group.agent_id, "chat:read")
        gid = g["grant"]["grant_id"]
        # access allowed while valid
        self.assertTrue(verify_grant(g))
        self.assertTrue(check_access(self.group.agent_id, self.corey.agent_id,
                                     "read", "chat"))
        # revoke (signed by the original grantor)
        self.assertTrue(revoke(gid, self.corey))
        self.assertTrue(REVOKED.is_revoked(gid))
        # access denied after revocation, both layers
        self.assertFalse(verify_grant(g))
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "read", "chat"))

    def test_revoke_requires_grantor(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        gid = g["grant"]["grant_id"]
        self.assertFalse(revoke(gid, self.guest1))  # not the grantor
        self.assertFalse(revoke(gid, self.group))   # group can't revoke either
        self.assertFalse(REVOKED.is_revoked(gid))
        self.assertTrue(verify_grant(g))  # grant untouched
        self.assertTrue(check_access(self.group.agent_id, self.corey.agent_id,
                                     "read", "chat"))

    def test_revoke_unknown_grant(self):
        self.assertFalse(revoke("gr-does-not-exist", self.corey))

    # ---- 5. silo enforcement: check_access ----------------------------------
    def test_access_no_grant_denied(self):
        # guest2 is trusted but never granted anything
        self.assertFalse(check_access(self.group.agent_id, self.guest2.agent_id,
                                      "read", "chat"))

    def test_access_wrong_scope_denied(self):
        grant(self.corey, self.group.agent_id, "chat:read")
        self.assertTrue(check_access(self.group.agent_id, self.corey.agent_id,
                                     "read", "chat"))
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "write", "chat"))     # not granted
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "approve", "spend"))  # not granted

    def test_access_expired_denied(self):
        grant(self.corey, self.group.agent_id, "chat:read", ttl_seconds=-60)
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "read", "chat"))

    def test_access_tampered_grant_in_store_denied(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        tampered = json.loads(json.dumps(g))
        tampered["grant"]["scope"] = "spend:approve"
        # attacker replaced the stored grant in place (put() now refuses
        # overwrites; an in-process attacker mutates the dict directly)
        GRANTS._grants[g["grant"]["grant_id"]] = tampered
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "read", "chat"))

    def test_grant_store_refuses_duplicate_id(self):
        g = grant(self.corey, self.group.agent_id, "chat:read")
        with self.assertRaises(ValueError):
            GRANTS.put(g)  # same grant_id twice must not silently overwrite

    def test_access_cross_user_denied(self):
        grant(self.corey, self.group.agent_id, "chat:read")
        # guest1's data must NOT be readable under corey's grant
        self.assertFalse(check_access(self.group.agent_id, self.guest1.agent_id,
                                      "read", "chat"))
        # and corey's own access still works (grant is his, not guest1's)
        self.assertTrue(check_access(self.group.agent_id, self.corey.agent_id,
                                     "read", "chat"))

    def test_access_requires_trust(self):
        grant(self.corey, self.group.agent_id, "chat:read")
        self.assertTrue(check_access(self.group.agent_id, self.corey.agent_id,
                                     "read", "chat"))
        set_trust(self.corey.agent_id, "riderxl", False)  # trust withdrawn
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "read", "chat"))

    def test_access_unknown_ids_denied(self):
        grant(self.corey, self.group.agent_id, "chat:read")
        self.assertFalse(check_access("g-nope", self.corey.agent_id, "read", "chat"))
        self.assertFalse(check_access(self.group.agent_id, "p-nope", "read", "chat"))
        # a personal seat cannot pose as the group agent
        self.assertFalse(check_access(self.corey.agent_id, self.corey.agent_id,
                                      "read", "chat"))

    def test_access_unknown_action_scope_denied(self):
        grant(self.corey, self.group.agent_id, "chat:read")
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "delete", "chat"))
        self.assertFalse(check_access(self.group.agent_id, self.corey.agent_id,
                                      "read", "bank"))

    # ---- 6. trust registry ---------------------------------------------------
    def test_trust_defaults_false(self):
        fresh = issue_personal_seat("newbie")
        self.assertFalse(is_trusted(fresh.agent_id, "riderxl"))
        self.assertFalse(link_personal_agent("riderxl", fresh))

    def test_trust_set_and_link(self):
        fresh = issue_personal_seat("newbie")
        set_trust(fresh.agent_id, "riderxl", True)
        self.assertTrue(is_trusted(fresh.agent_id, "riderxl"))
        self.assertTrue(link_personal_agent("riderxl", fresh))
        set_trust(fresh.agent_id, "riderxl", False)
        self.assertFalse(is_trusted(fresh.agent_id, "riderxl"))
        self.assertFalse(link_personal_agent("riderxl", fresh))

    def test_trust_is_per_group(self):
        set_trust(self.corey.agent_id, "other-group", True)
        self.assertTrue(is_trusted(self.corey.agent_id, "other-group"))
        self.assertTrue(is_trusted(self.corey.agent_id, "riderxl"))
        set_trust(self.corey.agent_id, "other-group", False)
        self.assertFalse(is_trusted(self.corey.agent_id, "other-group"))
        self.assertTrue(is_trusted(self.corey.agent_id, "riderxl"))

    def test_link_refuses_nonpersonal(self):
        set_trust(self.group.agent_id, "riderxl", True)
        self.assertFalse(link_personal_agent("riderxl", self.group))

    def test_known_scopes(self):
        self.assertEqual(set(known_scopes()),
                         {"chat:read", "chat:write", "spend:propose", "spend:approve"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
