import io
import json
import math
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from crypto_markets.volatility import (
    fetch_coinbase_candles,
    realized_volatility,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode())

    def __exit__(self, *_args):
        return None


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def open(self, request, timeout=None):
        self.request = request
        return FakeResponse(self.payload)


class CryptoVolatilityTests(unittest.TestCase):

    def now(self):
        return datetime(
            2026,
            8,
            17,
            23,
            45,
            tzinfo=timezone.utc,
        )

    def candles(self):
        base = 1787000000
        return [
            [base + 300, 0, 0, 0, 101.0, 10],
            [base, 0, 0, 0, 100.0, 10],
            [base + 600, 0, 0, 0, 99.0, 10],
            [base + 900, 0, 0, 0, 102.0, 10],
        ]

    def test_fetches_sorts_and_normalizes_candles(self):
        opener = FakeOpener(self.candles())

        with patch(
            "crypto_markets.volatility.urllib.request.build_opener",
            return_value=opener,
        ):
            rows = fetch_coinbase_candles(
                "BTC",
                window_hours=1,
                now=self.now(),
            )

        self.assertEqual(
            rows,
            [
                (1787000000, 100.0),
                (1787000300, 101.0),
                (1787000600, 99.0),
                (1787000900, 102.0),
            ],
        )
        self.assertIn("granularity=300", opener.request.full_url)
        self.assertIn("/products/BTC-USD/candles?", opener.request.full_url)

    def test_deduplicates_candle_timestamps(self):
        payload = self.candles()
        base = 1787000000
        payload.append([base + 600, 0, 0, 0, 98.0, 10])

        opener = FakeOpener(payload)

        with patch(
            "crypto_markets.volatility.urllib.request.build_opener",
            return_value=opener,
        ):
            rows = fetch_coinbase_candles(
                "BTC",
                window_hours=1,
                now=self.now(),
            )

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2], (1787000600, 98.0))

    def test_computes_positive_realized_volatility(self):
        opener = FakeOpener(self.candles())

        with patch(
            "crypto_markets.volatility.urllib.request.build_opener",
            return_value=opener,
        ):
            result = realized_volatility(
                "BTC",
                window_hours=1,
                now=self.now(),
            )

        self.assertEqual(result.asset, "BTC")
        self.assertEqual(result.product_id, "BTC-USD")
        self.assertEqual(result.candle_count, 4)
        self.assertEqual(result.return_count, 3)
        self.assertTrue(
            math.isfinite(result.realized_vol_annualized)
        )
        self.assertGreater(
            result.realized_vol_annualized,
            0,
        )

    def test_unsupported_asset_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "unsupported crypto volatility asset",
        ):
            fetch_coinbase_candles("SOL")

    def test_window_over_coinbase_limit_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "300-candle limit",
        ):
            fetch_coinbase_candles(
                "BTC",
                window_hours=48,
                now=self.now(),
            )

    def test_malformed_response_fails_closed(self):
        opener = FakeOpener({"bad": "shape"})

        with patch(
            "crypto_markets.volatility.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "not a list",
            ):
                fetch_coinbase_candles(
                    "BTC",
                    window_hours=1,
                    now=self.now(),
                )

    def test_insufficient_history_fails_closed(self):
        opener = FakeOpener(
            [[1787000000, 0, 0, 0, 100.0, 10]]
        )

        with patch(
            "crypto_markets.volatility.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "insufficient Coinbase candle history",
            ):
                fetch_coinbase_candles(
                    "BTC",
                    window_hours=1,
                    now=self.now(),
                )


if __name__ == "__main__":
    unittest.main()
