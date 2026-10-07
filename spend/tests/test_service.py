"""Tests: RiderXL GroupSpendService — the full proposal->vote->sealed
receipt chain, including the ANTI-INSTINCT rule (no approval => no spend).
"""

import json
import os

import pytest

from ..service import (
    GroupSpendService, SpendRefused, VoteError, QuoteRefused,
)
from ..approve import STATUS_APPROVED, STATUS_DEAD, POLICY_UNANIMOUS
from ..seal import verify_with_key_hex


@pytest.fixture
def svc(tmp_path):
    return GroupSpendService(receipts_dir=str(tmp_path / "receipts"))


def _cabin(svc, members=("corey", "alice", "bob"), policy="majority"):
    return svc.propose(
        group_id="cabin-trip",
        description="RiderXL spend proposal: book the cabin, $240 split 3 ways",
        amount_usd=240, rail="tron-usdt",
        members=list(members), policy=policy, proposed_by="corey")


class TestAntiInstinct:
    """execute() REFUSES unless the proposal reached threshold."""

    def test_execute_open_proposal_refused(self, svc):
        p = _cabin(svc)
        with pytest.raises(SpendRefused, match="threshold not met"):
            svc.execute(p.proposal_id)

    def test_execute_with_zero_votes_refused(self, svc):
        p = _cabin(svc)
        assert p.votes == {}
        with pytest.raises(SpendRefused, match="OPEN"):
            svc.execute(p.proposal_id)

    def test_execute_dead_proposal_refused(self, svc):
        p = _cabin(svc, policy=POLICY_UNANIMOUS)
        svc.deny(p.proposal_id, "alice")
        assert p.status == STATUS_DEAD
        with pytest.raises(SpendRefused, match="DEAD"):
            svc.execute(p.proposal_id)

    def test_execute_partially_approved_refused(self, svc):
        p = _cabin(svc)  # majority of 3 needs 2
        svc.approve(p.proposal_id, "corey")
        with pytest.raises(SpendRefused, match="threshold not met"):
            svc.execute(p.proposal_id)

    def test_unknown_proposal_refused(self, svc):
        from ..approve import ProposalError
        with pytest.raises(ProposalError):
            svc.execute("sp_doesnotexist")


class TestFullChain:
    def test_majority_chain_sealed_receipt(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        assert p.status == STATUS_APPROVED

        r = svc.execute(p.proposal_id)
        assert r["product"] == "RiderXL"
        assert r["mode"] == "MOCK"
        assert r["network"] == "none"
        assert r["mock_tx_id"].startswith("mock-tx-")
        assert "MOCK" in r["label"]
        assert r["votes"] == {"approve": ["alice", "corey"], "deny": []}
        assert r["quote"]["amount_cents"] == 24000
        assert r["quote"]["fee_cents"] == 120
        assert r["proposal"]["proposal_id"] == p.proposal_id
        assert r["seal_custody"] == "lab-local-mock"

        # seal verifies under the embedded key
        assert svc.verify_receipt(r) is True

        # written to disk as JSON
        path = r["_file"]
        assert os.path.exists(path)
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["mock_tx_id"] == r["mock_tx_id"]

    def test_unanimous_chain(self, svc):
        p = _cabin(svc, policy=POLICY_UNANIMOUS)
        for m in ("corey", "alice", "bob"):
            svc.approve(p.proposal_id, m)
        r = svc.execute(p.proposal_id)
        assert r["votes"]["approve"] == ["alice", "bob", "corey"]
        assert svc.verify_receipt(r) is True

    def test_double_execute_refused(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        svc.execute(p.proposal_id)
        with pytest.raises(SpendRefused, match="already executed"):
            svc.execute(p.proposal_id)

    def test_tampered_receipt_fails_verify(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        r = svc.execute(p.proposal_id)
        tampered = json.loads(json.dumps(r))
        tampered["mock_tx_id"] = "mock-tx-FORGED"
        assert svc.verify_receipt(tampered) is False

    def test_tampered_envelope_payload_fails_verify(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        r = svc.execute(p.proposal_id)
        forged = json.loads(json.dumps(r))
        forged["seal"]["payload"] = forged["seal"]["payload"].replace(
            "24000", "24001", 1)
        assert svc.verify_receipt(forged) is False

    def test_wrong_key_fails_verify(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        r = svc.execute(p.proposal_id)
        forged = dict(r)
        forged["seal_verify_key_hex"] = "00" * 32
        assert svc.verify_receipt(forged) is False

    def test_verify_standalone_with_key_hex(self, svc):
        p = _cabin(svc)
        svc.approve(p.proposal_id, "corey")
        svc.approve(p.proposal_id, "alice")
        r = svc.execute(p.proposal_id)
        env = r["seal"]
        assert verify_with_key_hex(env, env["verify_key_hex"]) is True

    def test_dust_proposal_never_created(self, svc):
        with pytest.raises(QuoteRefused):
            svc.propose(group_id="g", description="RiderXL spend proposal: dust",
                        amount_usd=0, rail="tron-usdt",
                        members=["a", "b"])

    def test_non_member_vote_rejected_end_to_end(self, svc):
        p = _cabin(svc)
        with pytest.raises(VoteError, match="not an eligible voter"):
            svc.approve(p.proposal_id, "mallory")
