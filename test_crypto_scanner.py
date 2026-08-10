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


class AllowAllPaperTradeManager:
    def should_open_trade(self, opportunity):
        return True

    def calculate_position_size(self, opportunity, available_cash):
        return min(100.0, float(available_cash or 0.0))

    def calculate_trade_levels(self, opportunity):
        current_price = opportunity.get("current_price")
        if current_price is None:
            return None
        entry_price = float(current_price)
        return {
            "entry_price": entry_price,
            "stop_loss": entry_price * 0.97,
            "take_profit": entry_price * 1.06,
            "risk_reward_ratio": 2.0,
        }

    def update_open_position(self, position, current_price):
        return {"action": "HOLD", "status": "HOLD", "closed": False, "exit_price": None, "realised_pnl": 0.0}


class RecordingPaperTradeManager:
    def __init__(self):
        self.evaluated_coin_ids = []

    def should_open_trade(self, opportunity):
        self.evaluated_coin_ids.append(opportunity.get("coin_id"))
        return False


class AlwaysOpenPaperTradeManager:
    def should_open_trade(self, opportunity):
        return True

    def calculate_position_size(self, opportunity, available_cash):
        return min(100.0, float(available_cash or 0.0))

    def calculate_trade_levels(self, opportunity):
        current_price = opportunity.get("current_price")
        if current_price is None:
            return None
        entry_price = float(current_price)
        return {
            "entry_price": entry_price,
            "stop_loss": entry_price * 0.97,
            "take_profit": entry_price * 1.06,
            "risk_reward_ratio": 2.0,
        }

    def update_open_position(self, position, current_price):
        if current_price is None:
            return {"action": "HOLD", "status": "HOLD", "closed": False, "exit_price": None, "realised_pnl": 0.0}
        if float(current_price) <= float(position.get("stop_loss")):
            return {
                "action": "CLOSE",
                "status": "STOP_LOSS",
                "closed": True,
                "exit_price": float(position.get("stop_loss")),
                "realised_pnl": (float(position.get("stop_loss")) - float(position.get("entry_price"))) * float(position.get("position_size")),
            }
        if float(current_price) >= float(position.get("take_profit")):
            return {
                "action": "CLOSE",
                "status": "TAKE_PROFIT",
                "closed": True,
                "exit_price": float(position.get("take_profit")),
                "realised_pnl": (float(position.get("take_profit")) - float(position.get("entry_price"))) * float(position.get("position_size")),
            }
        return {"action": "HOLD", "status": "HOLD", "closed": False, "exit_price": None, "realised_pnl": 0.0}


