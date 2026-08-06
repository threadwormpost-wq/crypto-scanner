import csv
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch

import requests

import atlas_one
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

    def test_rank_opportunity_breaks_ties_by_volume(self):
        data = [
            {
                "id": "bitcoin",
                "market_cap": 1_000_000_000,
                "total_volume": 20_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
            },
            {
                "id": "ethereum",
                "market_cap": 1_000_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
            },
        ]

        ranked = crypto_scanner.rank_opportunity(data)
        self.assertEqual([coin[0] for coin in ranked], ["Ethereum", "Bitcoin"])

    def test_calculate_rsi(self):
        rising_prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122, 124, 126, 128]
        falling_prices = [128, 126, 124, 122, 120, 118, 116, 114, 112, 110, 108, 106, 104, 102, 100]

        self.assertGreater(crypto_scanner.calculate_rsi(rising_prices), 50.0)
        self.assertLess(crypto_scanner.calculate_rsi(falling_prices), 50.0)

    def test_rsi_status_and_score_adjustment(self):
        oversold_entry = {
            "rsi_14": 25.0,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }
        neutral_entry = {
            "rsi_14": 50.0,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }
        overbought_entry = {
            "rsi_14": 75.0,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }

        self.assertEqual(crypto_scanner.get_rsi_status(25.0), "Oversold")
        self.assertEqual(crypto_scanner.get_rsi_status(50.0), "Neutral")
        self.assertEqual(crypto_scanner.get_rsi_status(75.0), "Overbought")
        self.assertGreater(
            crypto_scanner.calculate_opportunity_score(oversold_entry, 10, 10, 100, 100),
            crypto_scanner.calculate_opportunity_score(neutral_entry, 10, 10, 100, 100),
        )
        self.assertLess(
            crypto_scanner.calculate_opportunity_score(overbought_entry, 10, 10, 100, 100),
            crypto_scanner.calculate_opportunity_score(neutral_entry, 10, 10, 100, 100),
        )

    def test_request_with_retry_retries_on_rate_limit(self):
        response = unittest.mock.Mock()
        response.status_code = 429
        response.raise_for_status.side_effect = requests.HTTPError("rate limited")

        with patch("crypto_scanner.requests.get", return_value=response):
            with patch("crypto_scanner.time.sleep"):
                with self.assertRaises(requests.HTTPError):
                    crypto_scanner.request_with_retry("https://example.com")

    def test_request_with_retry_stops_after_max_retries_on_429(self):
        rate_limited_response = unittest.mock.Mock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "60"}

        with patch("crypto_scanner.requests.get", return_value=rate_limited_response) as mock_get:
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                with self.assertRaises(requests.HTTPError):
                    crypto_scanner.request_with_retry("https://example.com", max_retries=5)

        self.assertEqual(mock_get.call_count, 5)
        self.assertEqual(mock_sleep.call_count, 4)

    def test_request_with_retry_succeeds_after_rate_limited_retries(self):
        rate_limited_response = unittest.mock.Mock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "1"}

        success_response = unittest.mock.Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None

        with patch("crypto_scanner.requests.get", side_effect=[rate_limited_response, rate_limited_response, success_response]) as mock_get:
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                response = crypto_scanner.request_with_retry("https://example.com", max_retries=5)

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_request_with_retry_uses_exponential_backoff_without_retry_after(self):
        rate_limited_response = unittest.mock.Mock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {}
        rate_limited_response.raise_for_status.side_effect = requests.HTTPError("rate limited")

        with patch("crypto_scanner.requests.get", return_value=rate_limited_response):
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                with self.assertRaises(requests.HTTPError):
                    crypto_scanner.request_with_retry("https://example.com", max_retries=4)

        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [1, 2, 4])

    def test_request_with_retry_retries_on_temporary_server_error(self):
        server_error_response = unittest.mock.Mock()
        server_error_response.status_code = 503
        server_error_response.headers = {}

        success_response = unittest.mock.Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None

        with patch(
            "crypto_scanner.requests.get",
            side_effect=[server_error_response, success_response],
        ) as mock_get:
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                response = crypto_scanner.request_with_retry("https://example.com", max_retries=5)

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [1])

    def test_request_with_retry_does_not_retry_non_retryable_http_error(self):
        not_found_response = unittest.mock.Mock()
        not_found_response.status_code = 404
        not_found_response.raise_for_status.side_effect = requests.HTTPError("not found")

        with patch("crypto_scanner.requests.get", return_value=not_found_response) as mock_get:
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                with self.assertRaises(requests.HTTPError):
                    crypto_scanner.request_with_retry("https://example.com", max_retries=5)

        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    def test_request_with_retry_retries_on_connection_error_then_succeeds(self):
        success_response = unittest.mock.Mock()
        success_response.status_code = 200
        success_response.raise_for_status.return_value = None

        with patch(
            "crypto_scanner.requests.get",
            side_effect=[requests.ConnectionError("temporary outage"), success_response],
        ) as mock_get:
            with patch("crypto_scanner.time.sleep") as mock_sleep:
                response = crypto_scanner.request_with_retry("https://example.com", max_retries=5)

        self.assertIs(response, success_response)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [1])

    def test_fetch_market_data_uses_cache_and_tracks_reuse(self):
        cache = crypto_scanner.RateLimitedCache()

        response = unittest.mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = [{"id": "bitcoin", "current_price": 50000}]

        with patch("crypto_scanner.requests.get", return_value=response) as mock_get:
            first = crypto_scanner.fetch_market_data(cache)
            second = crypto_scanner.fetch_market_data(cache)

        self.assertEqual(first, second)
        self.assertEqual(mock_get.call_count, 1)

        stats = cache.get_request_stats()
        self.assertEqual(stats["total_api_requests_made"], 1)
        self.assertGreaterEqual(stats["cached_responses_reused"], 1)
        self.assertGreaterEqual(stats["duplicate_requests_avoided"], 1)
        self.assertEqual(stats["source_request_counts"], {"scanner.market_data": 1})

    def test_enrich_market_data_with_rsi_uses_market_snapshot(self):
        data = [
            {
                "id": "bitcoin",
                "price_change_percentage_1h_in_currency": 1.2,
                "price_change_percentage_24h_in_currency": 4.0,
                "price_change_percentage_7d_in_currency": 9.0,
            }
        ]

        with patch("crypto_scanner.fetch_historical_prices") as mock_fetch:
            enriched = crypto_scanner.enrich_market_data_with_rsi(data, cache={})

        self.assertEqual(enriched[0]["rsi_14"], crypto_scanner.estimate_rsi_from_market_data(data[0]))
        mock_fetch.assert_not_called()

    def test_enrich_market_data_with_indicators_uses_market_snapshot_without_candle_download(self):
        cache = crypto_scanner.RateLimitedCache()
        response = unittest.mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 1_000_000_000_000,
                "total_volume": 30_000_000_000,
                "price_change_percentage_1h_in_currency": 0.5,
                "price_change_percentage_24h_in_currency": 3.0,
                "price_change_percentage_7d_in_currency": 7.0,
                "sparkline_in_7d": {
                    "price": [
                        49750.0,
                        49820.0,
                        49900.0,
                        50000.0,
                    ]
                },
            }
        ]

        with patch("crypto_scanner.requests.get", return_value=response) as mock_get:
            market_data = crypto_scanner.fetch_market_data(cache)
            enriched = crypto_scanner.enrich_market_data_with_indicators(market_data, cache=cache)

        self.assertEqual(mock_get.call_count, 1)
        self.assertIn("rsi_14", enriched[0])
        self.assertIn("support_level", enriched[0])
        self.assertIn("resistance_level", enriched[0])
        self.assertIn("support_resistance_status", enriched[0])
        self.assertIn("multi_timeframe", enriched[0])
        stats = cache.get_request_stats()
        self.assertEqual(stats["total_api_requests_made"], 1)
        self.assertEqual(
            stats["source_request_counts"],
            {"scanner.market_data": 1},
        )

    def test_calculate_support_resistance_detects_near_support(self):
        prices = [100.0, 95.0, 99.0, 94.0, 98.0, 105.0, 101.0, 107.0, 103.0]

        levels = crypto_scanner.calculate_support_resistance(prices, 95.5)

        self.assertEqual(levels["support"], 95.0)
        self.assertEqual(levels["resistance"], 99.0)
        self.assertEqual(levels["status"], "Near Support")
        self.assertGreater(levels["score_adjustment"], 0)

    def test_calculate_support_resistance_detects_breaks(self):
        prices = [100.0, 95.0, 99.0, 94.0, 98.0, 105.0, 101.0, 107.0, 103.0]

        breakout = crypto_scanner.calculate_support_resistance(prices, 109.0)
        breakdown = crypto_scanner.calculate_support_resistance(prices, 92.0)

        self.assertEqual(breakout["status"], "Breaking Resistance")
        self.assertEqual(breakdown["status"], "Breaking Support")

    def test_support_resistance_adjustment_influences_opportunity_score(self):
        base_entry = {
            "market_cap": 100_000_000,
            "total_volume": 10_000_000,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
            "rsi_14": 50.0,
            "multi_timeframe_score": 50,
        }
        near_support_entry = dict(base_entry, support_resistance_score_adjustment=4)
        near_resistance_entry = dict(base_entry, support_resistance_score_adjustment=-4)

        near_support_score = crypto_scanner.calculate_opportunity_score(
            near_support_entry,
            10,
            10,
            20_000_000,
            100_000_000,
        )
        near_resistance_score = crypto_scanner.calculate_opportunity_score(
            near_resistance_entry,
            10,
            10,
            20_000_000,
            100_000_000,
        )

        self.assertGreater(near_support_score, near_resistance_score)

    def test_iteration_zero_reuses_initial_market_snapshot(self):
        calls = []

        def fake_fetch_market_data():
            calls.append("fetch")
            return [{"id": "bitcoin"}]

        def fake_enrich(data, cache=None):
            return data

        result = crypto_scanner.get_market_data_for_iteration(
            0,
            [{"id": "bitcoin", "rsi_14": 50.0}],
            fake_fetch_market_data,
            fake_enrich,
            {},
        )

        self.assertEqual(result, [{"id": "bitcoin", "rsi_14": 50.0}])
        self.assertEqual(calls, [])

    def test_top_opportunities_summary(self):
        data = [
            {
                "id": "bitcoin",
                "market_cap": 1_000_000_000,
                "total_volume": 20_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
            },
            {
                "id": "ethereum",
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
            },
            {
                "id": "dogecoin",
                "market_cap": 100_000_000,
                "total_volume": 500_000,
                "price_change_percentage_24h_in_currency": 1.0,
                "price_change_percentage_7d_in_currency": 1.0,
            },
        ]

        summary = crypto_scanner.build_top_opportunities_summary(data)
        self.assertIn("#1 Bitcoin", summary)
        self.assertIn("Opportunity score", summary)
        self.assertIn("Signal: Strong Buy", summary)
        self.assertIn("Volume: High", summary)

    def test_top_opportunity_analysis_uses_highest_ranked_coin(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "support_resistance_explanation": "Price is trading between support and resistance, so the next directional move is still being set up. Nearest support is £48,000.00 and nearest resistance is £52,000.00.",
            },
            {
                "id": "ethereum",
                "current_price": 3000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 45.0,
                "support_level": 2900.0,
                "resistance_level": 3100.0,
            },
            {
                "id": "dogecoin",
                "market_cap": 100_000_000,
                "total_volume": 500_000,
                "price_change_percentage_24h_in_currency": 1.0,
                "price_change_percentage_7d_in_currency": 1.0,
                "rsi_14": 40.0,
                "support_level": 0.09,
                "resistance_level": 0.12,
            },
        ]

        analysis = crypto_scanner.build_top_opportunity_analysis(data)
        self.assertIn("ATLAS ONE ANALYSIS", analysis)
        self.assertIn("Coin: Bitcoin", analysis)
        self.assertIn("Opportunity Score:", analysis)
        self.assertIn("Trend: Bullish", analysis)
        self.assertIn("Signal: Strong Buy", analysis)
        self.assertIn("RSI:", analysis)
        self.assertIn("Nearest Support: £", analysis)
        self.assertIn("Nearest Resistance: £", analysis)
        self.assertIn("Price vs Levels:", analysis)
        self.assertIn("Candlestick Pattern: None Detected", analysis)
        self.assertIn("Multi-timeframe:", analysis)
        self.assertIn("Timeframes:", analysis)
        self.assertIn("Volume: High", analysis)
        self.assertIn("Why support/resistance matters:", analysis)
        self.assertIn("Nearest support is £", analysis)
        self.assertIn("Why it is ranked first:", analysis)
        self.assertIn("Suggested Action:", analysis)
        self.assertIn("Confidence:", analysis)
        self.assertIn("Strategy Scorecard", analysis)
        self.assertIn("Trend Alignment:", analysis)
        self.assertIn("RSI Status:", analysis)
        self.assertIn("Volume Confirmation:", analysis)
        self.assertIn("Support/Resistance Position:", analysis)
        self.assertIn("Risk/Reward Quality:", analysis)
        self.assertIn("Momentum:", analysis)
        self.assertIn("Candlestick Pattern:", analysis)
        self.assertIn("Total Strategy Score:", analysis)
        self.assertIn("Recommendation Rationale:", analysis)

    def test_enrich_market_data_with_support_resistance_uses_sparkline_history(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 95.5,
                "sparkline_in_7d": {
                    "price": [100.0, 95.0, 99.0, 94.0, 98.0, 105.0, 101.0, 107.0, 103.0]
                },
            }
        ]

        with patch("atlas_one.fetch_historical_ohlc_prices") as mock_fetch:
            enriched = atlas_one.enrich_market_data_with_support_resistance(data)

        self.assertEqual(enriched[0]["support_level"], 95.0)
        self.assertEqual(enriched[0]["resistance_level"], 99.0)
        self.assertEqual(enriched[0]["support_resistance_status"], "Near Support")
        mock_fetch.assert_not_called()

    def test_enrich_market_data_with_support_resistance_falls_back_to_market_chart_when_sparkline_is_insufficient(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 95.5,
                "sparkline_in_7d": {"price": [95.0]},
            }
        ]

        price_points = [
            [1, 100.0, 101.0, 99.0, 100.0],
            [2, 100.0, 100.5, 94.0, 95.0],
            [3, 95.0, 99.0, 94.5, 98.0],
            [4, 98.0, 106.0, 97.5, 105.0],
            [5, 105.0, 107.0, 100.0, 103.0],
        ]

        with patch("atlas_one.fetch_historical_ohlc_prices", return_value=atlas_one.normalize_ohlc_candles(price_points)) as mock_fetch:
            enriched = atlas_one.enrich_market_data_with_support_resistance(data)

        self.assertEqual(enriched[0]["support_level"], 94.0)
        self.assertEqual(enriched[0]["resistance_level"], 99.0)
        self.assertEqual(enriched[0]["support_resistance_status"], "Near Support")
        mock_fetch.assert_called_once_with("bitcoin", cache=None)

    def test_fetch_historical_ohlc_prices_uses_cache(self):
        cache = crypto_scanner.RateLimitedCache()
        response = unittest.mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "prices": [[index, float(index)] for index in range(1, 201)]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, ".atlas_one_ohlc_cache.json")
            with patch("atlas_one.OHLC_PERSISTENT_CACHE_FILE", cache_path):
                with patch("crypto_scanner.requests.get", return_value=response) as mock_get:
                    first = crypto_scanner.fetch_historical_ohlc_prices("bitcoin", cache)
                    second = crypto_scanner.fetch_historical_ohlc_prices("bitcoin", cache)

        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 100)

    def test_fetch_historical_ohlc_prices_uses_persistent_cache_hit(self):
        cache = crypto_scanner.RateLimitedCache()
        expected_candles = [{"timestamp": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, ".atlas_one_ohlc_cache.json")
            with open(cache_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "bitcoin": {
                            "fetched_at": time.time(),
                            "candles": expected_candles,
                        }
                    },
                    file_obj,
                )

            with patch("atlas_one.OHLC_PERSISTENT_CACHE_FILE", cache_path):
                with patch("crypto_scanner.requests.get") as mock_get:
                    result = crypto_scanner.fetch_historical_ohlc_prices("bitcoin", cache)

        self.assertEqual(result, expected_candles)
        mock_get.assert_not_called()

    def test_fetch_historical_ohlc_prices_persistent_cache_miss_fetches_and_persists(self):
        cache = crypto_scanner.RateLimitedCache()
        response = unittest.mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "prices": [[index, float(index)] for index in range(1, 201)]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, ".atlas_one_ohlc_cache.json")
            with patch("atlas_one.OHLC_PERSISTENT_CACHE_FILE", cache_path):
                with patch("crypto_scanner.requests.get", return_value=response) as mock_get:
                    result = crypto_scanner.fetch_historical_ohlc_prices("bitcoin", cache)

                self.assertTrue(os.path.exists(cache_path))
                with open(cache_path, "r", encoding="utf-8") as file_obj:
                    persisted = json.load(file_obj)

        self.assertEqual(mock_get.call_count, 1)
        self.assertGreaterEqual(len(result), 100)
        self.assertIn("bitcoin", persisted)
        self.assertIn("candles", persisted["bitcoin"])

    def test_fetch_historical_ohlc_prices_expired_persistent_cache_fetches_network(self):
        cache = crypto_scanner.RateLimitedCache()
        stale_candles = [{"timestamp": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]
        response = unittest.mock.Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "prices": [[index, float(index)] for index in range(1, 201)]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, ".atlas_one_ohlc_cache.json")
            with open(cache_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "bitcoin": {
                            "fetched_at": time.time() - (atlas_one.OHLC_PRICE_TTL + 1),
                            "candles": stale_candles,
                        }
                    },
                    file_obj,
                )

            with patch("atlas_one.OHLC_PERSISTENT_CACHE_FILE", cache_path):
                with patch("crypto_scanner.requests.get", return_value=response) as mock_get:
                    result = crypto_scanner.fetch_historical_ohlc_prices("bitcoin", cache)

        self.assertEqual(mock_get.call_count, 1)
        self.assertGreaterEqual(len(result), 100)

    def test_trade_plan_is_reported_separately(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
            }
        ]

        trade_plan = crypto_scanner.build_trade_plan(data)
        self.assertIn("Trade Plan", trade_plan)
        self.assertIn("Risk Level:", trade_plan)

    def test_position_size_calculator_is_added_below_trade_plan(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
            }
        ]

        position_size = crypto_scanner.build_position_size_calculator(data)
        self.assertIn("Position Size Calculator", position_size)
        self.assertIn("Account Size:", position_size)
        self.assertIn("Risk Per Trade:", position_size)
        self.assertIn("Maximum £ Risk:", position_size)
        self.assertIn("Suggested Position Size:", position_size)
        self.assertIn("Risk/Reward Ratio:", position_size)
        self.assertIn("Estimated Profit at Take Profit 1:", position_size)
        self.assertIn("Estimated Profit at Take Profit 2:", position_size)
        self.assertIn("Maximum Loss if Stop Loss is hit:", position_size)

    def test_trade_plan_uses_gbp_currency_symbols(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
            }
        ]

        trade_plan = crypto_scanner.build_trade_plan(data)
        self.assertIn("Current Price: £", trade_plan)
        self.assertIn("Entry Zone: £", trade_plan)
        self.assertIn("Stop Loss: £", trade_plan)
        self.assertIn("Take Profit 1: £", trade_plan)
        self.assertIn("Take Profit 2: £", trade_plan)

    def test_rating_uses_score_and_trend(self):
        self.assertEqual(crypto_scanner.get_rating(85, "Bullish"), "Strong Buy")
        self.assertEqual(crypto_scanner.get_rating(70, "Bullish"), "Buy")
        self.assertEqual(crypto_scanner.get_rating(50, "Bullish"), "Buy")
        self.assertEqual(crypto_scanner.get_rating(45, "sideways"), "Hold")
        self.assertEqual(crypto_scanner.get_rating(25, "Bearish"), "Strong Sell")
        self.assertEqual(crypto_scanner.get_rating(30, "Bearish"), "Sell")

    def test_volume_status_and_score_boost(self):
        high_volume_entry = {
            "market_cap": 100_000_000,
            "total_volume": 20_000_000,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }
        normal_volume_entry = {
            "market_cap": 100_000_000,
            "total_volume": 1_000_000,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }
        low_volume_entry = {
            "market_cap": 100_000_000,
            "total_volume": 500_000,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
        }

        self.assertEqual(crypto_scanner.get_volume_status(high_volume_entry), "High")
        self.assertEqual(crypto_scanner.get_volume_status(normal_volume_entry), "Normal")
        self.assertEqual(crypto_scanner.get_volume_status(low_volume_entry), "Low")
        self.assertGreater(
            crypto_scanner.calculate_opportunity_score(high_volume_entry, 10, 10, 20_000_000, 100_000_000),
            crypto_scanner.calculate_opportunity_score(normal_volume_entry, 10, 10, 20_000_000, 100_000_000),
        )

    def test_multi_timeframe_analysis_outputs_15m_1h_4h(self):
        base_ts = 1_700_000_000_000
        prices = []
        for minute in range(0, 241):
            prices.append((base_ts + (minute * 60 * 1000), 100.0 + (minute * 0.05)))

        analysis = crypto_scanner.analyze_multi_timeframe(prices)

        self.assertIn("timeframes", analysis)
        self.assertIn("15m", analysis["timeframes"])
        self.assertIn("1h", analysis["timeframes"])
        self.assertIn("4h", analysis["timeframes"])
        self.assertIn("composite_score", analysis)
        self.assertIn("composite_trend", analysis)
        self.assertEqual(analysis["composite_trend"], "Bullish")
        self.assertGreaterEqual(analysis["composite_score"], 50)

    def test_multi_timeframe_score_influences_opportunity_score(self):
        base_entry = {
            "market_cap": 100_000_000,
            "total_volume": 10_000_000,
            "price_change_percentage_24h_in_currency": 2.0,
            "price_change_percentage_7d_in_currency": 2.0,
            "rsi_14": 50.0,
        }
        bullish_mtf_entry = dict(base_entry, multi_timeframe_score=90)
        bearish_mtf_entry = dict(base_entry, multi_timeframe_score=10)

        bullish_score = crypto_scanner.calculate_opportunity_score(
            bullish_mtf_entry,
            10,
            10,
            20_000_000,
            100_000_000,
        )
        bearish_score = crypto_scanner.calculate_opportunity_score(
            bearish_mtf_entry,
            10,
            10,
            20_000_000,
            100_000_000,
        )

        self.assertGreater(bullish_score, bearish_score)

    def test_enrich_market_data_with_multi_timeframe_adds_analysis(self):
        data = [
            {
                "id": "bitcoin",
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 4.8,
                "price_change_percentage_7d_in_currency": 8.0,
            }
        ]

        with patch("crypto_scanner.fetch_intraday_prices") as mock_fetch:
            enriched = crypto_scanner.enrich_market_data_with_multi_timeframe(data, cache={})

        self.assertIn("multi_timeframe", enriched[0])
        self.assertIn("multi_timeframe_score", enriched[0])
        self.assertIn("15m", enriched[0]["multi_timeframe"]["timeframes"])
        self.assertIn("1h", enriched[0]["multi_timeframe"]["timeframes"])
        self.assertIn("4h", enriched[0]["multi_timeframe"]["timeframes"])
        self.assertEqual(
            enriched[0]["multi_timeframe"]["timeframes"]["1h"]["change_percent"],
            data[0]["price_change_percentage_1h_in_currency"],
        )
        mock_fetch.assert_not_called()

    def test_table_includes_signal_column(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 1_000_000_000_000,
                "total_volume": 30_000_000_000,
                "price_change_percentage_1h_in_currency": 0.5,
                "price_change_percentage_24h_in_currency": 3.0,
                "price_change_percentage_7d_in_currency": 7.0,
            }
        ]

        table = crypto_scanner.build_table(data)
        headers = [column.header for column in table.columns]

        self.assertIn("Signal", headers)
        self.assertIn("Volume", headers)
        self.assertIn("RSI", headers)
        self.assertNotIn("Buy Signal", headers)
        self.assertNotIn("Sell Signal", headers)
        self.assertIsNone(table.title)

    def test_main_renders_final_table_once_after_all_iterations(self):
        initial_market_data = [{"id": "bitcoin", "current_price": 50000}]
        refreshed_market_data = [{"id": "bitcoin", "current_price": 51000}]
        initial_enriched = [{"id": "bitcoin", "current_price": 50000, "rsi_14": 55.0}]
        final_enriched = [{"id": "bitcoin", "current_price": 51000, "rsi_14": 58.0}]

        mock_console = unittest.mock.Mock()
        sentinel_table = object()
        sentinel_dashboard = "portfolio dashboard"
        sentinel_analysis = "analysis"
        sentinel_trade_plan = "trade plan"
        sentinel_position_size = "position size"

        with patch(
            "atlas_one.argparse.ArgumentParser.parse_args",
            return_value=crypto_scanner.argparse.Namespace(refresh_interval=0, iterations=2),
        ), patch("atlas_one.Console", return_value=mock_console), patch(
            "atlas_one.fetch_market_data",
            side_effect=[initial_market_data, refreshed_market_data],
        ) as mock_fetch_market_data, patch(
            "atlas_one.enrich_market_data_with_indicators",
            side_effect=[initial_enriched, final_enriched],
        ) as mock_enrich, patch(
            "atlas_one.build_portfolio_dashboard",
            return_value=sentinel_dashboard,
        ), patch(
            "atlas_one.build_top_opportunity_analysis",
            return_value=sentinel_analysis,
        ), patch(
            "atlas_one.process_paper_trades",
        ) as mock_process_paper_trades, patch(
            "atlas_one.build_trade_plan",
            return_value=sentinel_trade_plan,
        ), patch(
            "atlas_one.build_position_size_calculator",
            return_value=sentinel_position_size,
        ), patch("atlas_one.build_table", return_value=sentinel_table) as mock_build_table, patch(
            "atlas_one.log_request_audit"
        ), patch("atlas_one.time.sleep"):
            atlas_one.main()

        self.assertEqual(mock_fetch_market_data.call_count, 2)
        self.assertEqual(mock_enrich.call_count, 2)
        self.assertEqual(mock_process_paper_trades.call_count, 2)
        mock_build_table.assert_called_once_with(initial_enriched)
        printed_values = [call_args.args[0] for call_args in mock_console.print.call_args_list if call_args.args]
        self.assertEqual(printed_values.count(sentinel_table), 1)
        dashboard_index = printed_values.index(sentinel_dashboard)
        top_index = printed_values.index("[bold cyan]TOP OPPORTUNITIES[/bold cyan]")
        self.assertLess(dashboard_index, top_index)
        self.assertEqual(printed_values[top_index + 1], sentinel_table)
        self.assertEqual(printed_values[top_index + 2], sentinel_analysis)
        self.assertEqual(printed_values[top_index + 3], sentinel_trade_plan)
        self.assertEqual(printed_values[top_index + 4], sentinel_position_size)

    def test_build_portfolio_dashboard_displays_required_metrics(self):
        snapshot = {
            "starting_balance": 10000.0,
            "available_cash": 9250.5,
            "invested_capital": 749.5,
            "current_portfolio_value": 810.0,
            "total_equity": 10060.5,
            "realized_profit_loss": 120.25,
            "unrealized_profit_loss": 60.5,
            "open_trade_count": 2,
            "closed_trade_count": 1,
        }

        with patch("atlas_one.load_trade_journal_rows", return_value=[]), patch(
            "atlas_one.calculate_portfolio_snapshot", return_value=snapshot
        ):
            dashboard = atlas_one.build_portfolio_dashboard(data=[])

        self.assertIn("PORTFOLIO DASHBOARD", dashboard)
        self.assertIn("Starting Balance: £10,000.00", dashboard)
        self.assertIn("Available Cash: £9,250.50", dashboard)
        self.assertIn("Invested Capital: £749.50", dashboard)
        self.assertIn("Total Equity: £10,060.50", dashboard)
        self.assertIn("Realised P/L: £120.25", dashboard)
        self.assertIn("Unrealised P/L: £60.50", dashboard)
        self.assertIn("Open Positions: 2", dashboard)
        self.assertIn("No open paper positions.", dashboard)

    def test_build_portfolio_dashboard_displays_open_positions_table(self):
        snapshot = {
            "starting_balance": 10000.0,
            "available_cash": 9000.0,
            "invested_capital": 1000.0,
            "current_portfolio_value": 1200.0,
            "total_equity": 10200.0,
            "realized_profit_loss": 0.0,
            "unrealized_profit_loss": 200.0,
            "open_trade_count": 1,
            "closed_trade_count": 0,
        }
        trade_rows = [
            {
                "Coin": "Bitcoin",
                "Trade Status": "Open",
                "Position Size": "2.0",
                "Entry Price": "£100.00",
            }
        ]
        market_data = [{"id": "bitcoin", "current_price": 150.0}]

        with patch("atlas_one.load_trade_journal_rows", return_value=trade_rows), patch(
            "atlas_one.calculate_portfolio_snapshot", return_value=snapshot
        ), patch("atlas_one.get_usd_to_gbp_rate", return_value=1.0):
            dashboard = atlas_one.build_portfolio_dashboard(data=market_data)

        self.assertIn("Open Positions", dashboard)
        self.assertIn("Coin", dashboard)
        self.assertIn("Quantity", dashboard)
        self.assertIn("Entry Price", dashboard)
        self.assertIn("Current Price", dashboard)
        self.assertIn("Unrealised P/L", dashboard)
        self.assertIn("Bitcoin", dashboard)
        self.assertIn("2.00000000", dashboard)
        self.assertIn("£100.00", dashboard)
        self.assertIn("£150.00", dashboard)
        self.assertIn("£100.00", dashboard)
        self.assertNotIn("No open paper positions.", dashboard)

    def test_build_portfolio_dashboard_shows_no_open_positions_message(self):
        snapshot = {
            "starting_balance": 10000.0,
            "available_cash": 10000.0,
            "invested_capital": 0.0,
            "current_portfolio_value": 0.0,
            "total_equity": 10000.0,
            "realized_profit_loss": 0.0,
            "unrealized_profit_loss": 0.0,
            "open_trade_count": 0,
            "closed_trade_count": 0,
        }

        with patch("atlas_one.load_trade_journal_rows", return_value=[]), patch(
            "atlas_one.calculate_portfolio_snapshot", return_value=snapshot
        ):
            dashboard = atlas_one.build_portfolio_dashboard(data=[])

        self.assertIn("Open Positions", dashboard)
        self.assertIn("No open paper positions.", dashboard)

    def test_trade_journal_is_created_with_expected_headers(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )

            self.assertTrue(inserted)
            self.assertTrue(os.path.exists(journal_path))

            with open(journal_path, newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Date/Time"], "2026-07-27 12:00:00")
        self.assertEqual(rows[0]["Coin"], "Bitcoin")
        self.assertIn("Opportunity Score", rows[0])
        self.assertIn("Strategy Score", rows[0])
        self.assertIn("Suggested Action", rows[0])
        self.assertIn("Confidence", rows[0])
        self.assertIn("Current Price", rows[0])
        self.assertIn("Entry Price", rows[0])
        self.assertIn("Entry Zone", rows[0])
        self.assertIn("Stop Loss", rows[0])
        self.assertIn("Take Profit 1", rows[0])
        self.assertIn("Take Profit 2", rows[0])
        self.assertIn("Risk/Reward Ratio", rows[0])
        self.assertIn("Position Size", rows[0])
        self.assertIn("Risk Level", rows[0])
        self.assertIn("Trend", rows[0])
        self.assertIn("RSI", rows[0])
        self.assertIn("Volume", rows[0])
        self.assertIn("Recommendation Rationale", rows[0])
        self.assertIn("Trade Status", rows[0])
        self.assertIn("Exit Price", rows[0])
        self.assertIn("Exit Time", rows[0])
        self.assertIn("Exit Reason", rows[0])
        self.assertIn("Profit/Loss (£)", rows[0])
        self.assertIn("Profit/Loss (%)", rows[0])
        self.assertIn("Trade Duration", rows[0])
        self.assertIn("Notes", rows[0])
        self.assertEqual(rows[0]["Trade Status"], "Open")
        self.assertEqual(rows[0]["Current Price"], rows[0]["Entry Price"])
        self.assertNotEqual(rows[0]["Position Size"], "")
        self.assertEqual(rows[0]["Exit Price"], "")
        self.assertEqual(rows[0]["Exit Time"], "")
        self.assertEqual(rows[0]["Exit Reason"], "Not Triggered")
        self.assertEqual(rows[0]["Profit/Loss (£)"], "£0.00")
        self.assertEqual(rows[0]["Profit/Loss (%)"], "0.00%")
        self.assertEqual(rows[0]["Trade Duration"], "")
        self.assertEqual(rows[0]["Notes"], "")

    def test_trade_journal_deduplicates_entries_within_same_scan(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            seen_entries: set[tuple] = set()

            first_insert = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=seen_entries,
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )
            second_insert = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=seen_entries,
                timestamp=datetime(2026, 7, 27, 12, 0, 1),
            )

            with open(journal_path, newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertTrue(first_insert)
        self.assertFalse(second_insert)
        self.assertEqual(len(rows), 1)

    def test_record_trade_journal_entry_uses_portfolio_allocation_for_position_size(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch("atlas_one.get_usd_to_gbp_rate", return_value=0.79):
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
                starting_balance=10_000.0,
                position_size_pct=0.10,
            )
            rows = atlas_one.load_trade_journal_rows(journal_path)
            snapshot = atlas_one.calculate_portfolio_snapshot(
                data=data,
                trade_rows=rows,
                starting_balance=10_000.0,
            )

        self.assertTrue(inserted)
        self.assertEqual(len(rows), 1)
        position_units = atlas_one._parse_trade_journal_units(rows[0]["Position Size"])
        entry_price = atlas_one._parse_trade_journal_currency(rows[0]["Entry Price"])
        self.assertAlmostEqual(position_units * entry_price, 1_000.0, places=2)
        self.assertAlmostEqual(snapshot["available_cash"], 9_000.0, places=2)
        self.assertAlmostEqual(snapshot["invested_capital"], 1_000.0, places=2)
        self.assertAlmostEqual(snapshot["current_portfolio_value"], 1_000.0, places=2)
        self.assertAlmostEqual(snapshot["total_equity"], 10_000.0, places=2)

    def test_calculate_portfolio_snapshot_tracks_multiple_open_trades(self):
        bitcoin_data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]
        ethereum_data = [
            {
                "id": "ethereum",
                "current_price": 3000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.6,
                "price_change_percentage_24h_in_currency": 4.0,
                "price_change_percentage_7d_in_currency": 6.0,
                "rsi_14": 54.0,
                "support_level": 2900.0,
                "resistance_level": 3200.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 70,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.6},
                        "1h": {"trend": "Bullish", "change_percent": 1.1},
                        "4h": {"trend": "Bullish", "change_percent": 2.4},
                    },
                },
            }
        ]
        combined_market_data = bitcoin_data + ethereum_data

        with tempfile.TemporaryDirectory() as temp_dir, patch("atlas_one.get_usd_to_gbp_rate", return_value=0.79):
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            first_insert = atlas_one.record_trade_journal_entry(
                bitcoin_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
                starting_balance=10_000.0,
                position_size_pct=0.25,
            )
            second_insert = atlas_one.record_trade_journal_entry(
                ethereum_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 5, 0),
                starting_balance=10_000.0,
                position_size_pct=0.25,
            )
            snapshot = atlas_one.calculate_portfolio_snapshot(
                data=combined_market_data,
                journal_path=journal_path,
                starting_balance=10_000.0,
            )

        self.assertTrue(first_insert)
        self.assertTrue(second_insert)
        self.assertEqual(snapshot["open_trade_count"], 2)
        self.assertAlmostEqual(snapshot["available_cash"], 5_625.0, places=2)
        self.assertAlmostEqual(snapshot["invested_capital"], 4_375.0, places=2)
        self.assertAlmostEqual(snapshot["current_portfolio_value"], 4_375.0, places=2)
        self.assertAlmostEqual(snapshot["total_equity"], 10_000.0, places=2)

    def test_record_trade_journal_entry_rejects_new_trade_when_cash_is_fully_committed(self):
        bitcoin_data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]
        ethereum_data = [
            {
                "id": "ethereum",
                "current_price": 3000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.6,
                "price_change_percentage_24h_in_currency": 4.0,
                "price_change_percentage_7d_in_currency": 6.0,
                "rsi_14": 54.0,
                "support_level": 2900.0,
                "resistance_level": 3200.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 70,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.6},
                        "1h": {"trend": "Bullish", "change_percent": 1.1},
                        "4h": {"trend": "Bullish", "change_percent": 2.4},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir, patch("atlas_one.get_usd_to_gbp_rate", return_value=0.79):
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            first_insert = atlas_one.record_trade_journal_entry(
                bitcoin_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
                starting_balance=1_000.0,
                position_size_pct=1.0,
            )
            second_insert = atlas_one.record_trade_journal_entry(
                ethereum_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 5, 0),
                starting_balance=1_000.0,
                position_size_pct=1.0,
            )
            snapshot = atlas_one.calculate_portfolio_snapshot(
                data=bitcoin_data,
                journal_path=journal_path,
                starting_balance=1_000.0,
            )

        self.assertTrue(first_insert)
        self.assertFalse(second_insert)
        self.assertAlmostEqual(snapshot["available_cash"], 0.0, places=2)
        self.assertAlmostEqual(snapshot["invested_capital"], 1_000.0, places=2)

    def test_portfolio_helpers_reject_invalid_configuration(self):
        with self.assertRaises(ValueError):
            atlas_one.calculate_position_allocation(1_000.0, position_size_pct=0)

        with self.assertRaises(ValueError):
            atlas_one.calculate_position_allocation(1_000.0, position_size_pct=1.1)

        with self.assertRaises(ValueError):
            atlas_one.calculate_portfolio_snapshot(starting_balance=-1)

    def test_trade_journal_migrates_legacy_headers_and_preserves_rows(self):
        legacy_headers = [
            "Date/Time",
            "Coin",
            "Opportunity Score",
            "Strategy Score",
            "Suggested Action",
            "Confidence",
            "Current Price",
            "Entry Zone",
            "Stop Loss",
            "Take Profit 1",
            "Take Profit 2",
            "Risk/Reward Ratio",
            "Risk Level",
            "Trend",
            "RSI",
            "Volume",
            "Recommendation Rationale",
        ]
        legacy_row = {
            "Date/Time": "2026-07-26 09:00:00",
            "Coin": "Bitcoin",
            "Opportunity Score": "80",
            "Strategy Score": "74",
            "Suggested Action": "BUY",
            "Confidence": "90%",
            "Current Price": "£40,000.00",
            "Entry Zone": "£39,000.00 - £39,500.00",
            "Stop Loss": "£38,000.00",
            "Take Profit 1": "£42,000.00",
            "Take Profit 2": "£44,000.00",
            "Risk/Reward Ratio": "1.70",
            "Risk Level": "Low",
            "Trend": "Bullish",
            "RSI": "55.0 (Neutral)",
            "Volume": "High",
            "Recommendation Rationale": "Legacy rationale",
        }
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with open(journal_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=legacy_headers)
                writer.writeheader()
                writer.writerow(legacy_row)

            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )

            with open(journal_path, newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                rows = list(reader)
                headers = reader.fieldnames or []

        self.assertTrue(inserted)
        self.assertEqual(headers, atlas_one.TRADE_JOURNAL_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Coin"], "Bitcoin")
        self.assertEqual(rows[0]["Recommendation Rationale"], "Legacy rationale")
        self.assertEqual(rows[0]["Trade Status"], "")
        self.assertEqual(rows[0]["Exit Reason"], "")

    def test_record_trade_journal_entry_skips_non_buy_opportunities(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 1_000,
                "price_change_percentage_1h_in_currency": -0.1,
                "price_change_percentage_24h_in_currency": -1.0,
                "price_change_percentage_7d_in_currency": 0.2,
                "rsi_14": 78.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Near Resistance",
                "multi_timeframe": {
                    "composite_trend": "Sideways",
                    "composite_score": 45,
                    "timeframes": {
                        "15m": {"trend": "Sideways", "change_percent": 0.0},
                        "1h": {"trend": "Bearish", "change_percent": -0.1},
                        "4h": {"trend": "Sideways", "change_percent": 0.1},
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")

            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )

        self.assertFalse(inserted)
        self.assertFalse(os.path.exists(journal_path))

    def test_update_open_paper_trades_closes_trade_at_take_profit(self):
        opening_data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]
        closing_data = [{"id": "bitcoin", "current_price": 56000}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            inserted = atlas_one.record_trade_journal_entry(
                opening_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )
            closed_count = atlas_one.update_open_paper_trades(
                closing_data,
                journal_path=journal_path,
                timestamp=datetime(2026, 7, 27, 16, 30, 0),
            )

            with open(journal_path, newline="", encoding="utf-8") as file_obj:
                rows = list(csv.DictReader(file_obj))

        self.assertTrue(inserted)
        self.assertEqual(closed_count, 1)
        self.assertEqual(rows[0]["Trade Status"], "Closed")
        self.assertEqual(rows[0]["ExitReason"] if "ExitReason" in rows[0] else rows[0]["Exit Reason"], "Take Profit 2")
        self.assertEqual(rows[0]["Exit Time"], "2026-07-27 16:30:00")
        self.assertEqual(rows[0]["Trade Duration"], "4h 30m")
        self.assertTrue(atlas_one._parse_trade_journal_currency(rows[0]["Profit/Loss (£)"]) > 0)
        self.assertTrue(atlas_one._parse_trade_journal_percent(rows[0]["Profit/Loss (%)"]) > 0)

    def test_process_paper_trades_returns_updated_performance_statistics(self):
        opening_data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 72,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]
        closing_data = [{"id": "bitcoin", "current_price": 56000}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            first_result = atlas_one.process_paper_trades(
                opening_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
            )
            second_result = atlas_one.process_paper_trades(
                closing_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 16, 30, 0),
            )

        self.assertTrue(first_result["opened_trade"])
        self.assertEqual(first_result["closed_trades"], 0)
        self.assertAlmostEqual(first_result["portfolio_snapshot"]["available_cash"], 9_000.0, places=2)
        self.assertAlmostEqual(first_result["portfolio_snapshot"]["invested_capital"], 1_000.0, places=2)
        self.assertEqual(second_result["closed_trades"], 1)
        self.assertEqual(second_result["performance_statistics"]["total_trades"], 1)
        self.assertEqual(second_result["performance_statistics"]["winning_trades"], 1)
        self.assertGreater(second_result["performance_statistics"]["cumulative_profit_loss"], 0.0)
        self.assertEqual(second_result["portfolio_snapshot"]["open_trade_count"], 0)
        self.assertAlmostEqual(second_result["portfolio_snapshot"]["current_portfolio_value"], 0.0, places=2)
        self.assertGreater(second_result["portfolio_snapshot"]["available_cash"], 10_000.0)
        self.assertGreater(second_result["portfolio_snapshot"]["realized_profit_loss"], 0.0)
        self.assertAlmostEqual(second_result["portfolio_snapshot"]["unrealized_profit_loss"], 0.0, places=2)

    def test_performance_statistics_individual_metrics(self):
        trade_rows = [
            {"Profit/Loss (£)": "£125.00", "Profit/Loss (%)": "5.00%"},
            {"Profit/Loss (£)": "-£50.00", "Profit/Loss (%)": "-2.00%"},
            {"Profit/Loss (£)": "£75.00", "Profit/Loss (%)": "3.00%"},
            {"Profit/Loss (£)": "£0.00", "Profit/Loss (%)": "0.00%"},
        ]

        self.assertEqual(atlas_one.calculate_total_trades(trade_rows), 4)
        self.assertEqual(atlas_one.calculate_winning_trades(trade_rows), 2)
        self.assertEqual(atlas_one.calculate_losing_trades(trade_rows), 1)
        self.assertAlmostEqual(atlas_one.calculate_win_rate(trade_rows), 66.6666666667, places=6)
        self.assertAlmostEqual(atlas_one.calculate_average_return(trade_rows), 1.5)
        self.assertAlmostEqual(atlas_one.calculate_best_trade(trade_rows), 125.0)
        self.assertAlmostEqual(atlas_one.calculate_worst_trade(trade_rows), -50.0)
        self.assertAlmostEqual(atlas_one.calculate_cumulative_profit_loss(trade_rows), 150.0)
        self.assertAlmostEqual(atlas_one.calculate_profit_factor(trade_rows), 4.0)

    def test_performance_statistics_aggregate_snapshot(self):
        trade_rows = [
            {"Profit/Loss (£)": "£120.00", "Profit/Loss (%)": "6.00%"},
            {"Profit/Loss (£)": "£80.00", "Profit/Loss (%)": "4.00%"},
            {"Profit/Loss (£)": "-£50.00", "Profit/Loss (%)": "-2.00%"},
        ]

        stats = atlas_one.calculate_performance_statistics(trade_rows)

        self.assertEqual(stats["total_trades"], 3)
        self.assertEqual(stats["winning_trades"], 2)
        self.assertEqual(stats["losing_trades"], 1)
        self.assertAlmostEqual(stats["win_rate"], 66.6666666667, places=6)
        self.assertAlmostEqual(stats["average_return"], 8 / 3, places=6)
        self.assertAlmostEqual(stats["best_trade"], 120.0)
        self.assertAlmostEqual(stats["worst_trade"], -50.0)
        self.assertAlmostEqual(stats["cumulative_profit_loss"], 150.0)
        self.assertAlmostEqual(stats["profit_factor"], 4.0)

    def test_performance_statistics_handles_zero_loss_profit_factor(self):
        trade_rows = [
            {"Profit/Loss (£)": "£30.00", "Profit/Loss (%)": "1.00%"},
            {"Profit/Loss (£)": "£20.00", "Profit/Loss (%)": "2.00%"},
        ]

        self.assertEqual(atlas_one.calculate_losing_trades(trade_rows), 0)
        self.assertEqual(atlas_one.calculate_profit_factor(trade_rows), float("inf"))

    def test_load_trade_journal_rows_returns_csv_rows(self):
        sample_row = {
            "Date/Time": "2026-08-05 10:30:00",
            "Coin": "Bitcoin",
            "Opportunity Score": "80",
            "Strategy Score": "72",
            "Suggested Action": "BUY",
            "Confidence": "85%",
            "Current Price": "£50,000.00",
            "Entry Price": "£50,000.00",
            "Entry Zone": "£48,500.00 - £49,500.00",
            "Stop Loss": "£47,000.00",
            "Take Profit 1": "£52,500.00",
            "Take Profit 2": "£55,000.00",
            "Risk/Reward Ratio": "1.50",
            "Position Size": "4.00",
            "Risk Level": "Medium",
            "Trend": "Bullish",
            "RSI": "58.0 (Neutral)",
            "Volume": "High",
            "Recommendation Rationale": "Sample rationale",
            "Trade Status": "Closed",
            "Exit Price": "£52,500.00",
            "Exit Time": "2026-08-05 14:30:00",
            "Exit Reason": "Take Profit 1",
            "Profit/Loss (£)": "£250.00",
            "Profit/Loss (%)": "5.00%",
            "Trade Duration": "4h",
            "Notes": "Sample note",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with open(journal_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=atlas_one.TRADE_JOURNAL_HEADERS)
                writer.writeheader()
                writer.writerow(sample_row)

            loaded_rows = atlas_one.load_trade_journal_rows(journal_path)

        self.assertEqual(len(loaded_rows), 1)
        self.assertEqual(loaded_rows[0]["Coin"], "Bitcoin")
        self.assertEqual(loaded_rows[0]["Profit/Loss (£)"], "£250.00")

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

    def test_detect_candlestick_pattern_bullish_engulfing(self):
        candles = [
            {"timestamp": 1, "open": 105.0, "high": 106.0, "low": 99.0, "close": 100.0},
            {"timestamp": 2, "open": 99.0, "high": 109.0, "low": 98.5, "close": 107.0},
        ]

        pattern, explanation = crypto_scanner.detect_candlestick_pattern(candles)

        self.assertEqual(pattern, "Bullish Engulfing")
        self.assertIn("upward momentum", explanation)

    def test_detect_candlestick_pattern_bearish_engulfing(self):
        candles = [
            {"timestamp": 1, "open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0},
            {"timestamp": 2, "open": 106.0, "high": 107.0, "low": 98.0, "close": 99.0},
        ]

        pattern, explanation = crypto_scanner.detect_candlestick_pattern(candles)

        self.assertEqual(pattern, "Bearish Engulfing")
        self.assertIn("downside pressure", explanation)

    def test_detect_candlestick_pattern_hammer(self):
        candles = [
            {"timestamp": 1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
            {"timestamp": 2, "open": 100.4, "high": 100.8, "low": 97.0, "close": 100.7},
        ]

        pattern, explanation = crypto_scanner.detect_candlestick_pattern(candles)

        self.assertEqual(pattern, "Hammer")
        self.assertIn("bullish reversal", explanation)

    def test_detect_candlestick_pattern_none_detected(self):
        candles = [
            {"timestamp": 1, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
            {"timestamp": 2, "open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0},
        ]

        pattern, explanation = crypto_scanner.detect_candlestick_pattern(candles)

        self.assertEqual(pattern, "None Detected")
        self.assertEqual(explanation, "")

    def test_strategy_scorecard_adds_bullish_engulfing_points(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "candlestick_pattern": "Bullish Engulfing",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 80,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        analysis = crypto_scanner.build_top_opportunity_analysis(data)

        self.assertIn("Candlestick Pattern: Bullish Engulfing", analysis)
        self.assertIn("Candlestick Pattern: +10 pts", analysis)
        self.assertIn("bullish engulfing pattern increased conviction", analysis)

    def test_strategy_scorecard_adds_hammer_points_only_near_support(self):
        near_support_data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Near Support",
                "candlestick_pattern": "Hammer",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 80,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]
        neutral_data = [dict(near_support_data[0], support_resistance_status="Between Levels")]

        near_support_analysis = crypto_scanner.build_top_opportunity_analysis(near_support_data)
        neutral_analysis = crypto_scanner.build_top_opportunity_analysis(neutral_data)

        self.assertIn("Candlestick Pattern: +8 pts", near_support_analysis)
        self.assertIn("hammer near support added a bullish boost", near_support_analysis)
        self.assertIn("Candlestick Pattern: +0 pts", neutral_analysis)
        self.assertIn("hammer was informational only", neutral_analysis)

    def test_strategy_scorecard_subtracts_bearish_engulfing_points(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 100_000_000,
                "total_volume": 30_000_000,
                "price_change_percentage_1h_in_currency": 0.8,
                "price_change_percentage_24h_in_currency": 5.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "rsi_14": 55.0,
                "support_level": 48000.0,
                "resistance_level": 52000.0,
                "support_resistance_status": "Between Levels",
                "candlestick_pattern": "Bearish Engulfing",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 80,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.8},
                        "1h": {"trend": "Bullish", "change_percent": 1.4},
                        "4h": {"trend": "Bullish", "change_percent": 3.2},
                    },
                },
            }
        ]

        analysis = crypto_scanner.build_top_opportunity_analysis(data)

        self.assertIn("Candlestick Pattern: Bearish Engulfing", analysis)
        self.assertIn("Candlestick Pattern: -10 pts", analysis)
        self.assertIn("bearish engulfing pattern reduced conviction", analysis)


if __name__ == "__main__":
    unittest.main()
