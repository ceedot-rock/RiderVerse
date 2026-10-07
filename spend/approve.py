"""Spend proposal + approval voting — RiderXL lab prototype (lab-only).

Proposal flow: agent proposes a spend -> members vote approve/deny ->
threshold policy decides. Thresholds: "majority" (default) or "unanimous".

Permissions integration: a permissions callback is INJECTED at
construction with signature permissions(member_id, action) -> bool, where
action == "spend:approve". Worker 1's identity/core.py (grant()/
check_access()) does not exist yet — when it lands, wire it in as e.g.
    lambda member, action: identity.check_access(member, action)
Default is a permissive local stub so the module runs standalone today.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import uuid

ACTION_SPEND_APPROVE = "spend:approve"

POLICY_MAJORITY = "majority"
POLICY_UNANIMOUS = "unanimous"
_POLICIES = (POLICY_MAJORITY, POLICY_UNANIMOUS)

STATUS_OPEN = "OPEN"
STATUS_APPROVED = "APPROVED"
STATUS_DEAD = "DEAD"


class VoteError(Exception):
    """A vote was rejected (non-member, double vote, no grant, closed)."""


class ProposalError(Exception):
    """A proposal was malformed."""


def _permissive_stub(member_id: str, action: str) -> bool:
    """Default permissions callback: permissive, local-only.

    Replaced by the real identity module when it exists. Documents its
    own stand-in nature in every call path that uses it.
    """
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _needed_for_majority(n: int) -> int:
    return n // 2 + 1


class Proposal:
    """One spend proposal. votes: {member_id: "approve"|"deny"}.

    IMMUTABLE after construction (review fix 2026-10-07): external
    attribute writes raise AttributeError. All state transitions go
    through the defined methods below (_record_vote, _set_status,
    _mark_executed), which are the only paths that may mutate — via
    object.__setattr__. Threshold/policy can never be changed by direct
    attribute write.
    """

    def __init__(self, proposal_id: str, group_id: str, description: str,
                 amount_cents: int, rail: str, quote: dict,
                 members: list[str], policy: str, proposed_by: str | None):
        object.__setattr__(self, "_frozen", False)
        self.proposal_id = proposal_id
        self.group_id = group_id
        self.description = description
        self.amount_cents = amount_cents
        self.rail = rail
        self.quote = quote
        self.members = list(members)
        self.policy = policy
        self.proposed_by = proposed_by
        self.created_at = _now_iso()
        self.votes: dict[str, str] = {}
        self.status = STATUS_OPEN
        self.status_reason: str | None = None
        self.executed = False
        self.executed_at: str | None = None
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError(
                "Proposal is immutable after creation; mutate only via "
                "_record_vote() / _set_status() / _mark_executed()")
        object.__setattr__(self, name, value)

    # -- the ONLY sanctioned mutation paths (used by ProposalStore) ------
    def _record_vote(self, member_id: str, decision: str) -> None:
        votes = dict(self.votes)
        votes[member_id] = decision
        object.__setattr__(self, "votes", votes)

    def _set_status(self, status: str, reason: str | None) -> None:
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "status_reason", reason)

    def _mark_executed(self) -> None:
        object.__setattr__(self, "executed", True)
        object.__setattr__(self, "executed_at", _now_iso())

    # -- derived -----------------------------------------------------------
    @property
    def approvals(self) -> list[str]:
        return sorted(m for m, v in self.votes.items() if v == "approve")

    @property
    def denials(self) -> list[str]:
        return sorted(m for m, v in self.votes.items() if v == "deny")

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "group_id": self.group_id,
            "description": self.description,
            "amount_cents": self.amount_cents,
            "rail": self.rail,
            "quote": self.quote,
            "members": self.members,
            "policy": self.policy,
            "proposed_by": self.proposed_by,
            "created_at": self.created_at,
            "votes": dict(self.votes),
            "approvals": self.approvals,
            "denials": self.denials,
            "status": self.status,
            "status_reason": self.status_reason,
            "executed": self.executed,
            "executed_at": self.executed_at,
        }


class ProposalStore:
    """In-memory proposal registry with voting and threshold evaluation.

    The permissions callback is REQUIRED — there is no permissive default
    (review fix 2026-10-07: the old silent stub gave vote-anyone semantics
    to any caller that forgot to inject the real callback). All mutating
    paths take an internal lock so concurrent voters cannot double-vote
    or double-execute.
    """

    def __init__(self, permissions):
        # permissions(member_id, action) -> bool
        if not callable(permissions):
            raise ProposalError(
                "ProposalStore requires a permissions callback "
                "permissions(member_id, action) -> bool; refusing the "
                "permissive default")
        self._permissions = permissions
        self._proposals: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    # -- proposal ----------------------------------------------------------
    def propose(self, group_id: str, description: str, amount_cents: int,
                rail: str, quote: dict, members: list[str],
                policy: str = POLICY_MAJORITY,
                proposed_by: str | None = None) -> Proposal:
        if policy not in _POLICIES:
            raise ProposalError("policy must be one of %r" % (_POLICIES,))
        if not group_id or not isinstance(group_id, str):
            raise ProposalError("group_id required")
        if not description or not isinstance(description, str):
            raise ProposalError("description required")
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ProposalError("amount_cents must be a positive int")
        if not rail or not isinstance(rail, str):
            raise ProposalError("rail required")
        if not members or len(set(members)) != len(members):
            raise ProposalError("members must be a non-empty list of unique ids")

        pid = "sp_" + uuid.uuid4().hex[:12]
        p = Proposal(pid, group_id, description, amount_cents,
                     rail.strip().lower(), quote, list(members), policy,
                     proposed_by)
        with self._lock:
            self._proposals[pid] = p
        return p

    def get(self, proposal_id: str) -> Proposal:
        with self._lock:
            try:
                return self._proposals[proposal_id]
            except KeyError:
                raise ProposalError("unknown proposal %r" % (proposal_id,))

    # -- voting ------------------------------------------------------------
    def approve(self, proposal_id: str, member_id: str) -> Proposal:
        return self._vote(proposal_id, member_id, "approve")

    def deny(self, proposal_id: str, member_id: str) -> Proposal:
        return self._vote(proposal_id, member_id, "deny")

    def _vote(self, proposal_id: str, member_id: str,
              decision: str) -> Proposal:
        # Review fix 2026-10-07: the whole check-then-act sequence holds
        # the store lock — concurrent voters cannot both pass the
        # already-voted / threshold checks.
        with self._lock:
            p = self._proposals.get(proposal_id)
            if p is None:
                raise ProposalError("unknown proposal %r" % (proposal_id,))
            if p.status != STATUS_OPEN:
                raise VoteError("proposal %s is %s — voting closed"
                                % (proposal_id, p.status))
            if member_id not in p.members:
                raise VoteError("member %r is not an eligible voter on %s"
                                % (member_id, proposal_id))
            if member_id in p.votes:
                raise VoteError("member %r already voted (%s) on %s"
                                % (member_id, p.votes[member_id], proposal_id))
            if not self._permissions(member_id, ACTION_SPEND_APPROVE):
                raise VoteError("member %r lacks a valid %r grant"
                                % (member_id, ACTION_SPEND_APPROVE))

            p._record_vote(member_id, decision)
            self._evaluate(p)
            return p

    def _evaluate(self, p: Proposal) -> None:
        n = len(p.members)
        n_approve = len(p.approvals)
        n_deny = len(p.denials)

        if p.policy == POLICY_UNANIMOUS:
            if n_deny > 0:
                p._set_status(STATUS_DEAD, "unanimous veto: %s denied"
                              % ", ".join(p.denials))
            elif n_approve == n:
                p._set_status(STATUS_APPROVED,
                              "unanimous approval (%d/%d)" % (n_approve, n))
            return

        # majority
        needed = _needed_for_majority(n)
        if n_approve >= needed:
            p._set_status(STATUS_APPROVED, "majority approval (%d/%d, needed %d)"
                           % (n_approve, n, needed))
        elif n_deny > n - needed:
            # Mathematically eliminated: remaining votes can't reach needed.
            p._set_status(STATUS_DEAD, "majority unreachable: %d denied, "
                           "needed %d of %d" % (n_deny, needed, n))
