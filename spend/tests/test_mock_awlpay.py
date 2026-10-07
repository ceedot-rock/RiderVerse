"""Tests: RiderXL MockAwlPayClient.

Fee-math parity is checked against the REAL awlpay fee law
(~/workspace/awlpay/server/fees.py) for multiple amounts and tiers.
"""

import importlib.util
import os

import pytest

from ..mock_awlpay import (
    MockAwlPayClient, QuoteRefused, usd_to_cents,
    MOCK_MODE, MOCK_NETWORK, PRODUCT,
)

# The real fee law, imported independently of the module under test.
_REAL_PATH = os.path.expanduser("~/workspace/awlpay/server/fees.py")
_spec = importlib.util.spec_from_file_location("real_fees_test", _REAL_PATH)
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)


@pytest.fixture
def client():
    return MockAwlPayClient()


class TestQuoteFeeMath:
    @pytest.mark.parametrize("cents", [100, 1999, 24000, 1_000_000])
    def test_free_tier_parity_with_real_fees(self, client, cents):
        q = client.quote(cents / 100, "tron-usdt", tier="free")
        fee, status = _real.calculate_fees(cents, _real.FREE, 0, 0)
        assert q["fee_cents"] == fee == cents // 200
        assert q["fee_status"] == status == "free"
        assert q["net_cents"] == cents - fee

    def test_free_tier_exact_example(self, client):
        q = client.quote(240, "base-usdc", tier="free")
        assert q["amount_cents"] == 24000
        assert q["fee_cents"] == 120          # 0.5% of 24000, floored
        assert q["net_cents"] == 23880

    def test_pro_tier_parity_with_real_fees(self, client):
        q = client.quote(100, "lightning", tier="pro")
        fee, status = _real.calculate_fees(10000, _real.PRO, 0, 0)
        assert q["fee_cents"] == fee == 0
        assert q["fee_status"] == status == "pro_under_cap"

    def test_l33t_tier_parity_with_real_fees(self, client):
        q = client.quote(50, "stellar-xlm", tier="l33t")
        fee, status = _real.calculate_fees(5000, _real.L33T, 0, 0)
        assert (q["fee_cents"], q["fee_status"]) == (fee, status) == (0, "l33t")

    def test_pro_overage_parity(self, client):
        q = client.quote(100, "bsc", tier="pro",
                         volume_used_cents=2_999_999)  # pushes over $30k cap
        fee, status = _real.calculate_fees(10000, _real.PRO, 2_999_999, 0)
        assert q["fee_cents"] == fee == 50
        assert q["fee_status"] == status == "pro_overage"


class TestQuoteRefusals:
    def test_negative_amount_refused(self, client):
        with pytest.raises(QuoteRefused, match="negative"):
            client.quote(-5, "tron-usdt")

    def test_dust_refused(self, client):
        # $0.00: net = 0 - 0 <= 0 -> eaten by fees. (Under the 0.5% floor,
        # any positive amount keeps net > 0, so zero is the dust boundary.)
        with pytest.raises(QuoteRefused, match="dust"):
            client.quote(0, "tron-usdt")

    def test_unknown_tier_refused(self, client):
        with pytest.raises(QuoteRefused, match="unknown_tier"):
            client.quote(10, "tron-usdt", tier="diamond")

    def test_empty_rail_refused(self, client):
        with pytest.raises(QuoteRefused, match="rail"):
            client.quote(10, "   ")

    def test_rail_normalized(self, client):
        q = client.quote(10, "  Base-USDC ")
        assert q["rail"] == "base-usdc"


class TestExecute:
    def test_execute_returns_mock_receipt(self, client):
        q = client.quote(240, "tron-usdt")
        r = client.execute(q, description="RiderXL spend proposal: cabin")
        assert r["mode"] == MOCK_MODE == "MOCK"
        assert r["network"] == MOCK_NETWORK == "none"
        assert r["product"] == PRODUCT == "RiderXL"
        assert r["mock_tx_id"].startswith("mock-tx-")
        assert "MOCK" in r["label"]
        assert r["amount_cents"] == 24000
        assert r["quote"] is q

    def test_execute_rejects_non_mock_quote(self, client):
        with pytest.raises(QuoteRefused):
            client.execute({"mode": "REAL", "amount_cents": 100})

    def test_every_line_labeled_mock(self, client):
        q = client.quote(10, "lightning")
        assert q["mode"] == "MOCK" and q["network"] == "none"
        assert "MOCK" in q["label"]


class TestUsdToCents:
    @pytest.mark.parametrize("usd,expected", [
        (240, 24000), (240.0, 24000), ("240", 24000),
        (19.99, 1999), (0.01, 1), (1000000, 100_000_000),
    ])
    def test_exact_conversion(self, usd, expected):
        assert usd_to_cents(usd) == expected
