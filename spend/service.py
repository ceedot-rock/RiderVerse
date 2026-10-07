"""GroupSpendService — RiderXL lab-only spend governance (MOCK).

Full chain: propose (mock quote) -> members vote -> threshold met ->
mock-execute via MockAwlPayClient -> Chamber-sealed mock receipt written
to disk as JSON.

ANTI-INSTINCT RULE (enforced here, tested in tests/test_service.py):
  execute() REFUSES — raises SpendRefused — unless the proposal's status
  is APPROVED. No threshold, no spend. No exceptions, no bypass path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .approve import (
    Proposal, ProposalStore, VoteError, ProposalError,
    STATUS_APPROVED, STATUS_OPEN,
    ACTION_SPEND_APPROVE,
)
from .mock_awlpay import MockAwlPayClient, QuoteRefused, MOCK_MODE, MOCK_NETWORK
from .seal import SealAuthority, verify_with_key_hex


class SpendRefused(Exception):
    """execute() refused: proposal not approved, already executed, or bad."""


class GroupSpendService:
    """Spend governance for one group-agent deployment (lab-only, MOCK)."""

    def __init__(self, receipts_dir: str | None = None,
                 permissions=None,
                 seal_authority: SealAuthority | None = None,
                 client: MockAwlPayClient | None = None):
        # permissions(member_id, action) -> bool; default permissive stub
        # inside ProposalStore until worker 1's identity/core.py lands.
        self.store = ProposalStore(permissions=permissions)
        self.client = client or MockAwlPayClient()
        self.seal_authority = seal_authority or SealAuthority()
        self.receipts_dir = receipts_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "receipts")
        os.makedirs(self.receipts_dir, exist_ok=True)

    # -- propose -----------------------------------------------------------
    def propose(self, group_id: str, description: str, amount_usd,
                rail: str, members: list[str], policy: str = "majority",
                proposed_by: str | None = None,
                tier: str = "free") -> Proposal:
        quote = self.client.quote(amount_usd, rail, tier=tier)  # may raise
        return self.store.propose(
            group_id=group_id, description=description,
            amount_cents=quote["amount_cents"], rail=quote["rail"],
            quote=quote, members=members, policy=policy,
            proposed_by=proposed_by)

    # -- votes (thin pass-through; grant checks live in the store) --------
    def approve(self, proposal_id: str, member_id: str) -> Proposal:
        return self.store.approve(proposal_id, member_id)

    def deny(self, proposal_id: str, member_id: str) -> Proposal:
        return self.store.deny(proposal_id, member_id)

    # -- execute (ANTI-INSTINCT: approval gate) -----------------------------
    def execute(self, proposal_id: str) -> dict:
        """Mock-execute an APPROVED proposal and return the sealed receipt.

        Raises SpendRefused unless status == APPROVED. Raises SpendRefused
        again on a second call (a proposal executes at most once).
        """
        p = self.store.get(proposal_id)
        if p.status != STATUS_APPROVED:
            raise SpendRefused(
                "proposal %s is %s (%s) — threshold not met, no spend"
                % (proposal_id, p.status, p.status_reason))
        if p.executed:
            raise SpendRefused("proposal %s already executed — one spend "
                               "per approval" % proposal_id)

        mock = self.client.execute(p.quote, description=p.description)
        p._mark_executed()

        seal_payload = {
            "product": "RiderXL",
            "mode": MOCK_MODE,
            "network": MOCK_NETWORK,
            "label": "RiderXL MOCK SEALED RECEIPT — no real money moved",
            "proposal_id": p.proposal_id,
            "group_id": p.group_id,
            "description": p.description,
            "amount_cents": p.amount_cents,
            "rail": p.rail,
            "tier": p.quote.get("tier"),
            "fee_cents": p.quote.get("fee_cents"),
            "net_cents": p.quote.get("net_cents"),
            "mock_tx_id": mock["mock_tx_id"],
            "policy": p.policy,
            "approvals": p.approvals,
            "denials": p.denials,
            "status_reason": p.status_reason,
            "executed_at": p.executed_at,
        }
        envelope = self.seal_authority.seal(seal_payload)

        receipt = {
            "product": "RiderXL",
            "mode": MOCK_MODE,
            "network": MOCK_NETWORK,
            "label": ("RiderXL MOCK RECEIPT — sealed with a runtime lab key "
                      "(custody=lab-local-mock). No real money moved, "
                      "no chain broadcast, no production attestation."),
            "proposal": p.to_dict(),
            "votes": {"approve": p.approvals, "deny": p.denials},
            "quote": p.quote,
            "mock_tx_id": mock["mock_tx_id"],
            "seal": envelope,
            "seal_custody": self.seal_authority.custody,
            "seal_kid": self.seal_authority.kid,
            "seal_verify_key_hex": self.seal_authority.verify_key_hex,
        }
        path = os.path.join(self.receipts_dir,
                            "receipt_%s.json" % p.proposal_id)
        with open(path, "w") as f:
            json.dump(receipt, f, indent=2, sort_keys=True)
        receipt["_file"] = path
        return receipt

    def verify_receipt(self, receipt: dict) -> bool:
        """Verify a sealed receipt.

        Two checks, both required:
          1. the envelope signature verifies under the embedded key;
          2. the receipt body's key fields match the SEALED payload —
             a body that disagrees with what was actually sealed fails,
             even if the envelope itself is authentic.
        Never raises — False on any problem.
        """
        try:
            env = receipt.get("seal") or {}
            vkh = (receipt.get("seal_verify_key_hex")
                   or env.get("verify_key_hex"))
            if not vkh or not verify_with_key_hex(env, vkh):
                return False
            sealed = json.loads(env.get("payload", "{}"))
            # body-vs-sealed-payload consistency (receipt's canonical shape:
            # mock_tx_id top-level; proposal/quote/votes nested). Review fix
            # 2026-10-07: expanded from 5 fields to the full audit trail —
            # a body that disagrees with what was actually sealed fails,
            # even if the envelope itself is authentic.
            prop = receipt.get("proposal", {})
            votes = receipt.get("votes", {})
            quote = receipt.get("quote", {})
            checks = [
                (receipt.get("mock_tx_id"), sealed.get("mock_tx_id")),
                (prop.get("proposal_id"), sealed.get("proposal_id")),
                (prop.get("group_id"), sealed.get("group_id")),
                (prop.get("description"), sealed.get("description")),
                (prop.get("policy"), sealed.get("policy")),
                (prop.get("rail"), sealed.get("rail")),
                (prop.get("executed_at"), sealed.get("executed_at")),
                (votes.get("approve"), sealed.get("approvals")),
                (votes.get("deny"), sealed.get("denials")),
                (quote.get("amount_cents"), sealed.get("amount_cents")),
                (quote.get("fee_cents"), sealed.get("fee_cents")),
                (quote.get("net_cents"), sealed.get("net_cents")),
                (quote.get("tier"), sealed.get("tier")),
            ]
            if any(a != b for a, b in checks):
                return False
            return True
        except (ValueError, TypeError, AttributeError):
            return False


__all__ = [
    "GroupSpendService", "SpendRefused", "VoteError", "ProposalError",
    "QuoteRefused", "Proposal", "ProposalStore",
    "STATUS_APPROVED", "STATUS_OPEN", "ACTION_SPEND_APPROVE",
    "MockAwlPayClient", "SealAuthority",
]
