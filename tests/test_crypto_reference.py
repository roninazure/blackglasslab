import io
import json
import unittest
from unittest.mock import patch

from crypto_markets.reference import fetch_coinbase_reference_quote


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
        self.timeout = None

    def open(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload)


class CryptoReferenceTests(unittest.TestCase):

    def fixture(self):
        return {
            "bids": [["64000.10", "1.25", 2]],
            "asks": [["64000.20", "0.80", 3]],
            "sequence": 123,
            "auction_mode": False,
            "auction": None,
            "time": "2026-08-17T23:30:00.123Z",
        }

    def test_fetches_btc_top_of_book(self):
        opener = FakeOpener(self.fixture())

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            quote = fetch_coinbase_reference_quote("BTC")

        self.assertEqual(quote.asset, "BTC")
        self.assertEqual(quote.product_id, "BTC-USD")
        self.assertEqual(quote.bid, 64000.10)
        self.assertEqual(quote.ask, 64000.20)
        self.assertEqual(quote.bid_size, 1.25)
        self.assertEqual(quote.ask_size, 0.80)
        self.assertAlmostEqual(quote.mid, 64000.15)
        self.assertEqual(
            quote.exchange_time_utc,
            "2026-08-17T23:30:00.123000Z",
        )
        self.assertEqual(quote.source, "coinbase_exchange")
        self.assertIn(
            "/products/BTC-USD/book?level=1",
            opener.request.full_url,
        )

    def test_fetches_eth_product(self):
        opener = FakeOpener(self.fixture())

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            quote = fetch_coinbase_reference_quote("eth")

        self.assertEqual(quote.asset, "ETH")
        self.assertEqual(quote.product_id, "ETH-USD")
        self.assertIn(
            "/products/ETH-USD/book?level=1",
            opener.request.full_url,
        )

    def test_unsupported_asset_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "unsupported crypto reference asset",
        ):
            fetch_coinbase_reference_quote("SOL")

    def test_missing_bid_fails_closed(self):
        payload = self.fixture()
        payload["bids"] = []

        opener = FakeOpener(payload)

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "no bids",
            ):
                fetch_coinbase_reference_quote("BTC")

    def test_missing_ask_fails_closed(self):
        payload = self.fixture()
        payload["asks"] = []

        opener = FakeOpener(payload)

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "no asks",
            ):
                fetch_coinbase_reference_quote("BTC")

    def test_crossed_book_fails_closed(self):
        payload = self.fixture()
        payload["bids"] = [["64001", "1", 1]]
        payload["asks"] = [["64000", "1", 1]]

        opener = FakeOpener(payload)

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "invalid Coinbase top of book",
            ):
                fetch_coinbase_reference_quote("BTC")

    def test_missing_timestamp_fails_closed(self):
        payload = self.fixture()
        payload.pop("time")

        opener = FakeOpener(payload)

        with patch(
            "crypto_markets.reference.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "timestamp is required",
            ):
                fetch_coinbase_reference_quote("BTC")


if __name__ == "__main__":
    unittest.main()
