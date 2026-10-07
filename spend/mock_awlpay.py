"""RiderXL mock AwLPay client — lab-only spend rail for the group-agent prototype.

MOCK ONLY. Zero real money. This module never touches the network, never
signs a real transaction, and every dict it returns carries "mode": "MOCK"
and "network": "none".

Fee math mirrors the real FeeManager tiers exactly: it imports
~/workspace/awlpay/server/fees.py and uses its calculate_fees() verbatim.
If that file is ever unavailable, it falls back to the locked Free-tier
formula (amount_cents // 100 + 25) — Corey's 2026-09-27 lock: Free = 1.0%
+ $0.25/tx, integer cents, no floats.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP

# --------------------------------------------------------------------------
# Real fee law: import the production FeeManager math so the mock can never
# drift from it. Documented dependency, not a copy-paste.
# --------------------------------------------------------------------------
_AWL_FEES_PATH = os.path.expanduser("~/workspace/awlpay/server/fees.py")

try:
    _spec = importlib.util.spec_from_file_location("awlpay_fees_real", _AWL_FEES_PATH)
    _awl_fees = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_awl_fees)  # type: ignore[union-attr]
    calculate_fees = _awl_fees.calculate_fees
    FREE, PRO, L33T = _awl_fees.FREE, _awl_fees.PRO, _awl_fees.L33T
    _FEE_SOURCE = "real:~/workspace/awlpay/server/fees.py"
except (ImportError, ModuleNotFoundError, FileNotFoundError) as _e:
    # Review fix 2026-10-07: catch ONLY missing-module failures. A broad
    # `except Exception` here would silently mask a real bug (e.g. a
    # signature change) in fees.py and drift fee math without a trace.
    import sys as _sys
    _sys.stderr.write(
        "mock_awlpay: real fees.py unavailable (%s) — using hardcoded "
        "fallback fee formula. Fee math MUST be re-verified against the "
        "real module before any non-mock use.\n" % _e)
    FREE, PRO, L33T = 0, 1, 2
    FREE_BPS_DIV = 100
    FREE_FLAT_CENTS = 25

    def calculate_fees(amount_cents: int, tier: int,  # noqa: D103
                       volume_used_cents: int, txs_used: int):
        if amount_cents < 0:
            return 0, "refused_negative_amount"
        if tier == 0:
            return amount_cents // 100 + 25, "free"
        if tier == 1:
            under = (volume_used_cents + amount_cents <= 3_000_000
                     and txs_used < 500)
            return (0, "pro_under_cap") if under else \
                (amount_cents // 100 + 25, "pro_overage")
        if tier == 2:
            return (0, "l33t_fair_use_review") if txs_used > 127_669 else (0, "l33t")
        raise ValueError("unknown tier %r" % (tier,))

    _FEE_SOURCE = "fallback:locked free-tier formula (1.0% + $0.25)"

TIER_NAMES = {FREE: "free", PRO: "pro", L33T: "l33t"}

MOCK_MODE = "MOCK"
MOCK_NETWORK = "none"
PRODUCT = "RiderXL"  # lab-owner-locked product name (Corey, 2026-10-05)


class QuoteRefused(Exception):
    """Raised when a mock quote is refused (negative/dust/unknown rail)."""


def usd_to_cents(amount_usd) -> int:
    """Exact Decimal conversion — no float cents ambiguity."""
    d = Decimal(str(amount_usd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


class MockAwlPayClient:
    """MOCK AwLPay rail client. No network. No real money. Ever."""

    def quote(self, amount_usd, rail: str, tier: str = "free",
              volume_used_cents: int = 0, txs_used: int = 0) -> dict:
        """Build a mock quote. Raises QuoteRefused on negative/dust/bad rail."""
        if not isinstance(rail, str) or not rail.strip():
            raise QuoteRefused("rail must be a non-empty string")
        rail_norm = rail.strip().lower()

        amount_cents = usd_to_cents(amount_usd)
        if amount_cents < 0:
            raise QuoteRefused("refused_negative_amount")

        tier_id = {v: k for k, v in TIER_NAMES.items()}.get(tier.lower())
        if tier_id is None:
            raise QuoteRefused("unknown_tier:%s" % tier)

        fee_cents, fee_status = calculate_fees(amount_cents, tier_id,
                                               volume_used_cents, txs_used)
        net_cents = amount_cents - fee_cents
        if net_cents <= 0:
            raise QuoteRefused(
                "dust_eaten_by_fees: fee %d >= amount %d on tier %s"
                % (fee_cents, amount_cents, tier))

        return {
            "product": PRODUCT,
            "mode": MOCK_MODE,
            "network": MOCK_NETWORK,
            "label": "RiderXL MOCK QUOTE — no real money, no chain interaction",
            "amount_cents": amount_cents,
            "amount_usd": "%.2f" % (amount_cents / 100),
            "rail": rail_norm,
            "tier": TIER_NAMES[tier_id],
            "fee_cents": fee_cents,
            "fee_status": fee_status,
            "net_cents": net_cents,
            "fee_source": _FEE_SOURCE,
        }

    def execute(self, quote: dict, description: str = "") -> dict:
        """MOCK-execute an approved quote. Returns a MOCK receipt.

        Refuses refused/invalid quotes. Touches no network, moves no money.
        """
        if not isinstance(quote, dict) or quote.get("mode") != MOCK_MODE:
            raise QuoteRefused("execute requires a MOCK quote dict")
        tx_id = "mock-tx-" + uuid.uuid4().hex[:16]
        return {
            "product": PRODUCT,
            "mode": MOCK_MODE,
            "network": MOCK_NETWORK,
            "label": "RiderXL MOCK RECEIPT — no real money moved, "
                     "no chain broadcast",
            "mock_tx_id": tx_id,
            "description": description,
            "rail": quote.get("rail"),
            "amount_cents": quote.get("amount_cents"),
            "fee_cents": quote.get("fee_cents"),
            "net_cents": quote.get("net_cents"),
            "quote": quote,
        }
