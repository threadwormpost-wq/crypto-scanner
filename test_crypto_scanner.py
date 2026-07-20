import unittest

import crypto_scanner


class OpportunityScoreTests(unittest.TestCase):
    def test_score_is_bounded_and_ranks_highest_first(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 1_000_000_000_000,
                "total_volume": 30_000_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 9.0,
            },
            {
                "id": "dogecoin",
                "current_price": 0.1,
                "market_cap": 10_000_000_000,
                "total_volume": 2_000_000_000,
                "price_change_percentage_24h_in_currency": -1.0,
                "price_change_percentage_7d_in_currency": 2.0,
            },
            {
                "id": "ethereum",
                "current_price": 3000,
                "market_cap": 350_000_000_000,
                "total_volume": 12_000_000_000,
                "price_change_percentage_24h_in_currency": 3.0,
                "price_change_percentage_7d_in_currency": 7.0,
            },
        ]

        ranked = crypto_scanner.rank_opportunity(data)

        self.assertEqual([coin[0] for coin in ranked], ["Bitcoin", "Ethereum", "Dogecoin"])
        self.assertTrue(all(0 <= score <= 100 for _, _, score in ranked))

    def test_signal_thresholds(self):
        self.assertEqual(crypto_scanner.get_signal(85), "BUY")
        self.assertEqual(crypto_scanner.get_signal(25), "SELL")
        self.assertEqual(crypto_scanner.get_signal(40), "")

    def test_trend_classification(self):
        bullish = {
            "price_change_percentage_24h_in_currency": 3.5,
            "price_change_percentage_7d_in_currency": 8.0,
        }
        bearish = {
            "price_change_percentage_24h_in_currency": -2.1,
            "price_change_percentage_7d_in_currency": -5.3,
        }
        sideways = {
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": -1.0,
        }

        self.assertEqual(crypto_scanner.get_trend(bullish), "Bullish")
        self.assertEqual(crypto_scanner.get_trend(bearish), "Bearish")
        self.assertEqual(crypto_scanner.get_trend(sideways), "sideways")


if __name__ == "__main__":
    unittest.main()
