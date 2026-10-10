"""Tests: RiderXL ProposalStore — thresholds, voting rules, grant checks."""

import pytest

from ..approve import (
    ProposalStore, VoteError, ProposalError,
    POLICY_MAJORITY, POLICY_UNANIMOUS,
    STATUS_OPEN, STATUS_APPROVED, STATUS_DEAD,
    ACTION_SPEND_APPROVE,
)


def _quote(cents=24000):
    return {"mode": "MOCK", "network": "none", "amount_cents": cents,
            "rail": "tron-usdt", "tier": "free", "fee_cents": 265,
            "net_cents": cents - 265}


@pytest.fixture
def store():
    return ProposalStore(permissions=lambda member_id, action: True)


def _propose(store, members=("a", "b", "c"), policy=POLICY_MAJORITY, **kw):
    return store.propose(group_id="g1", description="RiderXL spend proposal",
                         amount_cents=24000, rail="tron-usdt",
                         quote=_quote(), members=list(members),
                         policy=policy, **kw)


class TestPropose:
    def test_happy_path_open(self, store):
        p = _propose(store)
        assert p.status == STATUS_OPEN
        assert p.proposal_id.startswith("sp_")
        assert p.policy == POLICY_MAJORITY

    def test_bad_policy_rejected(self, store):
        with pytest.raises(ProposalError, match="policy"):
            _propose(store, policy="oligarchy")

    def test_empty_members_rejected(self, store):
        with pytest.raises(ProposalError, match="members"):
            _propose(store, members=[])

    def test_duplicate_members_rejected(self, store):
        with pytest.raises(ProposalError, match="members"):
            _propose(store, members=["a", "a", "b"])

    def test_unknown_proposal(self, store):
        with pytest.raises(ProposalError, match="unknown proposal"):
            store.get("sp_nope")


class TestUnanimous:
    def test_all_approve(self, store):
        p = _propose(store, members=["a", "b"], policy=POLICY_UNANIMOUS)
        store.approve(p.proposal_id, "a")
        assert p.status == STATUS_OPEN
        store.approve(p.proposal_id, "b")
        assert p.status == STATUS_APPROVED
        assert p.approvals == ["a", "b"]

    def test_veto_kills(self, store):
        p = _propose(store, members=["a", "b", "c"],
                     policy=POLICY_UNANIMOUS)
        store.approve(p.proposal_id, "a")
        store.deny(p.proposal_id, "b")
        assert p.status == STATUS_DEAD
        assert "veto" in p.status_reason
        assert p.denials == ["b"]

    def test_single_member_unanimous(self, store):
        p = _propose(store, members=["solo"], policy=POLICY_UNANIMOUS)
        store.approve(p.proposal_id, "solo")
        assert p.status == STATUS_APPROVED


class TestMajority:
    def test_two_of_three(self, store):
        p = _propose(store)
        store.approve(p.proposal_id, "a")
        assert p.status == STATUS_OPEN
        store.approve(p.proposal_id, "b")
        assert p.status == STATUS_APPROVED
        assert "majority" in p.status_reason

    def test_mathematically_eliminated_dies(self, store):
        p = _propose(store)  # needs 2 of 3
        store.deny(p.proposal_id, "a")
        assert p.status == STATUS_OPEN  # still possible: b+c approve
        store.deny(p.proposal_id, "b")
        assert p.status == STATUS_DEAD
        assert "unreachable" in p.status_reason

    def test_three_of_five(self, store):
        p = _propose(store, members=["a", "b", "c", "d", "e"])
        store.approve(p.proposal_id, "a")
        store.approve(p.proposal_id, "c")
        store.approve(p.proposal_id, "e")
        assert p.status == STATUS_APPROVED

    def test_denial_without_elimination_stays_open(self, store):
        p = _propose(store, members=["a", "b", "c", "d"])  # needs 3
        store.deny(p.proposal_id, "a")
        assert p.status == STATUS_OPEN
        store.approve(p.proposal_id, "b")
        store.approve(p.proposal_id, "c")
        store.approve(p.proposal_id, "d")
        assert p.status == STATUS_APPROVED


class TestVoteRejections:
    def test_non_member_vote_rejected(self, store):
        p = _propose(store)
        with pytest.raises(VoteError, match="not an eligible voter"):
            store.approve(p.proposal_id, "mallory")

    def test_double_vote_rejected(self, store):
        p = _propose(store)
        store.approve(p.proposal_id, "a")
        with pytest.raises(VoteError, match="already voted"):
            store.deny(p.proposal_id, "a")

    def test_vote_after_close_rejected(self, store):
        p = _propose(store, members=["a", "b"], policy=POLICY_UNANIMOUS)
        store.approve(p.proposal_id, "a")
        store.approve(p.proposal_id, "b")
        with pytest.raises(VoteError, match="voting closed"):
            store.approve(p.proposal_id, "a")

    def test_revoked_grant_rejected(self, store):
        grants = {"a": True, "b": True, "c": False}  # c's grant revoked
        s = ProposalStore(
            permissions=lambda m, action: grants.get(m, False))
        p = _propose(s)
        with pytest.raises(VoteError, match="lacks a valid"):
            s.approve(p.proposal_id, "c")
        # others still fine
        s.approve(p.proposal_id, "a")
        s.approve(p.proposal_id, "b")
        assert p.status == STATUS_APPROVED

    def test_grant_checked_with_spend_approve_action(self):
        seen = []

        def cb(member_id, action):
            seen.append((member_id, action))
            return True

        s = ProposalStore(permissions=cb)
        p = _propose(s, members=["a"])
        s.approve(p.proposal_id, "a")
        assert seen == [("a", ACTION_SPEND_APPROVE)]

    def test_default_stub_is_permissive(self, store):
        p = _propose(store, members=["anyone"])
        store.approve(p.proposal_id, "anyone")
        assert p.status == STATUS_APPROVED

    def test_proposer_without_grant_cannot_vote(self):
        s = ProposalStore(permissions=lambda m, action: False)
        p = _propose(s, members=["a"], proposed_by="a")
        with pytest.raises(VoteError, match="lacks a valid"):
            s.approve(p.proposal_id, "a")
        assert p.status == STATUS_OPEN

class TestImmutability:
    """Review fix 2026-10-07: Proposal is immutable after creation."""

    def test_direct_status_write_raises(self, store):
        p = _propose(store, members=["a", "b"])
        with pytest.raises(AttributeError):
            p.status = STATUS_APPROVED

    def test_direct_executed_write_raises(self, store):
        p = _propose(store, members=["a", "b"])
        with pytest.raises(AttributeError):
            p.executed = True

    def test_direct_policy_write_raises(self, store):
        p = _propose(store, members=["a", "b"])
        with pytest.raises(AttributeError):
            p.policy = POLICY_UNANIMOUS

    def test_direct_votes_write_raises(self, store):
        p = _propose(store, members=["a", "b"])
        with pytest.raises(AttributeError):
            p.votes = {"a": "approve"}

    def test_mutation_attempt_leaves_status_open(self, store):
        p = _propose(store, members=["a", "b"])
        with pytest.raises(AttributeError):
            p.status = STATUS_APPROVED
        assert p.status == STATUS_OPEN  # untouched by the failed write
