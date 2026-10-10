"""RiderXL spend-governance demo — lab-only, MOCK only, zero real money.

Scenario (a): "book the cabin $240 split 3 ways", unanimous policy ->
  all three approve -> sealed RiderXL mock receipt.
Scenario (b): same spend, one member vetoes -> proposal DEAD, no receipt.
Scenario (c): majority policy, 2-of-3 approve with one deny ->
  sealed RiderXL mock receipt recording the dissent.

Writes sample JSON to ~/workspace/group-agent/spend/samples/.
Run:  python3 ~/workspace/group-agent/spend/demo.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

# The parent dir is "group-agent" (hyphenated), so load the "spend"
# package under an importable alias instead of a plain import.
PKG = "riderxl_spend"
pkg = types.ModuleType(PKG)
pkg.__path__ = [HERE]
sys.modules[PKG] = pkg


def _load(modname: str):
    full = PKG + "." + modname
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(HERE, modname + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    setattr(pkg, modname, mod)
    return mod


for _m in ("mock_awlpay", "approve", "seal", "service"):
    _load(_m)
_init_spec = importlib.util.spec_from_file_location(
    PKG, os.path.join(HERE, "__init__.py"))
_init = importlib.util.module_from_spec(_init_spec)
sys.modules[PKG] = _init
_init_spec.loader.exec_module(_init)

GroupSpendService = _init.GroupSpendService
POLICY_UNANIMOUS = _init.POLICY_UNANIMOUS
POLICY_MAJORITY = _init.POLICY_MAJORITY

SAMPLES = os.path.join(HERE, "samples")
os.makedirs(SAMPLES, exist_ok=True)


def scenario_a_unanimous_cabin() -> dict:
    print("RiderXL demo (a): book the cabin, $240 split 3 ways — unanimous")
    svc = GroupSpendService(receipts_dir=os.path.join(HERE, "receipts"),
                              permissions=lambda member_id, action: True)  # lab demo: explicit opt-in
    p = svc.propose(
        group_id="cabin-trip-oct",
        description="RiderXL spend proposal: book the cabin, $240 split 3 ways",
        amount_usd=240, rail="tron-usdt",
        members=["corey", "alice", "bob"],
        policy=POLICY_UNANIMOUS, proposed_by="corey")
    print("  RiderXL spend proposal %s OPEN (unanimous, 3 voters)" % p.proposal_id)
    for m in ("corey", "alice", "bob"):
        svc.approve(p.proposal_id, m)
        print("  vote: %s -> approve" % m)
    print("  status: %s (%s)" % (p.status, p.status_reason))
    receipt = svc.execute(p.proposal_id)
    print("  RiderXL mock receipt sealed: %s" % receipt["mock_tx_id"])
    print("  seal verifies: %s" % svc.verify_receipt(receipt))
    out = os.path.join(SAMPLES, "receipt_cabin_unanimous.json")
    with open(out, "w") as f:
        json.dump({k: v for k, v in receipt.items() if k != "_file"},
                  f, indent=2, sort_keys=True)
    print("  wrote %s" % out)
    return receipt


def scenario_b_veto_kills() -> dict:
    print("RiderXL demo (b): same spend, one member vetoes — unanimous")
    svc = GroupSpendService(receipts_dir=os.path.join(HERE, "receipts"),
                              permissions=lambda member_id, action: True)  # lab demo: explicit opt-in
    p = svc.propose(
        group_id="cabin-trip-oct",
        description="RiderXL spend proposal: book the cabin, $240 split 3 ways",
        amount_usd=240, rail="tron-usdt",
        members=["corey", "alice", "bob"],
        policy=POLICY_UNANIMOUS, proposed_by="corey")
    print("  RiderXL spend proposal %s OPEN" % p.proposal_id)
    svc.approve(p.proposal_id, "corey")
    print("  vote: corey -> approve")
    svc.deny(p.proposal_id, "alice")
    print("  vote: alice -> DENY (veto)")
    print("  status: %s (%s)" % (p.status, p.status_reason))
    dead_record = {
        "product": "RiderXL",
        "mode": "MOCK",
        "network": "none",
        "label": ("RiderXL DEAD PROPOSAL — vetoed, no spend executed, "
                  "no receipt issued, no money moved"),
        "proposal": p.to_dict(),
        "receipt_issued": False,
    }
    out = os.path.join(SAMPLES, "veto_dead_proposal.json")
    with open(out, "w") as f:
        json.dump(dead_record, f, indent=2, sort_keys=True)
    print("  wrote %s (receipt_issued=false)" % out)
    return dead_record


def scenario_c_majority_with_dissent() -> dict:
    print("RiderXL demo (c): majority policy, 2-of-3 approve, one denies")
    svc = GroupSpendService(receipts_dir=os.path.join(HERE, "receipts"),
                              permissions=lambda member_id, action: True)  # lab demo: explicit opt-in
    p = svc.propose(
        group_id="cabin-trip-oct",
        description=("RiderXL spend proposal: book the cabin, $240 split 3 "
                     "ways (majority)"),
        amount_usd=240, rail="lightning",
        members=["corey", "alice", "bob"],
        policy=POLICY_MAJORITY, proposed_by="corey")
    print("  RiderXL spend proposal %s OPEN (majority, 3 voters)" % p.proposal_id)
    svc.approve(p.proposal_id, "corey")
    print("  vote: corey -> approve")
    svc.deny(p.proposal_id, "bob")
    print("  vote: bob -> deny (dissent recorded, not a veto under majority)")
    svc.approve(p.proposal_id, "alice")
    print("  vote: alice -> approve")
    print("  status: %s (%s)" % (p.status, p.status_reason))
    receipt = svc.execute(p.proposal_id)
    print("  RiderXL mock receipt sealed: %s" % receipt["mock_tx_id"])
    print("  seal verifies: %s" % svc.verify_receipt(receipt))
    out = os.path.join(SAMPLES, "receipt_cabin_majority_dissent.json")
    with open(out, "w") as f:
        json.dump({k: v for k, v in receipt.items() if k != "_file"},
                  f, indent=2, sort_keys=True)
    print("  wrote %s" % out)
    return receipt


def main() -> None:
    scenario_a_unanimous_cabin()
    print()
    scenario_b_veto_kills()
    print()
    scenario_c_majority_with_dissent()
    print()
    print("RiderXL demo complete: MOCK only, no real money moved.")


if __name__ == "__main__":
    main()
