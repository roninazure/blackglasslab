import unittest

from crypto_markets.contracts import normalize_crypto_contract


def market(question, *, outcomes=None, tokens=None, prices=None, **extra):
    row = {
        "id": "3516862",
        "conditionId": "0xabc",
        "slug": "fixture",
        "question": question,
        "endDate": "2026-08-18T16:00:00Z",
        "outcomes": outcomes or ["Yes", "No"],
        "clobTokenIds": tokens or ["yes-token", "no-token"],
        "outcomePrices": prices or ["0.71", "0.29"],
    }
    row.update(extra)
    return row


class CryptoContractTests(unittest.TestCase):

    def test_normalizes_bitcoin_above_contract(self):
        result = normalize_crypto_contract(
            market(
                "Will the price of Bitcoin be above $64,000 on August 18?"
            ),
            event={"id": "833724"},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.asset, "BTC")
        self.assertEqual(result.contract_type, "ABOVE")
        self.assertEqual(result.strike_usd, 64000.0)
        self.assertEqual(result.expiry_utc, "2026-08-18T16:00:00Z")
        self.assertEqual(result.yes_token, "yes-token")
        self.assertEqual(result.no_token, "no-token")
        self.assertEqual(result.indicative_yes_price, 0.71)
        self.assertEqual(result.indicative_no_price, 0.29)

    def test_normalizes_ethereum_above_contract(self):
        result = normalize_crypto_contract(
            market(
                "Will the price of Ethereum be above $1,800 on August 18?"
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.asset, "ETH")
        self.assertEqual(result.contract_type, "ABOVE")
        self.assertEqual(result.strike_usd, 1800.0)

    def test_normalizes_reach_contract(self):
        result = normalize_crypto_contract(
            market(
                "Will Ethereum reach $2,250 on August 17?"
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.asset, "ETH")
        self.assertEqual(result.contract_type, "REACH")
        self.assertEqual(result.strike_usd, 2250.0)

    def test_normalizes_dip_contract(self):
        result = normalize_crypto_contract(
            market(
                "Will Ethereum dip to $1,900 on August 17?"
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.contract_type, "DIP")
        self.assertEqual(result.strike_usd, 1900.0)

    def test_maps_yes_no_tokens_by_outcome_not_position(self):
        result = normalize_crypto_contract(
            market(
                "Will the price of Bitcoin be above $64,000 on August 18?",
                outcomes=["No", "Yes"],
                tokens=["no-token", "yes-token"],
                prices=["0.29", "0.71"],
            )
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.yes_token, "yes-token")
        self.assertEqual(result.no_token, "no-token")
        self.assertEqual(result.indicative_yes_price, 0.71)

    def test_unsupported_crypto_question_fails_closed(self):
        result = normalize_crypto_contract(
            market(
                "Will Bitcoin outperform Ethereum this week?"
            )
        )

        self.assertIsNone(result)

    def test_missing_expiry_fails_closed(self):
        row = market(
            "Will the price of Bitcoin be above $64,000 on August 18?"
        )
        row.pop("endDate")

        self.assertIsNone(
            normalize_crypto_contract(row)
        )

    def test_non_binary_market_fails_closed(self):
        result = normalize_crypto_contract(
            market(
                "Will the price of Bitcoin be above $64,000 on August 18?",
                outcomes=["Yes", "No", "Maybe"],
                tokens=["yes", "no", "maybe"],
            )
        )

        self.assertIsNone(result)

    def test_missing_condition_id_fails_closed(self):
        row = market(
            "Will the price of Bitcoin be above $64,000 on August 18?"
        )
        row["conditionId"] = ""

        self.assertIsNone(
            normalize_crypto_contract(row)
        )


if __name__ == "__main__":
    unittest.main()