class TrackingUpdatePaperTradeManager(AlwaysOpenPaperTradeManager):
    def __init__(self):
        self.update_calls = []

    def update_open_position(self, position, current_price):
        self.update_calls.append((position.get("coin_id"), current_price))
        return {"action": "HOLD", "status": "HOLD", "closed": False, "exit_price": None, "realised_pnl": 0.0}


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
        sentinel_paper_dashboard = "paper account dashboard"
        sentinel_analysis = "analysis"
        sentinel_trade_plan = "trade plan"
        sentinel_position_size = "position size"
        trade_result = {
            "paper_trade_engine_summary": {"trades_closed": []},
            "portfolio_snapshot": {"available_cash": 10_000.0},
        }
        mock_live = unittest.mock.MagicMock()
        mock_live.__enter__.return_value = mock_live
        mock_live.__exit__.return_value = False

        with patch(
            "atlas_one.argparse.ArgumentParser.parse_args",
            return_value=crypto_scanner.argparse.Namespace(refresh_interval=0, iterations=3),
        ), patch("atlas_one.Console", return_value=mock_console), patch(
            "atlas_one.fetch_market_data",
            side_effect=[initial_market_data, refreshed_market_data, refreshed_market_data],
        ) as mock_fetch_market_data, patch(
            "atlas_one.enrich_market_data_with_indicators",
            side_effect=[initial_enriched, final_enriched, final_enriched],
        ) as mock_enrich, patch(
            "atlas_one.build_portfolio_dashboard",
            return_value=sentinel_dashboard,
        ), patch(
            "atlas_one.build_top_opportunity_analysis",
            return_value=sentinel_analysis,
        ), patch(
            "atlas_one.process_paper_trades",
            side_effect=[trade_result, trade_result, trade_result],
        ) as mock_process_paper_trades, patch(
            "atlas_one.build_trade_plan",
            return_value=sentinel_trade_plan,
        ), patch(
            "atlas_one.build_position_size_calculator",
            return_value=sentinel_position_size,
        ), patch(
            "atlas_one.PaperTradeStatistics.calculate",
            return_value={"current_account_balance": 10_000.0},
        ) as mock_statistics_calculate, patch(
            "atlas_one.PaperTradeDashboard.render",
            return_value=sentinel_paper_dashboard,
        ), patch("atlas_one.Live", return_value=mock_live), patch("atlas_one.build_table", return_value=sentinel_table) as mock_build_table, patch(
            "atlas_one.log_request_audit"
        ), patch("atlas_one.time.sleep"):
            atlas_one.main()

        self.assertEqual(mock_fetch_market_data.call_count, 3)
        self.assertEqual(mock_enrich.call_count, 3)
        self.assertEqual(mock_process_paper_trades.call_count, 3)
        self.assertEqual(mock_statistics_calculate.call_count, 3)
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
        self.assertEqual(printed_values.count(sentinel_paper_dashboard), 0)
        self.assertEqual(mock_live.update.call_count, 2)
        self.assertEqual(
            [call_args.args[0] for call_args in mock_live.update.call_args_list],
            [sentinel_paper_dashboard, sentinel_paper_dashboard],
        )

    def test_main_uses_shared_engine_for_paper_trade_statistics(self):
        market_data = [{"id": "bitcoin", "current_price": 50000, "rsi_14": 55.0}]
        trade_results = [
            {
                "paper_trade_engine_summary": {
                    "trades_closed": [{"coin_id": "bitcoin", "realised_pnl": 25.0}],
                },
                "portfolio_snapshot": {"available_cash": 9_900.0},
            },
            {
                "paper_trade_engine_summary": {
                    "trades_closed": [{"coin_id": "ethereum", "realised_pnl": -10.0}],
                },
                "portfolio_snapshot": {"available_cash": 9_850.0},
            },
        ]
        mock_live = unittest.mock.MagicMock()
        mock_live.__enter__.return_value = mock_live
        mock_live.__exit__.return_value = False

        with patch(
            "atlas_one.argparse.ArgumentParser.parse_args",
            return_value=crypto_scanner.argparse.Namespace(refresh_interval=0, iterations=2),
        ), patch("atlas_one.Console", return_value=unittest.mock.Mock()), patch(
            "atlas_one.fetch_market_data",
            side_effect=[market_data, market_data],
        ), patch(
            "atlas_one.enrich_market_data_with_indicators",
            side_effect=[market_data, market_data],
        ), patch(
            "atlas_one.build_portfolio_dashboard",
            return_value="portfolio",
        ), patch(
            "atlas_one.build_top_opportunity_analysis",
            return_value="analysis",
        ), patch(
            "atlas_one.build_trade_plan",
            return_value="trade plan",
        ), patch(
            "atlas_one.build_position_size_calculator",
            return_value="position size",
        ), patch(
            "atlas_one.build_table",
            return_value=object(),
        ), patch(
            "atlas_one.process_paper_trades",
            side_effect=trade_results,
        ), patch(
            "atlas_one.PaperTradeDashboard.render",
            return_value="paper dashboard",
        ), patch("atlas_one.Live", return_value=mock_live), patch(
            "atlas_one.log_request_audit"
        ), patch("atlas_one.time.sleep"), patch(
            "atlas_one.PaperTradeStatistics.calculate",
            return_value={"current_account_balance": 10_000.0},
        ) as mock_statistics_calculate:
            atlas_one.main()

        self.assertEqual(mock_statistics_calculate.call_count, 2)
        first_engine = mock_statistics_calculate.call_args_list[0].kwargs["paper_trade_engine"]
        second_engine = mock_statistics_calculate.call_args_list[1].kwargs["paper_trade_engine"]
        self.assertIs(first_engine, second_engine)

    def test_main_accumulates_closed_trades_for_statistics_dashboard(self):
        market_data = [{"id": "bitcoin", "current_price": 50000, "rsi_14": 55.0}]
        trade_results = [
            {
                "paper_trade_engine_summary": {
                    "trades_closed": [{"coin_id": "bitcoin", "realised_pnl": 30.0}],
                },
                "portfolio_snapshot": {"available_cash": 9_900.0},
            },
            {
                "paper_trade_engine_summary": {
                    "trades_closed": [{"coin_id": "ethereum", "realised_pnl": -15.0}],
                },
                "portfolio_snapshot": {"available_cash": 9_850.0},
            },
        ]
        mock_live = unittest.mock.MagicMock()
        mock_live.__enter__.return_value = mock_live
        mock_live.__exit__.return_value = False

        with patch(
            "atlas_one.argparse.ArgumentParser.parse_args",
            return_value=crypto_scanner.argparse.Namespace(refresh_interval=0, iterations=2),
        ), patch("atlas_one.Console", return_value=unittest.mock.Mock()), patch(
            "atlas_one.fetch_market_data",
            side_effect=[market_data, market_data],
        ), patch(
            "atlas_one.enrich_market_data_with_indicators",
            side_effect=[market_data, market_data],
        ), patch(
            "atlas_one.build_portfolio_dashboard",
            return_value="portfolio",
        ), patch(
            "atlas_one.build_top_opportunity_analysis",
            return_value="analysis",
        ), patch(
            "atlas_one.build_trade_plan",
            return_value="trade plan",
        ), patch(
            "atlas_one.build_position_size_calculator",
            return_value="position size",
        ), patch(
            "atlas_one.build_table",
            return_value=object(),
        ), patch(
            "atlas_one.process_paper_trades",
            side_effect=trade_results,
        ), patch(
            "atlas_one.PaperTradeDashboard.render",
            return_value="paper dashboard",
        ), patch("atlas_one.Live", return_value=mock_live), patch(
            "atlas_one.log_request_audit"
        ), patch("atlas_one.time.sleep"), patch(
            "atlas_one.PaperTradeStatistics.calculate",
            return_value={"current_account_balance": 10_000.0},
        ) as mock_statistics_calculate:
            atlas_one.main()

        first_closed_trades = mock_statistics_calculate.call_args_list[0].kwargs["closed_trades"]
        second_closed_trades = mock_statistics_calculate.call_args_list[1].kwargs["closed_trades"]
        self.assertEqual(len(first_closed_trades), 1)
        self.assertEqual(len(second_closed_trades), 2)
        self.assertEqual(second_closed_trades[0]["coin_id"], "bitcoin")
        self.assertEqual(second_closed_trades[1]["coin_id"], "ethereum")

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

    def test_paper_trade_manager_returns_false_for_score_69(self):
        manager = atlas_one.PaperTradeManager()
        self.assertFalse(manager.should_open_trade({"coin_id": "bitcoin", "score": 69}))

    def test_paper_trade_manager_returns_true_for_score_70(self):
        manager = atlas_one.PaperTradeManager()
        self.assertTrue(manager.should_open_trade({"coin_id": "bitcoin", "score": 70}))

    def test_paper_trade_manager_returns_true_for_score_95(self):
        manager = atlas_one.PaperTradeManager()
        self.assertTrue(manager.should_open_trade({"coin_id": "bitcoin", "score": 95}))

    def test_paper_trade_manager_position_size_score_69_returns_zero(self):
        manager = atlas_one.PaperTradeManager()
        self.assertEqual(manager.calculate_position_size({"coin_id": "bitcoin", "score": 69}, 1000.0), 0.0)

    def test_paper_trade_manager_position_size_score_70_returns_two_percent(self):
        manager = atlas_one.PaperTradeManager()
        self.assertAlmostEqual(
            manager.calculate_position_size({"coin_id": "bitcoin", "score": 70}, 1000.0),
            20.0,
        )

    def test_paper_trade_manager_position_size_score_80_returns_three_percent(self):
        manager = atlas_one.PaperTradeManager()
        self.assertAlmostEqual(
            manager.calculate_position_size({"coin_id": "bitcoin", "score": 80}, 1000.0),
            30.0,
        )

    def test_paper_trade_manager_position_size_score_90_returns_five_percent(self):
        manager = atlas_one.PaperTradeManager()
        self.assertAlmostEqual(
            manager.calculate_position_size({"coin_id": "bitcoin", "score": 90}, 1000.0),
            50.0,
        )

    def test_paper_trade_manager_position_size_invalid_score_returns_zero(self):
        manager = atlas_one.PaperTradeManager()
        self.assertEqual(manager.calculate_position_size({"coin_id": "bitcoin", "score": "invalid"}, 1000.0), 0.0)

    def test_paper_trade_manager_trade_levels_valid_opportunity(self):
        manager = atlas_one.PaperTradeManager()
        levels = manager.calculate_trade_levels({"coin_id": "bitcoin", "current_price": 100.0})

        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels["entry_price"], 100.0)

    def test_paper_trade_manager_trade_levels_stop_loss_is_three_percent_below(self):
        manager = atlas_one.PaperTradeManager()
        levels = manager.calculate_trade_levels({"coin_id": "bitcoin", "current_price": 100.0})

        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels["stop_loss"], 97.0)

    def test_paper_trade_manager_trade_levels_take_profit_is_six_percent_above(self):
        manager = atlas_one.PaperTradeManager()
        levels = manager.calculate_trade_levels({"coin_id": "bitcoin", "current_price": 100.0})

        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels["take_profit"], 106.0)

    def test_paper_trade_manager_trade_levels_has_two_to_one_risk_reward_ratio(self):
        manager = atlas_one.PaperTradeManager()
        levels = manager.calculate_trade_levels({"coin_id": "bitcoin", "current_price": 100.0})

        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels["risk_reward_ratio"], 2.0)

    def test_paper_trade_manager_trade_levels_returns_none_for_missing_or_invalid_price(self):
        manager = atlas_one.PaperTradeManager()

        self.assertIsNone(manager.calculate_trade_levels({"coin_id": "bitcoin"}))
        self.assertIsNone(manager.calculate_trade_levels({"coin_id": "bitcoin", "current_price": "invalid"}))

    def test_paper_trade_manager_update_open_position_closes_on_stop_loss(self):
        manager = atlas_one.PaperTradeManager()
        position = {
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "position_size": 2.0,
        }

        result = manager.update_open_position(position, current_price=96.0)

        self.assertEqual(result["status"], "STOP_LOSS")
        self.assertTrue(result["closed"])
        self.assertEqual(result["action"], "CLOSE")
        self.assertAlmostEqual(result["realised_pnl"], -6.0)

    def test_paper_trade_manager_update_open_position_closes_on_take_profit(self):
        manager = atlas_one.PaperTradeManager()
        position = {
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "position_size": 2.0,
        }

        result = manager.update_open_position(position, current_price=107.0)

        self.assertEqual(result["status"], "TAKE_PROFIT")
        self.assertTrue(result["closed"])
        self.assertEqual(result["action"], "CLOSE")
        self.assertAlmostEqual(result["realised_pnl"], 12.0)

    def test_paper_trade_manager_update_open_position_holds_between_levels(self):
        manager = atlas_one.PaperTradeManager()
        position = {
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "position_size": 2.0,
        }

        result = manager.update_open_position(position, current_price=101.0)

        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["closed"])
        self.assertEqual(result["action"], "HOLD")

    def test_paper_trade_manager_update_open_position_holds_for_invalid_price(self):
        manager = atlas_one.PaperTradeManager()
        position = {
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "position_size": 2.0,
        }

        result = manager.update_open_position(position, current_price="invalid")

        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["closed"])
        self.assertEqual(result["action"], "HOLD")

    def test_paper_trade_manager_update_open_position_holds_for_missing_price(self):
        manager = atlas_one.PaperTradeManager()
        position = {
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "position_size": 2.0,
        }

        result = manager.update_open_position(position, current_price=None)

        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["closed"])
        self.assertEqual(result["action"], "HOLD")

    def test_paper_trade_engine_opens_trade_for_eligible_opportunity(self):
        engine = atlas_one.PaperTradeEngine()
        opportunities = [{"coin_id": "bitcoin", "score": 80, "current_price": 100.0}]

        summary = engine.process_latest_opportunities(opportunities, available_cash=10_000.0)

        self.assertEqual(len(summary["new_trades_opened"]), 1)
        self.assertEqual(len(summary["trades_closed"]), 0)
        self.assertEqual(len(summary["trades_still_open"]), 1)
        self.assertEqual(summary["trades_still_open"][0]["coin_id"], "bitcoin")

    def test_paper_trade_engine_ignores_opportunities_failing_entry_rules(self):
        engine = atlas_one.PaperTradeEngine()
        opportunities = [{"coin_id": "bitcoin", "score": 69, "current_price": 100.0}]

        summary = engine.process_latest_opportunities(opportunities, available_cash=10_000.0)

        self.assertEqual(len(summary["new_trades_opened"]), 0)
        self.assertEqual(len(summary["trades_closed"]), 0)
        self.assertEqual(len(summary["trades_still_open"]), 0)

    def test_paper_trade_engine_ignores_already_open_positions(self):
        engine = atlas_one.PaperTradeEngine()
        opportunities = [{"coin_id": "bitcoin", "score": 80, "current_price": 100.0}]

        first_summary = engine.process_latest_opportunities(opportunities, available_cash=10_000.0)
        second_summary = engine.process_latest_opportunities(opportunities, available_cash=10_000.0)

        self.assertEqual(len(first_summary["new_trades_opened"]), 1)
        self.assertEqual(len(second_summary["new_trades_opened"]), 0)
        self.assertEqual(len(second_summary["trades_still_open"]), 1)

    def test_paper_trade_engine_closes_positions_and_removes_from_active_list(self):
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=AlwaysOpenPaperTradeManager())
        opening_snapshot = [{"coin_id": "bitcoin", "score": 80, "current_price": 100.0}]
        closing_snapshot = [{"coin_id": "bitcoin", "score": 80, "current_price": 106.0}]

        first_summary = engine.process_latest_opportunities(opening_snapshot, available_cash=10_000.0)
        second_summary = engine.process_latest_opportunities(closing_snapshot, available_cash=10_000.0)

        self.assertEqual(len(first_summary["new_trades_opened"]), 1)
        self.assertEqual(len(second_summary["trades_closed"]), 1)
        self.assertEqual(second_summary["trades_closed"][0]["status"], "TAKE_PROFIT")
        self.assertEqual(len(second_summary["trades_still_open"]), 0)

    def test_paper_trade_engine_summary_contains_expected_keys(self):
        engine = atlas_one.PaperTradeEngine()
        opportunities = [{"coin_id": "bitcoin", "score": 80, "current_price": 100.0}]

        summary = engine.process_latest_opportunities(opportunities, available_cash=10_000.0)

        self.assertIn("new_trades_opened", summary)
        self.assertIn("trades_closed", summary)
        self.assertIn("trades_still_open", summary)

    def test_paper_trade_statistics_empty_history(self):
        engine = atlas_one.PaperTradeEngine()
        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=[],
            starting_balance=10_000.0,
        )

        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats["open_trades"], 0)
        self.assertEqual(stats["closed_trades"], 0)
        self.assertEqual(stats["winning_trades"], 0)
        self.assertEqual(stats["losing_trades"], 0)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertEqual(stats["total_realised_pnl"], 0.0)
        self.assertEqual(stats["average_realised_pnl"], 0.0)
        self.assertEqual(stats["best_trade"], 0.0)
        self.assertEqual(stats["worst_trade"], 0.0)
        self.assertEqual(stats["current_account_balance"], 10_000.0)

    def test_paper_trade_statistics_all_winning_trades(self):
        engine = atlas_one.PaperTradeEngine()
        closed_trades = [
            {"realised_pnl": 50.0},
            {"realised_pnl": 25.0},
            {"realised_pnl": 75.0},
        ]

        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=closed_trades,
            starting_balance=10_000.0,
        )

        self.assertEqual(stats["total_trades"], 3)
        self.assertEqual(stats["open_trades"], 0)
        self.assertEqual(stats["closed_trades"], 3)
        self.assertEqual(stats["winning_trades"], 3)
        self.assertEqual(stats["losing_trades"], 0)
        self.assertEqual(stats["win_rate"], 100.0)
        self.assertEqual(stats["total_realised_pnl"], 150.0)
        self.assertEqual(stats["average_realised_pnl"], 50.0)
        self.assertEqual(stats["best_trade"], 75.0)
        self.assertEqual(stats["worst_trade"], 25.0)

    def test_paper_trade_statistics_all_losing_trades(self):
        engine = atlas_one.PaperTradeEngine()
        closed_trades = [
            {"realised_pnl": -50.0},
            {"realised_pnl": -25.0},
            {"realised_pnl": -75.0},
        ]

        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=closed_trades,
            starting_balance=10_000.0,
        )

        self.assertEqual(stats["total_trades"], 3)
        self.assertEqual(stats["open_trades"], 0)
        self.assertEqual(stats["closed_trades"], 3)
        self.assertEqual(stats["winning_trades"], 0)
        self.assertEqual(stats["losing_trades"], 3)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertEqual(stats["total_realised_pnl"], -150.0)
        self.assertEqual(stats["average_realised_pnl"], -50.0)
        self.assertEqual(stats["best_trade"], -25.0)
        self.assertEqual(stats["worst_trade"], -75.0)

    def test_paper_trade_statistics_mixed_results(self):
        engine = atlas_one.PaperTradeEngine()
        closed_trades = [
            {"realised_pnl": 120.0},
            {"realised_pnl": -40.0},
            {"realised_pnl": 20.0},
            {"realised_pnl": -10.0},
        ]

        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=closed_trades,
            starting_balance=10_000.0,
        )

        self.assertEqual(stats["total_trades"], 4)
        self.assertEqual(stats["winning_trades"], 2)
        self.assertEqual(stats["losing_trades"], 2)
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertEqual(stats["total_realised_pnl"], 90.0)
        self.assertAlmostEqual(stats["average_realised_pnl"], 22.5)
        self.assertEqual(stats["best_trade"], 120.0)
        self.assertEqual(stats["worst_trade"], -40.0)

    def test_paper_trade_statistics_account_balance_calculation(self):
        engine = atlas_one.PaperTradeEngine()
        engine.open_positions = [
            {"coin_id": "bitcoin", "allocated_cash": 300.0},
            {"coin_id": "ethereum", "allocated_cash": 200.0},
        ]
        closed_trades = [
            {"realised_pnl": 150.0},
            {"realised_pnl": -50.0},
        ]

        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=closed_trades,
            starting_balance=10_000.0,
        )

        self.assertEqual(stats["open_trades"], 2)
        self.assertEqual(stats["closed_trades"], 2)
        self.assertEqual(stats["total_trades"], 4)
        self.assertEqual(stats["current_account_balance"], 9_600.0)

    def test_paper_trade_dashboard_render_includes_required_fields(self):
        statistics = {
            "total_trades": 5,
            "open_trades": 2,
            "closed_trades": 3,
            "winning_trades": 2,
            "losing_trades": 1,
            "win_rate": 66.67,
            "total_realised_pnl": 120.5,
            "average_realised_pnl": 40.166,
            "best_trade": 100.0,
            "worst_trade": -25.0,
            "current_account_balance": 10_120.5,
            "cash_available": 9_400.0,
        }

        rendered = atlas_one.PaperTradeDashboard(statistics).render()

        self.assertIn("========================================", rendered)
        self.assertIn("ATLAS ONE PAPER ACCOUNT", rendered)
        self.assertIn("Current Balance: £10,120.50", rendered)
        self.assertIn("Cash Available: £9,400.00", rendered)
        self.assertIn("Open Trades: 2", rendered)
        self.assertIn("Closed Trades: 3", rendered)
        self.assertIn("Winning Trades: 2", rendered)
        self.assertIn("Losing Trades: 1", rendered)
        self.assertIn("Win Rate: 66.67%", rendered)
        self.assertIn("Total Realised PnL: £120.50", rendered)
        self.assertIn("Average Realised PnL: £40.17", rendered)
        self.assertIn("Best Trade: £100.00", rendered)
        self.assertIn("Worst Trade: £-25.00", rendered)

    def test_paper_trade_dashboard_uses_statistics_from_paper_trade_statistics(self):
        engine = atlas_one.PaperTradeEngine()
        stats = atlas_one.PaperTradeStatistics().calculate(
            paper_trade_engine=engine,
            closed_trades=[],
            starting_balance=10_000.0,
        )

        rendered = atlas_one.PaperTradeDashboard(stats).render()

        self.assertIn("Current Balance: £10,000.00", rendered)
        self.assertIn("Cash Available: £10,000.00", rendered)

    def test_paper_trade_dashboard_render_is_read_only(self):
        statistics = {
            "open_trades": 1,
            "closed_trades": 2,
            "winning_trades": 1,
            "losing_trades": 1,
            "win_rate": 50.0,
            "total_realised_pnl": 10.0,
            "average_realised_pnl": 5.0,
            "best_trade": 20.0,
            "worst_trade": -10.0,
            "current_account_balance": 9_900.0,
        }
        expected = dict(statistics)

        atlas_one.PaperTradeDashboard(statistics).render()

        self.assertEqual(statistics, expected)

    def test_process_paper_trades_engine_summary_no_opportunities(self):
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=AlwaysOpenPaperTradeManager())

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            result = atlas_one.process_paper_trades(
                [],
                journal_path=journal_path,
                seen_entries=set(),
                paper_trade_engine=engine,
            )

        summary = result["paper_trade_engine_summary"]
        self.assertEqual(summary["new_trades_opened"], [])
        self.assertEqual(summary["trades_closed"], [])
        self.assertEqual(summary["trades_still_open"], [])

    def test_process_paper_trades_engine_summary_one_qualifying_opportunity(self):
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=AlwaysOpenPaperTradeManager())
        data = [{"id": "bitcoin", "current_price": 50000}]
        ranked = [{"coin_id": "bitcoin", "score": 80, "current_price": 50000.0, "suggested_action": "BUY"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with patch("atlas_one._build_ranked_opportunities_for_trade_decision", return_value=ranked), patch(
                "atlas_one.record_trade_journal_entry", return_value=False
            ):
                result = atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )

        summary = result["paper_trade_engine_summary"]
        self.assertEqual(len(summary["new_trades_opened"]), 1)
        self.assertEqual(len(summary["trades_still_open"]), 1)

    def test_process_paper_trades_engine_summary_duplicate_opportunity_ignored(self):
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=AlwaysOpenPaperTradeManager())
        data = [{"id": "bitcoin", "current_price": 50000}]
        ranked = [{"coin_id": "bitcoin", "score": 80, "current_price": 50000.0, "suggested_action": "BUY"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with patch("atlas_one._build_ranked_opportunities_for_trade_decision", return_value=ranked), patch(
                "atlas_one.record_trade_journal_entry", return_value=False
            ):
                first = atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )
                second = atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )

        self.assertEqual(len(first["paper_trade_engine_summary"]["new_trades_opened"]), 1)
        self.assertEqual(len(second["paper_trade_engine_summary"]["new_trades_opened"]), 0)
        self.assertEqual(len(second["paper_trade_engine_summary"]["trades_still_open"]), 1)

    def test_process_paper_trades_engine_summary_existing_open_position_updated(self):
        tracking_manager = TrackingUpdatePaperTradeManager()
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=tracking_manager)
        data = [{"id": "bitcoin", "current_price": 50000}]
        ranked = [{"coin_id": "bitcoin", "score": 80, "current_price": 50000.0, "suggested_action": "BUY"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with patch("atlas_one._build_ranked_opportunities_for_trade_decision", return_value=ranked), patch(
                "atlas_one.record_trade_journal_entry", return_value=False
            ):
                atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )
                atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )

        self.assertGreaterEqual(len(tracking_manager.update_calls), 1)

    def test_process_paper_trades_engine_summary_does_not_open_for_non_buy_signal(self):
        engine = atlas_one.PaperTradeEngine(paper_trade_manager=AlwaysOpenPaperTradeManager())
        data = [{"id": "bitcoin", "current_price": 50000}]
        ranked = [{"coin_id": "bitcoin", "score": 95, "current_price": 50000.0, "suggested_action": "WATCH"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            with patch("atlas_one._build_ranked_opportunities_for_trade_decision", return_value=ranked), patch(
                "atlas_one.record_trade_journal_entry", return_value=False
            ):
                result = atlas_one.process_paper_trades(
                    data,
                    journal_path=journal_path,
                    seen_entries=set(),
                    paper_trade_engine=engine,
                )

        summary = result["paper_trade_engine_summary"]
        self.assertEqual(summary["new_trades_opened"], [])
        self.assertEqual(summary["trades_still_open"], [])

    def test_record_trade_journal_entry_evaluates_all_ranked_opportunities(self):
        data = [
            {
                "id": "bitcoin",
                "current_price": 50000,
                "market_cap": 1_000_000_000,
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
            },
            {
                "id": "ethereum",
                "current_price": 3000,
                "market_cap": 1_000_000_000,
                "total_volume": 20_000_000,
                "price_change_percentage_1h_in_currency": 0.2,
                "price_change_percentage_24h_in_currency": 2.0,
                "price_change_percentage_7d_in_currency": 3.0,
                "rsi_14": 50.0,
                "support_level": 2900.0,
                "resistance_level": 3200.0,
                "support_resistance_status": "Between Levels",
                "multi_timeframe": {
                    "composite_trend": "Bullish",
                    "composite_score": 65,
                    "timeframes": {
                        "15m": {"trend": "Bullish", "change_percent": 0.4},
                        "1h": {"trend": "Bullish", "change_percent": 0.8},
                        "4h": {"trend": "Bullish", "change_percent": 1.1},
                    },
                },
            },
        ]

        manager = RecordingPaperTradeManager()
        expected_coin_order = [coin_id for _, coin_id, _ in atlas_one.rank_opportunity(data)]

        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "trade_journal.csv")
            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
                paper_trade_manager=manager,
            )

        self.assertFalse(inserted)
        self.assertEqual(manager.evaluated_coin_ids, expected_coin_order)

    def test_trade_journal_creates_parent_directories_for_nested_paths(self):
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
            journal_dir = os.path.join(temp_dir, "nested", "journals")
            journal_path = os.path.join(journal_dir, "trade_journal.csv")
            inserted = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 0, 0),
                paper_trade_manager=AllowAllPaperTradeManager(),
            )

            self.assertTrue(inserted)
            self.assertTrue(os.path.exists(journal_path))
            self.assertTrue(os.path.isdir(journal_dir))

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
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
            )
            second_insert = atlas_one.record_trade_journal_entry(
                data,
                journal_path=journal_path,
                seen_entries=seen_entries,
                timestamp=datetime(2026, 7, 27, 12, 0, 1),
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
            )
            second_insert = atlas_one.record_trade_journal_entry(
                ethereum_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 5, 0),
                starting_balance=10_000.0,
                position_size_pct=0.25,
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
            )
            second_insert = atlas_one.record_trade_journal_entry(
                ethereum_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 12, 5, 0),
                starting_balance=1_000.0,
                position_size_pct=1.0,
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
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
                paper_trade_manager=AllowAllPaperTradeManager(),
            )
            second_result = atlas_one.process_paper_trades(
                closing_data,
                journal_path=journal_path,
                seen_entries=set(),
                timestamp=datetime(2026, 7, 27, 16, 30, 0),
                paper_trade_manager=AllowAllPaperTradeManager(),
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
