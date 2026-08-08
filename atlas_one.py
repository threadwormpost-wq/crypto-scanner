#!/usr/bin/env python3
"""Live cryptocurrency scanner powered by the free CoinGecko API."""
print("=" * 60)
print("ATLAS ONE")
print("AI Crypto Opportunity Scanner")
print("Version: 0.1 Alpha")
print("Finding opportunities. Explaining decisions.")
print("=" * 60)
print()
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

# The coins we want to track in the terminal dashboard.
COINS: Tuple[Tuple[str, str], ...] = (
    ("Bitcoin", "bitcoin"),
    ("Ethereum", "ethereum"),
    ("XRP", "ripple"),
    ("Solana", "solana"),
    ("BNB", "binancecoin"),
    ("Cardano", "cardano"),
    ("Dogecoin", "dogecoin"),
    ("Avalanche", "avalanche-2"),
    ("Chainlink", "chainlink"),
    ("Polkadot", "polkadot"),
)

API_URL = "https://api.coingecko.com/api/v3/coins/markets"
HISTORY_API_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
OHLC_API_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
USD_TO_GBP_RATE_URL = "https://api.frankfurter.app/latest?from=USD&to=GBP"
DEFAULT_USD_TO_GBP_RATE = 0.79
DEBUG = False
TRADE_JOURNAL_FILE = "trade_journal.csv"
TRADE_JOURNAL_HEADERS = [
    "Date/Time",
    "Coin",
    "Opportunity Score",
    "Strategy Score",
    "Suggested Action",
    "Confidence",
    "Current Price",
    "Entry Price",
    "Entry Zone",
    "Stop Loss",
    "Take Profit 1",
    "Take Profit 2",
    "Risk/Reward Ratio",
    "Position Size",
    "Risk Level",
    "Trend",
    "RSI",
    "Volume",
    "Recommendation Rationale",
    "Trade Status",
    "Exit Price",
    "Exit Time",
    "Exit Reason",
    "Profit/Loss (£)",
    "Profit/Loss (%)",
    "Trade Duration",
    "Notes",
]

TRADE_STATUS_DEFAULT = "Pending"
TRADE_STATUS_OPEN = "Open"
TRADE_STATUS_CLOSED = "Closed"
EXIT_REASON_DEFAULT = "Not Triggered"
EXIT_REASON_STOP_LOSS = "Stop Loss"
EXIT_REASON_TAKE_PROFIT_1 = "Take Profit 1"
EXIT_REASON_TAKE_PROFIT_2 = "Take Profit 2"
DEFAULT_PAPER_STARTING_BALANCE = 10_000.0
DEFAULT_PAPER_POSITION_SIZE_PCT = 0.10

# Rate limiting and caching configuration
MIN_REQUEST_INTERVAL = 0.5  # Minimum seconds between API requests to respect rate limits
MARKET_DATA_TTL = 60  # Cache market data for 60 seconds (2 iterations at 30s interval)
HISTORICAL_PRICE_TTL = 300  # Cache historical prices for 5 minutes (more stable data)
INTRADAY_PRICE_TTL = 60  # Cache intraday prices for 1 minute
OHLC_PRICE_TTL = 300  # Cache OHLC candles for 5 minutes
OHLC_PERSISTENT_CACHE_FILE = ".atlas_one_ohlc_cache.json"
TOP_HISTORY_FETCH = 3  # Number of top-ranked coins eligible for deferred market_chart history fetches.

logger = logging.getLogger(__name__)


class PaperTradeManager:
    """Manage paper-trade entry decisions for ranked opportunities."""

    def should_open_trade(self, opportunity: dict) -> bool:
        """Return whether a paper trade should be opened for the opportunity."""
        score = opportunity.get("score") if isinstance(opportunity, dict) else None
        if score is None:
            return False
        try:
            return float(score) >= 70.0
        except (TypeError, ValueError):
            return False

    def calculate_position_size(self, opportunity: dict, available_cash: float) -> float:
        """Return the cash amount to allocate from available cash based on score tiers."""
        score = opportunity.get("score") if isinstance(opportunity, dict) else None
        if score is None:
            return 0.0

        try:
            normalized_score = float(score)
            normalized_available_cash = max(0.0, float(available_cash))
        except (TypeError, ValueError):
            return 0.0

        if normalized_score >= 90.0:
            allocation_pct = 0.05
        elif normalized_score >= 80.0:
            allocation_pct = 0.03
        elif normalized_score >= 70.0:
            allocation_pct = 0.02
        else:
            return 0.0

        allocation = normalized_available_cash * allocation_pct
        return min(allocation, normalized_available_cash)

    def calculate_trade_levels(self, opportunity: dict) -> dict | None:
        """Return paper-trade entry, stop, target and risk/reward for a valid opportunity price."""
        if not isinstance(opportunity, dict):
            return None

        price = opportunity.get("current_price")
        try:
            entry_price = float(price)
        except (TypeError, ValueError):
            return None

        if entry_price <= 0:
            return None

        stop_loss = entry_price * 0.97
        take_profit = entry_price * 1.06
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
        risk_reward_ratio = reward / risk if risk > 0 else 0.0

        return {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward_ratio": risk_reward_ratio,
        }

    def update_open_position(self, position: dict, current_price: float) -> dict:
        """Update an open paper position and return HOLD or a closed result."""
        hold_response = {
            "action": "HOLD",
            "status": "HOLD",
            "closed": False,
            "exit_price": None,
            "realised_pnl": 0.0,
        }

        if not isinstance(position, dict):
            return hold_response

        try:
            normalized_current_price = float(current_price)
            entry_price = float(position.get("entry_price"))
            stop_loss = float(position.get("stop_loss"))
            take_profit = float(position.get("take_profit"))
            position_size = float(position.get("position_size", 1.0))
        except (TypeError, ValueError):
            return hold_response

        if (
            normalized_current_price <= 0
            or entry_price <= 0
            or stop_loss <= 0
            or take_profit <= 0
            or position_size <= 0
        ):
            return hold_response

        if normalized_current_price <= stop_loss:
            exit_price = stop_loss
            return {
                "action": "CLOSE",
                "status": "STOP_LOSS",
                "closed": True,
                "exit_price": exit_price,
                "realised_pnl": (exit_price - entry_price) * position_size,
            }

        if normalized_current_price >= take_profit:
            exit_price = take_profit
            return {
                "action": "CLOSE",
                "status": "TAKE_PROFIT",
                "closed": True,
                "exit_price": exit_price,
                "realised_pnl": (exit_price - entry_price) * position_size,
            }

        return hold_response


class PaperTradeEngine:
    """Coordinate paper-trade lifecycle decisions using PaperTradeManager."""

    def __init__(self, paper_trade_manager: PaperTradeManager | None = None):
        self.paper_trade_manager = paper_trade_manager or PaperTradeManager()
        self.open_positions: List[dict] = []

    @staticmethod
    def _get_opportunity_coin_id(opportunity: dict) -> str | None:
        """Return a stable coin identifier from ranked opportunity payloads."""
        coin_id = opportunity.get("coin_id") or opportunity.get("id")
        if coin_id is None:
            return None
        return str(coin_id)

    def process_latest_opportunities(self, opportunities: List[dict], available_cash: float) -> dict:
        """Open and update paper positions from the latest ranked opportunities."""
        valid_opportunities = [item for item in opportunities if isinstance(item, dict)]

        try:
            remaining_cash = max(0.0, float(available_cash))
        except (TypeError, ValueError):
            remaining_cash = 0.0

        open_coin_ids = {
            str(position.get("coin_id"))
            for position in self.open_positions
            if position.get("coin_id") is not None
        }

        new_trades_opened: List[dict] = []
        for opportunity in valid_opportunities:
            coin_id = self._get_opportunity_coin_id(opportunity)
            if coin_id is None or coin_id in open_coin_ids:
                continue

            if not self.paper_trade_manager.should_open_trade(opportunity):
                continue

            levels = self.paper_trade_manager.calculate_trade_levels(opportunity)
            if not levels:
                continue

            allocation_cash = self.paper_trade_manager.calculate_position_size(opportunity, remaining_cash)
            if allocation_cash <= 0:
                continue

            entry_price = float(levels.get("entry_price") or 0.0)
            if entry_price <= 0:
                continue

            position_units = allocation_cash / entry_price
            if position_units <= 0:
                continue

            position = {
                "coin_id": coin_id,
                "entry_price": entry_price,
                "stop_loss": float(levels.get("stop_loss") or 0.0),
                "take_profit": float(levels.get("take_profit") or 0.0),
                "risk_reward_ratio": float(levels.get("risk_reward_ratio") or 0.0),
                "position_size": position_units,
                "allocated_cash": allocation_cash,
                "status": "OPEN",
            }
            self.open_positions.append(position)
            open_coin_ids.add(coin_id)
            remaining_cash = max(0.0, remaining_cash - allocation_cash)
            new_trades_opened.append(dict(position))

        latest_price_by_coin_id: Dict[str, object] = {}
        for opportunity in valid_opportunities:
            coin_id = self._get_opportunity_coin_id(opportunity)
            if coin_id is None:
                continue
            latest_price_by_coin_id[coin_id] = opportunity.get("current_price")

        trades_closed: List[dict] = []
        active_positions: List[dict] = []
        for position in self.open_positions:
            coin_id = str(position.get("coin_id")) if position.get("coin_id") is not None else ""
            update_result = self.paper_trade_manager.update_open_position(
                position,
                latest_price_by_coin_id.get(coin_id),
            )
            if update_result.get("closed"):
                closed_position = dict(position)
                closed_position.update(update_result)
                trades_closed.append(closed_position)
                continue
            active_positions.append(position)

        self.open_positions = active_positions
        return {
            "new_trades_opened": new_trades_opened,
            "trades_closed": trades_closed,
            "trades_still_open": [dict(position) for position in self.open_positions],
        }


class CachedData:
    """Store data with expiration timestamp."""
    def __init__(self, data, ttl_seconds: int):
        self.data = data
        self.expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
    
    def is_expired(self) -> bool:
        """Check if cached data has expired."""
        return datetime.now() >= self.expires_at


class RateLimitedCache:
    """Manages a cache with TTL and tracks last request time for rate limiting."""
    def __init__(self):
        self.market_data: Optional[CachedData] = None
        self.price_cache: Dict[str, CachedData] = {}
        self.intraday_price_cache: Dict[str, CachedData] = {}
        self.ohlc_price_cache: Dict[str, CachedData] = {}
        self.last_request_time = 0.0
        self.total_api_requests_made = 0
        self.cached_responses_reused = 0
        self.duplicate_requests_avoided = 0
        self.endpoint_request_counts: Dict[str, int] = {}
        self.source_request_counts: Dict[str, int] = {}
        self.request_audit_log: List[dict] = []
        self.ohlc_market_chart_request_counts: Dict[str, int] = {}
        # Phase 2 temporary DEBUG telemetry counters.
        self.scan_total_coins_scanned = 0
        self.scan_sparkline_resolutions = 0
        self.scan_memory_cache_hits = 0
        self.scan_persistent_cache_hits = 0
        self.scan_history_fetches = 0
        self.scan_total_market_chart_requests = 0

    def record_api_request(self, url: str, params: dict | None = None, source: str = "unknown") -> int | None:
        """Track CoinGecko API request volume by endpoint and caller source."""
        if "api.coingecko.com" not in url:
            return None

        self.total_api_requests_made += 1
        endpoint_key = url.split("?")[0]
        if params:
            param_chunks = [f"{key}={params[key]}" for key in sorted(params.keys())]
            endpoint_key = f"{endpoint_key}?{'&'.join(param_chunks)}"

        coin_id: str | None = None
        path_parts = [chunk for chunk in urlparse(url).path.split("/") if chunk]
        if "coins" in path_parts:
            coins_index = path_parts.index("coins")
            if coins_index + 1 < len(path_parts):
                coin_candidate = path_parts[coins_index + 1]
                if coin_candidate not in {"markets"}:
                    coin_id = coin_candidate
        if coin_id is None and params and params.get("ids"):
            coin_id = str(params.get("ids"))

        timestamp = datetime.now().isoformat(timespec="seconds")
        self.endpoint_request_counts[endpoint_key] = self.endpoint_request_counts.get(endpoint_key, 0) + 1
        self.source_request_counts[source] = self.source_request_counts.get(source, 0) + 1
        self.request_audit_log.append(
            {
                "request_number": self.total_api_requests_made,
                "source": source,
                "endpoint": endpoint_key,
                "coin_id": coin_id,
                "timestamp": timestamp,
            }
        )
        return self.total_api_requests_made

    def record_cache_reuse(self) -> None:
        """Track cache reuse events."""
        self.cached_responses_reused += 1

    def record_duplicate_avoided(self) -> None:
        """Track duplicate request avoidance through cache hits."""
        self.duplicate_requests_avoided += 1

    def record_scan_coin_processed(self) -> None:
        """Track how many coins were processed in support/resistance enrichment."""
        self.scan_total_coins_scanned += 1

    def record_scan_sparkline_resolution(self) -> None:
        """Track support/resistance resolutions that used sparkline history."""
        self.scan_sparkline_resolutions += 1

    def record_scan_memory_cache_hit(self) -> None:
        """Track in-memory OHLC cache hits used by support/resistance."""
        self.scan_memory_cache_hits += 1

    def record_scan_persistent_cache_hit(self) -> None:
        """Track persistent OHLC cache hits used by support/resistance."""
        self.scan_persistent_cache_hits += 1

    def record_scan_history_fetch(self) -> None:
        """Track support/resistance fallback history fetch attempts."""
        self.scan_history_fetches += 1

    def record_scan_market_chart_request(self) -> None:
        """Track total market_chart requests issued for support/resistance."""
        self.scan_total_market_chart_requests += 1

    def get_request_stats(self) -> dict:
        """Return a copy of current request statistics."""
        return {
            "total_api_requests_made": self.total_api_requests_made,
            "cached_responses_reused": self.cached_responses_reused,
            "duplicate_requests_avoided": self.duplicate_requests_avoided,
            "endpoint_request_counts": dict(self.endpoint_request_counts),
            "source_request_counts": dict(self.source_request_counts),
            "request_audit_log": list(self.request_audit_log),
            "scan_debug_counters": {
                "total_coins_scanned": self.scan_total_coins_scanned,
                "sparkline_resolutions": self.scan_sparkline_resolutions,
                "memory_cache_hits": self.scan_memory_cache_hits,
                "persistent_cache_hits": self.scan_persistent_cache_hits,
                "history_fetches": self.scan_history_fetches,
                "total_market_chart_requests": self.scan_total_market_chart_requests,
            },
        }
    
    def get_market_data(self) -> Optional[List[dict]]:
        """Retrieve market data if not expired."""
        if self.market_data and not self.market_data.is_expired():
            return self.market_data.data
        return None
    
    def set_market_data(self, data: List[dict]) -> None:
        """Store market data with TTL."""
        self.market_data = CachedData(data, MARKET_DATA_TTL)
    
    def get_historical_prices(self, coin_id: str) -> Optional[List[float]]:
        """Retrieve historical prices if not expired."""
        if coin_id in self.price_cache and not self.price_cache[coin_id].is_expired():
            return self.price_cache[coin_id].data
        return None
    
    def set_historical_prices(self, coin_id: str, prices: List[float]) -> None:
        """Store historical prices with TTL."""
        self.price_cache[coin_id] = CachedData(prices, HISTORICAL_PRICE_TTL)

    def get_intraday_prices(self, coin_id: str) -> Optional[List[Tuple[int, float]]]:
        """Retrieve intraday prices if not expired."""
        if coin_id in self.intraday_price_cache and not self.intraday_price_cache[coin_id].is_expired():
            return self.intraday_price_cache[coin_id].data
        return None

    def set_intraday_prices(self, coin_id: str, prices: List[Tuple[int, float]]) -> None:
        """Store intraday prices with TTL."""
        self.intraday_price_cache[coin_id] = CachedData(prices, INTRADAY_PRICE_TTL)

    def get_ohlc_prices(self, coin_id: str) -> Optional[List[dict]]:
        """Retrieve OHLC candles if not expired."""
        if coin_id in self.ohlc_price_cache and not self.ohlc_price_cache[coin_id].is_expired():
            return self.ohlc_price_cache[coin_id].data
        return None

    def set_ohlc_prices(self, coin_id: str, prices: List[dict]) -> None:
        """Store OHLC candles with TTL."""
        self.ohlc_price_cache[coin_id] = CachedData(prices, OHLC_PRICE_TTL)
    
    def wait_if_needed(self) -> None:
        """Enforce minimum interval between API requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    
    def mark_request_made(self) -> None:
        """Update timestamp of last API request."""
        self.last_request_time = time.time()


def get_usd_to_gbp_rate() -> float:
    """Return the current USD-to-GBP exchange rate, falling back to a default if needed."""
    if getattr(get_usd_to_gbp_rate, "cached_rate", None) is not None:
        return get_usd_to_gbp_rate.cached_rate

    try:
        response = requests.get(USD_TO_GBP_RATE_URL, timeout=2)
        response.raise_for_status()
        payload = response.json() or {}
        rates = payload.get("rates") or {}
        rate = rates.get("GBP")
        if rate is not None:
            rate = float(rate)
            if rate > 0:
                get_usd_to_gbp_rate.cached_rate = rate
                return rate
    except Exception:
        pass

    get_usd_to_gbp_rate.cached_rate = DEFAULT_USD_TO_GBP_RATE
    return get_usd_to_gbp_rate.cached_rate


get_usd_to_gbp_rate.cached_rate = None


def request_with_retry(
    url: str,
    params: dict | None = None,
    timeout: int = 10,
    max_retries: int = 5,
    request_tracker: RateLimitedCache | None = None,
    request_source: str = "unknown",
) -> requests.Response:
    """
    Perform an HTTP GET with bounded retries for transient failures.

    Retries HTTP 429 and temporary server/network errors up to max_retries,
    honoring Retry-After when available and otherwise using exponential backoff.
    """
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    retryable_statuses = {408, 429, 500, 502, 503, 504}

    def _default_backoff(attempt: int) -> int:
        return min(60, max(1, 2 ** attempt))

    def _retry_after_delay(response: requests.Response) -> int | None:
        headers = getattr(response, "headers", None)
        if headers is None:
            return None

        try:
            retry_after_value = headers["Retry-After"]
        except (KeyError, TypeError, IndexError):
            return None

        try:
            return min(60, max(1, int(float(retry_after_value))))
        except (TypeError, ValueError):
            return None

    attempts_made = 0
    while attempts_made < max_retries:
        attempt_index = attempts_made
        attempts_made += 1
        try:
            request_number = None
            request_entry = None
            if request_tracker is not None:
                request_number = request_tracker.record_api_request(url, params=params, source=request_source)
                request_entry = request_tracker.request_audit_log[-1]
            response = requests.get(url, params=params, timeout=timeout)

            if response.status_code in retryable_statuses:
                if response.status_code == 429:
                    logger.warning(
                        "[REQUEST_AUDIT] response_429 request=%s source=%s coin_id=%s endpoint=%s",
                        request_number,
                        request_source,
                        (request_entry or {}).get("coin_id") or "n/a",
                        (request_entry or {}).get("endpoint", url),
                    )
                if attempts_made >= max_retries:
                    raise requests.HTTPError(
                        f"Retryable HTTP status {response.status_code} persisted after {max_retries} attempts"
                    )

                wait_time = _retry_after_delay(response) or _default_backoff(attempt_index)

                console_output = Console()
                if response.status_code == 429:
                    console_output.print(
                        f"[yellow]Rate limited (429). Attempt {attempts_made}/{max_retries}. Waiting {wait_time}s...[/yellow]"
                    )
                else:
                    console_output.print(
                        f"[yellow]Temporary server error ({response.status_code}). Attempt {attempts_made}/{max_retries}. Waiting {wait_time}s...[/yellow]"
                    )
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            return response

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            if attempts_made >= max_retries:
                raise
            wait_time = _default_backoff(attempt_index)
            console_output = Console()
            console_output.print(f"[yellow]Temporary network failure. Retrying in {wait_time}s...[/yellow]")
            time.sleep(wait_time)
        except requests.exceptions.HTTPError:
            raise
        except requests.exceptions.RequestException:
            if attempts_made >= max_retries:
                raise
            wait_time = _default_backoff(attempt_index)
            time.sleep(wait_time)

    raise requests.HTTPError(f"Request failed after {max_retries} attempts")


def fetch_market_data(cache: RateLimitedCache) -> List[dict]:
    """
    Fetch the latest market data for the selected coins from CoinGecko.
    
    Returns cached data if fresh (not expired), otherwise fetches new data
    and enforces rate limiting between requests.
    """
    # Check if cached data is still fresh
    cached_data = cache.get_market_data()
    if cached_data:
        cache.record_cache_reuse()
        cache.record_duplicate_avoided()
        return cached_data
    
    # Enforce minimum time between requests
    cache.wait_if_needed()
    
    params = {
        "vs_currency": "usd",
        "ids": ",".join(coin_id for _, coin_id in COINS),
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "sparkline": True,
        "price_change_percentage": "1h,24h,7d",
    }

    response = request_with_retry(
        API_URL,
        params=params,
        request_tracker=cache,
        request_source="scanner.market_data",
    )
    cache.mark_request_made()
    data = response.json()
    
    # Cache the fetched data
    cache.set_market_data(data)
    return data


def format_currency(value: float | None) -> str:
    """Format large currency values with commas and a dollar sign."""
    if value is None:
        return "N/A"
    return f"${value:,.0f}"


def format_gbp_currency(value: float | None) -> str:
    """Format a currency value in GBP."""
    if value is None:
        return "N/A"
    return f"£{value:,.2f}"


STATUS_STRONG_GO = "🟢 STRONG / GO"
STATUS_WATCH = "🟡 WATCH"
STATUS_AVOID_RISK = "🔴 AVOID / RISK"
STATUS_INFO = "🔵 INFO"
STATUS_SIGNAL = "🟣 SIGNAL"
STATUS_NEUTRAL = "⚪ NEUTRAL"


def get_rating_status_label(rating: str) -> str:
    """Map rating labels to the standardized status system."""
    if rating in {"Strong Buy", "Buy"}:
        return STATUS_STRONG_GO
    if rating == "Hold":
        return STATUS_WATCH
    if rating in {"Sell", "Strong Sell"}:
        return STATUS_AVOID_RISK
    return STATUS_NEUTRAL


def get_trend_status_label(trend: str) -> str:
    """Map trend labels to the standardized status system."""
    if trend == "Bullish":
        return STATUS_STRONG_GO
    if trend == "Bearish":
        return STATUS_AVOID_RISK
    return STATUS_NEUTRAL


def get_volume_status_label(volume_status: str) -> str:
    """Map volume classifications to the standardized status system."""
    if volume_status == "High":
        return STATUS_SIGNAL
    if volume_status == "Low":
        return STATUS_AVOID_RISK
    return STATUS_NEUTRAL


def get_level_status_label(level_status: str) -> str:
    """Map support/resistance position to the standardized status system."""
    if level_status in {"Breaking Resistance", "Near Support"}:
        return STATUS_STRONG_GO if level_status == "Breaking Resistance" else STATUS_SIGNAL
    if level_status in {"Near Resistance", "Breaking Support"}:
        return STATUS_AVOID_RISK
    return STATUS_NEUTRAL


def get_action_status_label(action: str) -> str:
    """Map suggested actions to the standardized status system."""
    if action == "BUY":
        return STATUS_STRONG_GO
    if action == "WATCH":
        return STATUS_WATCH
    if action == "AVOID":
        return STATUS_AVOID_RISK
    return STATUS_NEUTRAL


def format_change(value: float | None) -> Text:
    """Return a Rich Text object with green/red styling for percentage changes."""
    if value is None:
        value = 0.0

    text = Text(f"{value:+.2f}%")
    text.stylize("green" if value >= 0 else "red")
    return text


def get_volume_status(entry: dict) -> str:
    """Classify trading volume relative to the coin's market cap."""
    if not entry:
        return "Normal"

    market_cap = float(entry.get("market_cap") or 0.0)
    total_volume = float(entry.get("total_volume") or 0.0)

    if market_cap <= 0:
        return "Normal"

    ratio = total_volume / market_cap
    if ratio >= 0.2:
        return "High"
    if ratio <= 0.005:
        return "Low"
    return "Normal"


def _get_cached_historical_prices(cache: object, coin_id: str) -> Optional[List[float]]:
    """Retrieve historical prices from either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "get_historical_prices"):
        return cache.get_historical_prices(coin_id)
    if isinstance(cache, dict):
        return cache.get(coin_id)
    return None


def _set_cached_historical_prices(cache: object, coin_id: str, prices: List[float]) -> None:
    """Store historical prices in either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "set_historical_prices"):
        cache.set_historical_prices(coin_id, prices)
    elif isinstance(cache, dict):
        cache[coin_id] = prices


def _get_cached_intraday_prices(cache: object, coin_id: str) -> Optional[List[Tuple[int, float]]]:
    """Retrieve intraday prices from either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "get_intraday_prices"):
        return cache.get_intraday_prices(coin_id)
    if isinstance(cache, dict):
        return cache.get(f"intraday:{coin_id}")
    return None


def _set_cached_intraday_prices(cache: object, coin_id: str, prices: List[Tuple[int, float]]) -> None:
    """Store intraday prices in either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "set_intraday_prices"):
        cache.set_intraday_prices(coin_id, prices)
    elif isinstance(cache, dict):
        cache[f"intraday:{coin_id}"] = prices


def _get_cached_ohlc_prices(cache: object, coin_id: str) -> Optional[List[dict]]:
    """Retrieve OHLC candles from either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "get_ohlc_prices"):
        return cache.get_ohlc_prices(coin_id)
    if isinstance(cache, dict):
        return cache.get(f"ohlc:{coin_id}")
    return None


def _set_cached_ohlc_prices(cache: object, coin_id: str, prices: List[dict]) -> None:
    """Store OHLC candles in either a RateLimitedCache or a simple dict."""
    if hasattr(cache, "set_ohlc_prices"):
        cache.set_ohlc_prices(coin_id, prices)
    elif isinstance(cache, dict):
        cache[f"ohlc:{coin_id}"] = prices


def _load_persistent_ohlc_cache() -> Dict[str, dict]:
    """Load persisted OHLC fallback cache from disk."""
    if not os.path.exists(OHLC_PERSISTENT_CACHE_FILE):
        return {}

    try:
        with open(OHLC_PERSISTENT_CACHE_FILE, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
            if isinstance(payload, dict):
                return payload
    except (OSError, ValueError, TypeError):
        return {}

    return {}


def _save_persistent_ohlc_cache(payload: Dict[str, dict]) -> None:
    """Persist OHLC fallback cache to disk."""
    try:
        with open(OHLC_PERSISTENT_CACHE_FILE, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj)
    except OSError:
        return


def _get_persistent_ohlc_prices(coin_id: str) -> Optional[List[dict]]:
    """Return persisted OHLC fallback candles when still within TTL."""
    payload = _load_persistent_ohlc_cache()
    entry = payload.get(coin_id)
    if not isinstance(entry, dict):
        return None

    fetched_at = entry.get("fetched_at")
    candles = entry.get("candles")
    if not isinstance(candles, list):
        return None

    try:
        fetched_at_ts = float(fetched_at)
    except (TypeError, ValueError):
        return None

    if (time.time() - fetched_at_ts) > OHLC_PRICE_TTL:
        return None

    return candles


def _set_persistent_ohlc_prices(coin_id: str, prices: List[dict]) -> None:
    """Persist OHLC fallback candles for reuse across restarts within TTL."""
    payload = _load_persistent_ohlc_cache()
    payload[coin_id] = {
        "fetched_at": time.time(),
        "candles": prices,
    }
    _save_persistent_ohlc_cache(payload)


def _safe_percent(entry: dict, key: str) -> float:
    """Return a percentage field as a float, defaulting to zero."""
    return float(entry.get(key) or 0.0)


def _get_current_price(entry: dict) -> float:
    """Return the current price for an entry, defaulting to zero."""
    current_price = entry.get("current_price")
    if current_price is None:
        current_price = entry.get("price")
    return float(current_price or 0.0)


def normalize_ohlc_candles(raw_candles: List[object]) -> List[dict]:
    """Normalize OHLC payloads into a sorted list of candle dictionaries."""
    candles: List[dict] = []
    for candle in raw_candles:
        if isinstance(candle, dict):
            timestamp = candle.get("timestamp") or candle.get("time") or candle.get("date")
            open_price = candle.get("open")
            high_price = candle.get("high")
            low_price = candle.get("low")
            close_price = candle.get("close")
        elif isinstance(candle, (list, tuple)) and len(candle) >= 5:
            timestamp, open_price, high_price, low_price, close_price = candle[:5]
        else:
            continue

        if open_price is None or high_price is None or low_price is None or close_price is None:
            continue

        candles.append(
            {
                "timestamp": timestamp,
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
            }
        )

    candles.sort(key=lambda candle: float(candle.get("timestamp") or 0.0))
    return candles

def coerce_support_resistance_candles(prices: List[object]) -> List[dict]:
    """Normalize either OHLC candles or plain price points into candle dictionaries."""
    if not prices:
        return []

    first_item = prices[0]
    if isinstance(first_item, dict):
        return normalize_ohlc_candles(prices)

    numeric_prices = [float(price) for price in prices if price is not None]
    if len(numeric_prices) < 3:
        return []

    return [
        {
            "timestamp": index,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
        }
        for index, price in enumerate(numeric_prices)
    ]


def detect_swings_from_candles(candles: List[dict]) -> tuple[List[float], List[float]]:
    """Return swing low and swing high candidates from OHLC candles."""
    support_candidates: List[float] = []
    resistance_candidates: List[float] = []

    if len(candles) < 3:
        return support_candidates, resistance_candidates

    for index in range(1, len(candles) - 1):
        candle = candles[index]
        previous_candle = candles[index - 1]
        next_candle = candles[index + 1]
        low_price = float(candle["low"])
        high_price = float(candle["high"])

        if low_price < float(previous_candle["low"]) and low_price < float(next_candle["low"]):
            support_candidates.append(low_price)
        if high_price > float(previous_candle["high"]) and high_price > float(next_candle["high"]):
            resistance_candidates.append(high_price)

    return support_candidates, resistance_candidates


def detect_candlestick_pattern(candles: List[dict]) -> tuple[str, str]:
    """Detect simple candlestick patterns from recent OHLC candles."""
    normalized = normalize_ohlc_candles(candles)
    if len(normalized) < 2:
        return "None Detected", ""

    previous = normalized[-2]
    current = normalized[-1]

    prev_open = float(previous["open"])
    prev_close = float(previous["close"])
    curr_open = float(current["open"])
    curr_close = float(current["close"])

    previous_bearish = prev_close < prev_open
    previous_bullish = prev_close > prev_open
    current_bullish = curr_close > curr_open
    current_bearish = curr_close < curr_open

    # Bullish engulfing: bearish candle followed by a larger bullish body that engulfs it.
    if previous_bearish and current_bullish and curr_open <= prev_close and curr_close >= prev_open:
        return (
            "Bullish Engulfing",
            "Buyers fully reversed the prior candle, suggesting upward momentum may be building.",
        )

    # Bearish engulfing: bullish candle followed by a larger bearish body that engulfs it.
    if previous_bullish and current_bearish and curr_open >= prev_close and curr_close <= prev_open:
        return (
            "Bearish Engulfing",
            "Sellers overwhelmed the prior candle, suggesting downside pressure may increase.",
        )

    high_price = float(current["high"])
    low_price = float(current["low"])
    body_size = abs(curr_close - curr_open)
    total_range = max(high_price - low_price, 0.0)
    lower_wick = min(curr_open, curr_close) - low_price
    upper_wick = high_price - max(curr_open, curr_close)

    # Hammer: small body near the top of the range with a long lower shadow.
    if total_range > 0:
        effective_body = max(body_size, total_range * 0.03)
        if lower_wick >= 2 * effective_body and upper_wick <= effective_body:
            return (
                "Hammer",
                "Price was rejected from lower levels, which can signal a potential bullish reversal.",
            )

    return "None Detected", ""


def calculate_support_resistance(prices: List[object], current_price: float) -> dict:
    """Return the nearest support/resistance levels and current price status."""
    candles = coerce_support_resistance_candles(prices)
    if len(candles) < 3 or current_price <= 0:
        return {
            "support": None,
            "resistance": None,
            "status": "Between Levels",
            "explanation": "Recent price structure is incomplete, so support and resistance are not actionable yet.",
            "score_adjustment": 0,
        }

    support_candidates, resistance_candidates = detect_swings_from_candles(candles)

    if not support_candidates:
        support_candidates = sorted({candle["low"] for candle in candles})
    if not resistance_candidates:
        resistance_candidates = sorted({candle["high"] for candle in candles})

    support = max((price for price in support_candidates if price < current_price), default=None)
    resistance = min((price for price in resistance_candidates if price > current_price), default=None)

    if support is None and candles:
        support = min(candle["low"] for candle in candles)
    if resistance is None and candles:
        resistance = max(candle["high"] for candle in candles)

    support_gap = ((current_price - support) / support) if support > 0 else 0.0
    resistance_gap = ((resistance - current_price) / resistance) if resistance > 0 else 0.0
    breakout_threshold = 0.01
    proximity_threshold = 0.02

    if current_price > resistance * (1 + breakout_threshold):
        status = "Breaking Resistance"
        explanation = "Price is clearing resistance, which can confirm upside momentum if the breakout holds."
        score_adjustment = 6
    elif current_price < support * (1 - breakout_threshold):
        status = "Breaking Support"
        explanation = "Price is losing support, which raises the risk of continued downside if sellers stay in control."
        score_adjustment = -6
    elif support_gap <= proximity_threshold:
        status = "Near Support"
        explanation = "Price is sitting close to support, which can offer a tighter-risk entry if buyers defend the level."
        score_adjustment = 4
    elif resistance_gap <= proximity_threshold:
        status = "Near Resistance"
        explanation = "Price is pressing into resistance, so upside may stall unless buyers force a breakout."
        score_adjustment = -4
    else:
        status = "Between Levels"
        explanation = "Price is trading between support and resistance, so the next directional move is still being set up."
        score_adjustment = 0

    return {
        "support": support,
        "resistance": resistance,
        "status": status,
        "explanation": explanation,
        "score_adjustment": score_adjustment,
    }


def estimate_rsi_from_market_data(entry: dict) -> float | None:
    """Approximate RSI using the current 1h/24h/7d market snapshot."""
    if not entry:
        return None

    weekly_change = _safe_percent(entry, "price_change_percentage_7d_in_currency")
    daily_change = _safe_percent(entry, "price_change_percentage_24h_in_currency")
    hourly_change = _safe_percent(entry, "price_change_percentage_1h_in_currency")

    change_buckets = [
        *[(weekly_change - daily_change) / 8] * 8,
        *[(daily_change - hourly_change) / 4] * 4,
        *[hourly_change / 3] * 3,
    ]

    synthetic_prices = [100.0]
    for bucket_change in change_buckets:
        synthetic_prices.append(synthetic_prices[-1] * (1 + (bucket_change / 100)))

    return calculate_rsi(synthetic_prices)


def analyze_multi_timeframe_from_market_data(entry: dict) -> dict:
    """Estimate 15m/1h/4h momentum directly from the current market snapshot."""
    one_hour_change = _safe_percent(entry, "price_change_percentage_1h_in_currency")
    four_hour_change = _safe_percent(entry, "price_change_percentage_24h_in_currency") / 6
    fifteen_minute_change = one_hour_change / 4

    timeframe_changes = {
        "15m": fifteen_minute_change,
        "1h": one_hour_change,
        "4h": four_hour_change,
    }
    timeframe_weights = {"15m": 0.25, "1h": 0.35, "4h": 0.40}

    timeframe_result = {}
    weighted_score = 0.0
    bullish_count = 0
    bearish_count = 0

    for timeframe_name, change_percent in timeframe_changes.items():
        trend, tf_score = _classify_timeframe_change(change_percent)
        if trend == "Bullish":
            bullish_count += 1
        elif trend == "Bearish":
            bearish_count += 1

        timeframe_result[timeframe_name] = {
            "change_percent": change_percent,
            "trend": trend,
            "score": tf_score,
        }
        weighted_score += tf_score * timeframe_weights[timeframe_name]

    composite_score = round(weighted_score * 100)
    if bullish_count > bearish_count:
        composite_trend = "Bullish"
    elif bearish_count > bullish_count:
        composite_trend = "Bearish"
    else:
        composite_trend = "Sideways"

    return {
        "timeframes": timeframe_result,
        "composite_score": composite_score,
        "composite_trend": composite_trend,
    }


def fetch_historical_prices(coin_id: str, cache: RateLimitedCache | dict | None = None) -> List[float]:
    """
    Fetch daily historical prices for a coin from CoinGecko.
    
    Returns cached data if fresh (not expired), otherwise fetches new data
    with rate limiting. Caches for 5 minutes since daily prices are stable.
    """
    if not coin_id:
        return []

    if cache is None:
        cache = {}

    # Check if cached historical data is still fresh
    cached_prices = _get_cached_historical_prices(cache, coin_id)
    if cached_prices:
        if hasattr(cache, "record_cache_reuse"):
            cache.record_cache_reuse()
        if hasattr(cache, "record_duplicate_avoided"):
            cache.record_duplicate_avoided()
        return cached_prices

    # Enforce minimum time between requests when rate-limited cache is available
    if hasattr(cache, "wait_if_needed"):
        cache.wait_if_needed()

    params = {
        "vs_currency": "usd",
        "days": 30,
        "interval": "daily",
    }
    tracker = cache if hasattr(cache, "record_api_request") else None
    response = request_with_retry(
        HISTORY_API_URL.format(coin_id=coin_id),
        params=params,
        request_tracker=tracker,
        request_source=f"rsi.historical_prices[{coin_id}]",
    )
    if hasattr(cache, "mark_request_made"):
        cache.mark_request_made()
    
    payload = response.json()
    prices = payload.get("prices", [])
    normalized_prices = [float(point[1]) for point in prices if len(point) > 1]

    # Cache the historical prices with TTL
    _set_cached_historical_prices(cache, coin_id, normalized_prices)
    return normalized_prices


def fetch_intraday_prices(coin_id: str, cache: RateLimitedCache | dict | None = None) -> List[Tuple[int, float]]:
    """Fetch intraday prices and return a normalized list of (timestamp_ms, price)."""
    if not coin_id:
        return []

    if cache is None:
        cache = {}

    cached_prices = _get_cached_intraday_prices(cache, coin_id)
    if cached_prices:
        if hasattr(cache, "record_cache_reuse"):
            cache.record_cache_reuse()
        if hasattr(cache, "record_duplicate_avoided"):
            cache.record_duplicate_avoided()
        return cached_prices

    if hasattr(cache, "wait_if_needed"):
        cache.wait_if_needed()

    params = {
        "vs_currency": "usd",
        "days": 1,
    }
    tracker = cache if hasattr(cache, "record_api_request") else None
    response = request_with_retry(
        HISTORY_API_URL.format(coin_id=coin_id),
        params=params,
        request_tracker=tracker,
        request_source=f"multi_timeframe.intraday_prices[{coin_id}]",
    )
    if hasattr(cache, "mark_request_made"):
        cache.mark_request_made()

    payload = response.json()
    prices = payload.get("prices", [])
    normalized_prices = [(int(point[0]), float(point[1])) for point in prices if len(point) > 1]

    _set_cached_intraday_prices(cache, coin_id, normalized_prices)
    return normalized_prices


def fetch_historical_ohlc_prices(coin_id: str, cache: RateLimitedCache | dict | None = None) -> List[dict]:
    """Fetch OHLC-style candle history for a coin using CoinGecko market-chart data."""
    if not coin_id:
        return []

    if cache is None:
        cache = {}

    if DEBUG:
        logger.info(
            "[DEBUG] fetch_historical_ohlc_prices entry coin_id=%s cache_type=%s",
            coin_id,
            type(cache).__name__,
        )

    cached_prices = _get_cached_ohlc_prices(cache, coin_id)
    if cached_prices:
        if DEBUG:
            logger.info(
                "[DEBUG] fetch_historical_ohlc_prices coin_id=%s source=in-memory-cache candles=%d",
                coin_id,
                len(cached_prices),
            )
        if hasattr(cache, "record_cache_reuse"):
            cache.record_cache_reuse()
        if hasattr(cache, "record_duplicate_avoided"):
            cache.record_duplicate_avoided()
        if hasattr(cache, "record_scan_memory_cache_hit"):
            cache.record_scan_memory_cache_hit()
        return cached_prices

    persisted_prices = _get_persistent_ohlc_prices(coin_id)
    if persisted_prices:
        if DEBUG:
            logger.info(
                "[DEBUG] fetch_historical_ohlc_prices coin_id=%s source=persistent-cache candles=%d",
                coin_id,
                len(persisted_prices),
            )
        _set_cached_ohlc_prices(cache, coin_id, persisted_prices)
        if hasattr(cache, "record_cache_reuse"):
            cache.record_cache_reuse()
        if hasattr(cache, "record_duplicate_avoided"):
            cache.record_duplicate_avoided()
        if hasattr(cache, "record_scan_persistent_cache_hit"):
            cache.record_scan_persistent_cache_hit()
        return persisted_prices

    if DEBUG:
        logger.info(
            "[DEBUG] fetch_historical_ohlc_prices coin_id=%s source=coingecko-api",
            coin_id,
        )

    if hasattr(cache, "wait_if_needed"):
        cache.wait_if_needed()

    params = {
        "vs_currency": "usd",
        "days": "max",
        "interval": "hourly",
    }
    tracker = cache if hasattr(cache, "record_api_request") else None
    if hasattr(cache, "record_scan_history_fetch"):
        cache.record_scan_history_fetch()
    if hasattr(cache, "record_scan_market_chart_request"):
        cache.record_scan_market_chart_request()
    market_chart_request_count = 1
    if hasattr(cache, "ohlc_market_chart_request_counts"):
        request_counts = getattr(cache, "ohlc_market_chart_request_counts")
        request_counts[coin_id] = request_counts.get(coin_id, 0) + 1
        market_chart_request_count = request_counts[coin_id]
    if DEBUG:
        logger.info(
            "[DEBUG] CoinGecko market_chart request coin_id=%s per_scan_count=%d endpoint=%s",
            coin_id,
            market_chart_request_count,
            HISTORY_API_URL.format(coin_id=coin_id),
        )
    response = request_with_retry(
        HISTORY_API_URL.format(coin_id=coin_id),
        params=params,
        request_tracker=tracker,
        request_source=f"support_resistance.market_chart[{coin_id}]",
    )
    if hasattr(cache, "mark_request_made"):
        cache.mark_request_made()

    payload = response.json()
    price_points = payload.get("prices", []) if isinstance(payload, dict) else []
    normalized_points = [
        (int(point[0]), float(point[1]))
        for point in price_points
        if isinstance(point, (list, tuple)) and len(point) > 1
    ]

    if not normalized_points:
        _set_cached_ohlc_prices(cache, coin_id, [])
        return []

    bucket_size = max(1, len(normalized_points) // 100)
    normalized_candles: List[dict] = []
    for start_index in range(0, len(normalized_points), bucket_size):
        bucket = normalized_points[start_index : start_index + bucket_size]
        if not bucket:
            continue
        prices_only = [price for _, price in bucket]
        normalized_candles.append(
            {
                "timestamp": bucket[-1][0],
                "open": prices_only[0],
                "high": max(prices_only),
                "low": min(prices_only),
                "close": prices_only[-1],
            }
        )

    _set_cached_ohlc_prices(cache, coin_id, normalized_candles)
    _set_persistent_ohlc_prices(coin_id, normalized_candles)
    return normalized_candles


def _get_price_at_or_before(prices: List[Tuple[int, float]], target_timestamp_ms: int) -> float | None:
    """Return the latest price at or before target timestamp."""
    candidate_price = None
    for timestamp_ms, price in prices:
        if timestamp_ms <= target_timestamp_ms:
            candidate_price = price
        else:
            break
    return candidate_price


def _classify_timeframe_change(change_percent: float) -> Tuple[str, float]:
    """Map timeframe percentage change to a trend label and a normalized score."""
    if change_percent >= 1.0:
        return "Bullish", 1.0
    if change_percent >= 0.3:
        return "Bullish", 0.75
    if change_percent <= -1.0:
        return "Bearish", 0.0
    if change_percent <= -0.3:
        return "Bearish", 0.25
    return "Sideways", 0.5


def analyze_multi_timeframe(prices: List[Tuple[int, float]]) -> dict:
    """Analyze 15m, 1h, and 4h momentum from intraday prices."""
    if not prices:
        return {
            "timeframes": {
                "15m": {"change_percent": 0.0, "trend": "Sideways", "score": 0.5},
                "1h": {"change_percent": 0.0, "trend": "Sideways", "score": 0.5},
                "4h": {"change_percent": 0.0, "trend": "Sideways", "score": 0.5},
            },
            "composite_score": 50,
            "composite_trend": "Sideways",
        }

    last_timestamp_ms, last_price = prices[-1]
    timeframe_minutes = {"15m": 15, "1h": 60, "4h": 240}
    timeframe_weights = {"15m": 0.25, "1h": 0.35, "4h": 0.40}

    timeframe_result = {}
    weighted_score = 0.0
    bullish_count = 0
    bearish_count = 0

    for timeframe_name, minutes in timeframe_minutes.items():
        target_timestamp_ms = last_timestamp_ms - (minutes * 60 * 1000)
        baseline_price = _get_price_at_or_before(prices, target_timestamp_ms)
        if baseline_price is None or baseline_price <= 0:
            change_percent = 0.0
            trend, tf_score = "Sideways", 0.5
        else:
            change_percent = ((last_price - baseline_price) / baseline_price) * 100
            trend, tf_score = _classify_timeframe_change(change_percent)

        if trend == "Bullish":
            bullish_count += 1
        elif trend == "Bearish":
            bearish_count += 1

        timeframe_result[timeframe_name] = {
            "change_percent": change_percent,
            "trend": trend,
            "score": tf_score,
        }
        weighted_score += tf_score * timeframe_weights[timeframe_name]

    composite_score = round(weighted_score * 100)
    if bullish_count > bearish_count:
        composite_trend = "Bullish"
    elif bearish_count > bullish_count:
        composite_trend = "Bearish"
    else:
        composite_trend = "Sideways"

    return {
        "timeframes": timeframe_result,
        "composite_score": composite_score,
        "composite_trend": composite_trend,
    }


def calculate_rsi(prices: List[float], period: int = 14) -> float | None:
    """Calculate the standard RSI-14 from a list of historical prices."""
    if not prices or len(prices) < period + 1:
        return None

    changes = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    recent_changes = changes[-period:]

    gains = [change for change in recent_changes if change > 0]
    losses = [-change for change in recent_changes if change < 0]

    if not gains and not losses:
        return 50.0

    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_rsi_status(rsi: float | None) -> str:
    """Classify RSI values as Oversold, Neutral, or Overbought."""
    if rsi is None:
        return "Neutral"
    if rsi <= 30:
        return "Oversold"
    if rsi >= 70:
        return "Overbought"
    return "Neutral"


def format_rsi(entry: dict) -> str:
    """Return a human-readable RSI value with its classification."""
    if not entry:
        return "N/A"

    rsi_value = entry.get("rsi_14")
    if rsi_value is None:
        return "N/A"

    rsi_float = float(rsi_value)
    return f"{rsi_float:.1f} ({get_rsi_status(rsi_float)})"


def enrich_market_data_with_rsi(data: List[dict], cache: RateLimitedCache | dict | None = None) -> List[dict]:
    """
    Attach an RSI-style momentum reading derived from the current market snapshot.
    """
    enriched: List[dict] = []

    for entry in data:
        enriched_entry = dict(entry)
        enriched_entry["rsi_14"] = estimate_rsi_from_market_data(enriched_entry)
        enriched.append(enriched_entry)
    return enriched


def enrich_market_data_with_multi_timeframe(data: List[dict], cache: RateLimitedCache | dict | None = None) -> List[dict]:
    """Attach 15m/1h/4h multi-timeframe analysis from the market snapshot."""
    enriched: List[dict] = []

    for entry in data:
        enriched_entry = dict(entry)
        analysis = analyze_multi_timeframe_from_market_data(enriched_entry)
        enriched_entry["multi_timeframe"] = analysis
        enriched_entry["multi_timeframe_score"] = analysis.get("composite_score", 50)
        enriched.append(enriched_entry)

    return enriched


def enrich_market_data_with_support_resistance(data: List[dict], cache: RateLimitedCache | dict | None = None) -> List[dict]:
    """Attach support/resistance levels derived from recent embedded price history."""
    enriched: List[dict] = []
    max_history_fetches = max(0, int(TOP_HISTORY_FETCH))
    apply_history_fetch_cap = hasattr(cache, "record_scan_history_fetch")
    history_fetches_this_pass = 0

    for entry in data:
        enriched_entry = dict(entry)
        if hasattr(cache, "record_scan_coin_processed"):
            cache.record_scan_coin_processed()
        coin_id = enriched_entry.get("id")
        current_price = _get_current_price(enriched_entry)
        sparkline = enriched_entry.get("sparkline_in_7d") or {}
        historical_prices = [float(price) for price in (sparkline.get("price") or []) if price is not None]
        if current_price > 0 and (not historical_prices or historical_prices[-1] != current_price):
            historical_prices.append(current_price)

        # Prefer scan-fresh sparkline data and only fall back to market-chart history when it is missing/insufficient.
        if len(historical_prices) >= 3:
            if hasattr(cache, "record_scan_sparkline_resolution"):
                cache.record_scan_sparkline_resolution()
            levels = calculate_support_resistance(historical_prices, current_price)
            candlestick_pattern, candlestick_explanation = ("None Detected", "")
        else:
            should_fetch_history = bool(coin_id)
            if apply_history_fetch_cap and history_fetches_this_pass >= max_history_fetches:
                should_fetch_history = False
                if DEBUG:
                    logger.info(
                        "[DEBUG] support_resistance history fetch skipped for coin_id=%s due to TOP_HISTORY_FETCH=%d cap",
                        coin_id,
                        max_history_fetches,
                    )

            candles = fetch_historical_ohlc_prices(coin_id, cache=cache) if should_fetch_history else []
            if should_fetch_history:
                history_fetches_this_pass += 1
            if candles:
                levels = calculate_support_resistance(candles, current_price)
                candlestick_pattern, candlestick_explanation = detect_candlestick_pattern(candles)
            else:
                levels = calculate_support_resistance(historical_prices, current_price)
                candlestick_pattern, candlestick_explanation = ("None Detected", "")
        enriched_entry["support_level"] = levels["support"]
        enriched_entry["resistance_level"] = levels["resistance"]
        enriched_entry["support_resistance_status"] = levels["status"]
        enriched_entry["support_resistance_explanation"] = levels["explanation"]
        enriched_entry["support_resistance_score_adjustment"] = levels["score_adjustment"]
        enriched_entry["candlestick_pattern"] = candlestick_pattern
        enriched_entry["candlestick_pattern_explanation"] = candlestick_explanation
        enriched.append(enriched_entry)

    return enriched


def enrich_market_data_with_indicators(data: List[dict], cache: RateLimitedCache | dict | None = None) -> List[dict]:
    """Attach all computed indicators used by the scanner."""
    with_rsi = enrich_market_data_with_rsi(data, cache=cache)
    with_levels = enrich_market_data_with_support_resistance(with_rsi, cache=cache)
    return enrich_market_data_with_multi_timeframe(with_levels, cache=cache)


def calculate_opportunity_score(
    entry: dict,
    max_positive_24h: float,
    max_positive_7d: float,
    max_volume: float,
    max_market_cap: float,
) -> int:
    """Score a coin from 0 to 100 using positive momentum, volume and market-cap signals."""
    if not entry:
        return 0

    price_change_24h = max(float(entry.get("price_change_percentage_24h_in_currency") or 0.0), 0.0)
    price_change_7d = max(float(entry.get("price_change_percentage_7d_in_currency") or 0.0), 0.0)
    total_volume = float(entry.get("total_volume") or 0.0)
    market_cap = float(entry.get("market_cap") or 0.0)
    rsi = float(entry.get("rsi_14") or 0.0)
    multi_timeframe_score = float(entry.get("multi_timeframe_score") or 50.0)
    support_resistance_score_adjustment = float(entry.get("support_resistance_score_adjustment") or 0.0)

    normalized_24h = price_change_24h / max_positive_24h if max_positive_24h else 0.0
    normalized_7d = price_change_7d / max_positive_7d if max_positive_7d else 0.0
    normalized_volume = total_volume / max_volume if max_volume else 0.0
    normalized_market_cap = market_cap / max_market_cap if max_market_cap else 0.0

    # Scoring weights
    WEIGHT_24H = 0.30
    WEIGHT_7D = 0.30
    WEIGHT_VOLUME = 0.15
    WEIGHT_MARKET_CAP = 0.15
    WEIGHT_MULTI_TIMEFRAME = 0.10

    # Bonus / penalty values
    RSI_BONUS = 5
    VOLUME_BONUS = 5

    score = (
        WEIGHT_24H * normalized_24h
        + WEIGHT_7D * normalized_7d
        + WEIGHT_VOLUME * normalized_volume
        + WEIGHT_MARKET_CAP * normalized_market_cap
        + WEIGHT_MULTI_TIMEFRAME * (multi_timeframe_score / 100)
    ) * 100

    rsi_status = get_rsi_status(rsi)
    if rsi_status == "Oversold":
        score += RSI_BONUS
    elif rsi_status == "Overbought":
        score -= RSI_BONUS

    volume_status = get_volume_status(entry)
    if volume_status == "High":
        score += VOLUME_BONUS
    elif volume_status == "Low":
        score -= VOLUME_BONUS

    score += support_resistance_score_adjustment

    return min(100, max(0, round(score)))

def rank_opportunity(data: List[dict]) -> List[Tuple[str, str, int]]:
    """Return the tracked coins sorted by opportunity score descending, then volume."""
    coin_lookup = {entry["id"]: entry for entry in data}
    ranked: List[Tuple[str, str, int]] = []

    positive_24h_values = [
        max(float(coin_lookup.get(coin_id, {}).get("price_change_percentage_24h_in_currency") or 0.0), 0.0)
        for _, coin_id in COINS
        if coin_lookup.get(coin_id)
    ]
    positive_7d_values = [
        max(float(coin_lookup.get(coin_id, {}).get("price_change_percentage_7d_in_currency") or 0.0), 0.0)
        for _, coin_id in COINS
        if coin_lookup.get(coin_id)
    ]
    volume_values = [float(coin_lookup.get(coin_id, {}).get("total_volume") or 0.0) for _, coin_id in COINS if coin_lookup.get(coin_id)]
    market_cap_values = [float(coin_lookup.get(coin_id, {}).get("market_cap") or 0.0) for _, coin_id in COINS if coin_lookup.get(coin_id)]

    max_positive_24h = max(positive_24h_values) if positive_24h_values else 0.0
    max_positive_7d = max(positive_7d_values) if positive_7d_values else 0.0
    max_volume = max(volume_values) if volume_values else 0.0
    max_market_cap = max(market_cap_values) if market_cap_values else 0.0

    for display_name, coin_id in COINS:
        entry = coin_lookup.get(coin_id)
        if entry is None:
            continue

        score = calculate_opportunity_score(
            entry,
            max_positive_24h,
            max_positive_7d,
            max_volume,
            max_market_cap,
        )
        ranked.append((display_name, coin_id, score))

    ranked.sort(key=lambda item: (item[2], float(coin_lookup.get(item[1], {}).get("total_volume") or 0.0)), reverse=True)
    return ranked


def build_top_opportunities_summary(data: List[dict]) -> str:
    """Return a summary of the top three ranked opportunities."""
    ranked = rank_opportunity(data)
    summary_lines = []

    for index, (display_name, coin_id, score) in enumerate(ranked[:3], start=1):
        entry = next((entry for entry in data if entry.get("id") == coin_id), None)
        if entry is None:
            continue

        trend = get_trend(entry)
        rating = get_rating(score, trend)
        volume_status = get_volume_status(entry)
        rating_status = get_rating_status_label(rating)
        summary_lines.append(
            f"#{index} {display_name} | Opportunity score: {score} | Trend: {trend} | Signal: {rating} | Status: {rating_status} | Volume: {volume_status} | RSI: {format_rsi(entry)}"
        )

    return "\n".join(summary_lines)


def get_suggested_action(score: int, trend: str, rsi_value: float | None, volume_status: str) -> Tuple[str, int]:
    """Return a weighted action and confidence percentage based on the existing indicators."""
    if rsi_value is None:
        rsi_value = 50.0

    action = "AVOID"
    positive_indicators = 0

    if score >= 40:
        positive_indicators += 1
    if trend == "Bullish":
        positive_indicators += 1
    if 45 <= rsi_value <= 65:
        positive_indicators += 1
    if volume_status in {"Normal", "High"}:
        positive_indicators += 1

    if score >= 40 and trend == "Bullish" and 45 <= rsi_value <= 65 and volume_status in {"Normal", "High"}:
        action = "BUY"
    elif 25 <= score <= 39 and (trend == "sideways" or score < 40) and 35 <= rsi_value <= 70:
        action = "WATCH"
    elif score < 25 or trend == "Bearish" or rsi_value < 30 or rsi_value > 75:
        action = "AVOID"

    if action == "BUY":
        confidence = min(95, 50 + (positive_indicators * 10))
    elif action == "WATCH":
        confidence = min(95, 50 + (positive_indicators * 8))
    else:
        confidence = min(95, 50 + (positive_indicators * 6))

    return action, confidence


def get_market_data_for_iteration(
    iteration: int,
    current_data: List[dict],
    fetch_market_data_func,
    enrich_data_func,
    cache: object,
) -> List[dict]:
    """Reuse the initial snapshot on iteration 0 and fetch fresh data otherwise."""
    if iteration == 0:
        return current_data

    if fetch_market_data_func is None:
        return current_data

    try:
        market_data = fetch_market_data_func()
    except Exception:
        return current_data

    if enrich_data_func is None:
        return market_data

    return enrich_data_func(market_data, cache=cache)


def _build_ranked_opportunities_for_trade_decision(data: List[dict]) -> List[dict]:
    """Return ranked opportunities with core metrics for paper-trade gating."""
    coin_lookup = {entry.get("id"): entry for entry in data if isinstance(entry, dict)}
    ranked_opportunities: List[dict] = []

    for rank_position, (display_name, coin_id, score) in enumerate(rank_opportunity(data), start=1):
        entry = coin_lookup.get(coin_id)
        if entry is None:
            continue

        trend = get_trend(entry)
        rating = get_rating(score, trend)
        rsi_value = entry.get("rsi_14")
        volume_status = get_volume_status(entry)
        suggested_action, confidence = get_suggested_action(score, trend, rsi_value, volume_status)
        ranked_opportunities.append(
            {
                "rank": rank_position,
                "display_name": display_name,
                "coin_id": coin_id,
                "score": score,
                "trend": trend,
                "rating": rating,
                "suggested_action": suggested_action,
                "confidence": confidence,
                "volume_status": volume_status,
                "rsi_14": rsi_value,
                "entry": entry,
            }
        )

    return ranked_opportunities


def _get_opportunity_context(
    data: List[dict],
    display_name: str,
    coin_id: str,
    score: int,
    rank_position: int,
) -> dict | None:
    """Return full derived context for a specific ranked opportunity."""
    entry = next((entry for entry in data if entry.get("id") == coin_id), None)
    if entry is None:
        return None

    trend = get_trend(entry)
    rating = get_rating(score, trend)
    rsi_value = entry.get("rsi_14")
    volume_status = get_volume_status(entry)
    suggested_action, confidence = get_suggested_action(score, trend, rsi_value, volume_status)
    support_level = entry.get("support_level")
    resistance_level = entry.get("resistance_level")
    support_resistance_status = entry.get("support_resistance_status", "Between Levels")
    support_resistance_explanation = entry.get(
        "support_resistance_explanation",
        "Price is trading between support and resistance, so the next directional move is still being set up.",
    )
    candlestick_pattern = entry.get("candlestick_pattern", "None Detected")
    candlestick_pattern_explanation = entry.get("candlestick_pattern_explanation", "")
    multi_timeframe = entry.get("multi_timeframe") or {}
    timeframe_data = multi_timeframe.get("timeframes") or {}
    timeframe_15m = timeframe_data.get("15m") or {"trend": "Sideways", "change_percent": 0.0}
    timeframe_1h = timeframe_data.get("1h") or {"trend": "Sideways", "change_percent": 0.0}
    timeframe_4h = timeframe_data.get("4h") or {"trend": "Sideways", "change_percent": 0.0}
    mtf_trend = multi_timeframe.get("composite_trend", "Sideways")
    mtf_score = multi_timeframe.get("composite_score", 50)

    if rank_position == 1:
        explanation = (
            f"{display_name} ranks first because its momentum, volume profile, and market-cap strength "
            f"produce the highest opportunity score ({score}) while the current {trend.lower()} trend "
            f"and {volume_status.lower()} volume support its lead."
        )
    else:
        explanation = (
            f"{display_name} is ranked #{rank_position} with an opportunity score of {score}, "
            f"supported by a {trend.lower()} trend and {volume_status.lower()} volume profile."
        )

    current_price = _get_current_price(entry)

    gbp_rate = get_usd_to_gbp_rate()
    current_price_gbp = current_price * gbp_rate
    support_level = entry.get("support_level")
    resistance_level = entry.get("resistance_level")
    support_level_gbp = float(support_level) * gbp_rate if support_level is not None else None
    resistance_level_gbp = float(resistance_level) * gbp_rate if resistance_level is not None else None
    entry_zone_low = current_price_gbp * 0.97
    entry_zone_high = current_price_gbp * 0.99
    stop_loss = entry_zone_low * 0.97
    take_profit_1 = current_price_gbp * 1.05
    take_profit_2 = current_price_gbp * 1.10

    if confidence >= 80:
        risk_level = "Low"
    elif confidence >= 60:
        risk_level = "Medium"
    else:
        risk_level = "High"

    account_size = 1000.0
    risk_per_trade = 0.01
    max_risk = account_size * risk_per_trade
    entry_zone_midpoint = (entry_zone_low + entry_zone_high) / 2.0
    risk_per_unit = max(entry_zone_midpoint - stop_loss, 0.0)
    suggested_position_size = max_risk / risk_per_unit if risk_per_unit > 0 else 0.0
    risk_reward_ratio = (take_profit_1 - entry_zone_midpoint) / risk_per_unit if risk_per_unit > 0 else 0.0
    estimated_profit_tp1 = suggested_position_size * (take_profit_1 - entry_zone_midpoint)
    estimated_profit_tp2 = suggested_position_size * (take_profit_2 - entry_zone_midpoint)
    max_loss_if_stop_hit = suggested_position_size * risk_per_unit

    return {
        "display_name": display_name,
        "score": score,
        "entry": entry,
        "trend": trend,
        "rating": rating,
        "volume_status": volume_status,
        "suggested_action": suggested_action,
        "confidence": confidence,
        "support_level": support_level_gbp,
        "resistance_level": resistance_level_gbp,
        "support_resistance_status": support_resistance_status,
        "support_resistance_explanation": (
            f"{support_resistance_explanation}"
            + (
                f" Nearest support is £{support_level_gbp:,.2f}"
                if support_level_gbp is not None
                else ""
            )
            + (
                f" and nearest resistance is £{resistance_level_gbp:,.2f}."
                if resistance_level_gbp is not None
                else ""
            )
        ),
        "candlestick_pattern": candlestick_pattern,
        "candlestick_pattern_explanation": candlestick_pattern_explanation,
        "mtf_trend": mtf_trend,
        "mtf_score": mtf_score,
        "timeframe_15m": timeframe_15m,
        "timeframe_1h": timeframe_1h,
        "timeframe_4h": timeframe_4h,
        "explanation": explanation,
        "current_price_gbp": current_price_gbp,
        "entry_zone_low": entry_zone_low,
        "entry_zone_high": entry_zone_high,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_level": risk_level,
        "account_size": account_size,
        "risk_per_trade": risk_per_trade,
        "max_risk": max_risk,
        "suggested_position_size": suggested_position_size,
        "risk_reward_ratio": risk_reward_ratio,
        "estimated_profit_tp1": estimated_profit_tp1,
        "estimated_profit_tp2": estimated_profit_tp2,
        "max_loss_if_stop_hit": max_loss_if_stop_hit,
    }


def _get_top_opportunity_context(data: List[dict]) -> dict | None:
    """Return the derived metrics for the highest-ranked opportunity."""
    ranked_opportunities = _build_ranked_opportunities_for_trade_decision(data)
    if not ranked_opportunities:
        return None

    top_opportunity = ranked_opportunities[0]
    return _get_opportunity_context(
        data,
        display_name=str(top_opportunity["display_name"]),
        coin_id=str(top_opportunity["coin_id"]),
        score=int(top_opportunity["score"]),
        rank_position=int(top_opportunity["rank"]),
    )


def _build_strategy_scorecard(context: dict) -> tuple[list[str], int, str]:
    """Build a display-only strategy scorecard from existing context signals."""
    criteria_scores: list[tuple[str, int, str]] = []

    trend = str(context.get("trend", "sideways")).lower()
    mtf_trend = str(context.get("mtf_trend", "Sideways")).lower()
    if trend == "bullish" and "bull" in mtf_trend:
        trend_points = 20
        trend_reason = "Bullish trend is aligned across timeframes."
    elif trend == "bullish" and "sideways" in mtf_trend:
        trend_points = 8
        trend_reason = "Bullish trend is present, but higher-timeframe alignment is only neutral."
    elif trend == "bearish" and "bear" in mtf_trend:
        trend_points = -20
        trend_reason = "Bearish trend is aligned across timeframes."
    elif trend == "bearish":
        trend_points = -8
        trend_reason = "Trend is bearish, limiting long-side conviction."
    elif "bull" in mtf_trend:
        trend_points = 5
        trend_reason = "Short-term action is sideways, but higher timeframes lean bullish."
    elif "bear" in mtf_trend:
        trend_points = -5
        trend_reason = "Short-term action is sideways with bearish higher-timeframe pressure."
    else:
        trend_points = 0
        trend_reason = "Trend is mixed and not strongly directional."
    criteria_scores.append(("Trend Alignment", trend_points, trend_reason))

    rsi_value = context.get("entry", {}).get("rsi_14")
    rsi_status = get_rsi_status(float(rsi_value)) if rsi_value is not None else "Neutral"
    if rsi_status == "Oversold":
        rsi_points = 12
        rsi_reason = "Oversold RSI can support a rebound setup."
    elif rsi_status == "Overbought":
        rsi_points = -15
        rsi_reason = "Overbought RSI increases pullback risk."
    else:
        rsi_float = float(rsi_value) if rsi_value is not None else 50.0
        if 45 <= rsi_float <= 65:
            rsi_points = 15
            rsi_reason = "Neutral RSI in a healthy continuation zone."
        else:
            rsi_points = 6
            rsi_reason = "RSI is neutral but not in the strongest continuation range."
    criteria_scores.append(("RSI Status", rsi_points, rsi_reason))

    volume_status = str(context.get("volume_status", "Normal"))
    if volume_status == "High":
        volume_points = 15
        volume_reason = "High volume confirms participation behind the move."
    elif volume_status == "Low":
        volume_points = -15
        volume_reason = "Low volume weakens confirmation for follow-through."
    else:
        volume_points = 6
        volume_reason = "Normal volume supports the setup without strong confirmation."
    criteria_scores.append(("Volume Confirmation", volume_points, volume_reason))

    support_resistance_status = str(context.get("support_resistance_status", "Between Levels"))
    if support_resistance_status == "Breaking Resistance":
        sr_points = 15
        sr_reason = "Price is breaking resistance, supporting upside continuation."
    elif support_resistance_status == "Near Support":
        sr_points = 10
        sr_reason = "Price is near support, improving entry risk control."
    elif support_resistance_status == "Near Resistance":
        sr_points = -10
        sr_reason = "Price is near resistance, where upside can stall."
    elif support_resistance_status == "Breaking Support":
        sr_points = -15
        sr_reason = "Price is breaking support, increasing downside risk."
    else:
        sr_points = 0
        sr_reason = "Price is between levels with no strong level edge."
    criteria_scores.append(("Support/Resistance Position", sr_points, sr_reason))

    risk_reward_ratio = float(context.get("risk_reward_ratio") or 0.0)
    if risk_reward_ratio >= 2.0:
        rr_points = 20
        rr_reason = "Risk/reward is strong relative to downside risk."
    elif risk_reward_ratio >= 1.5:
        rr_points = 14
        rr_reason = "Risk/reward is favorable."
    elif risk_reward_ratio >= 1.0:
        rr_points = 8
        rr_reason = "Risk/reward is acceptable but not exceptional."
    elif risk_reward_ratio >= 0.7:
        rr_points = -6
        rr_reason = "Risk/reward is below ideal thresholds."
    else:
        rr_points = -15
        rr_reason = "Risk/reward is weak for this setup."
    criteria_scores.append(("Risk/Reward Quality", rr_points, rr_reason))

    mtf_score = float(context.get("mtf_score") or 50.0)
    if mtf_score >= 75:
        momentum_points = 15
        momentum_reason = "Momentum is strong across multiple timeframes."
    elif mtf_score >= 60:
        momentum_points = 8
        momentum_reason = "Momentum is constructive but not dominant."
    elif mtf_score >= 45:
        momentum_points = 2
        momentum_reason = "Momentum is neutral."
    elif mtf_score >= 30:
        momentum_points = -8
        momentum_reason = "Momentum is weakening."
    else:
        momentum_points = -15
        momentum_reason = "Momentum is weak across timeframes."
    criteria_scores.append(("Momentum", momentum_points, momentum_reason))

    candlestick_pattern = str(context.get("candlestick_pattern", "None Detected"))
    support_resistance_status = str(context.get("support_resistance_status", "Between Levels"))
    if candlestick_pattern == "Bullish Engulfing":
        pattern_points = 10
        pattern_reason = "Bullish engulfing adds upside confirmation to the setup."
        pattern_influence = "The bullish engulfing pattern increased conviction in the recommendation."
    elif candlestick_pattern == "Bearish Engulfing":
        pattern_points = -10
        pattern_reason = "Bearish engulfing warns that downside pressure may be building."
        pattern_influence = "The bearish engulfing pattern reduced conviction in the recommendation."
    elif candlestick_pattern == "Hammer" and support_resistance_status == "Near Support":
        pattern_points = 8
        pattern_reason = "Hammer near support suggests buyers are defending a key level."
        pattern_influence = "The hammer near support added a bullish boost to the recommendation."
    elif candlestick_pattern == "Hammer":
        pattern_points = 0
        pattern_reason = "Hammer is present but not near support, so no score boost is applied."
        pattern_influence = "The hammer was informational only because it was not near support."
    else:
        pattern_points = 0
        pattern_reason = "No recognized candlestick pattern was detected."
        pattern_influence = "No candlestick pattern adjustment was applied to the recommendation."
    criteria_scores.append(("Candlestick Pattern", pattern_points, pattern_reason))

    contribution_total = sum(points for _, points, _ in criteria_scores)
    strategy_score = max(0, min(100, 50 + contribution_total))

    strongest_positive = max(criteria_scores, key=lambda item: item[1])
    strongest_negative = min(criteria_scores, key=lambda item: item[1])
    recommendation_rationale = (
        f"{context.get('suggested_action')} is selected because {strongest_positive[0].lower()} "
        f"is the strongest tailwind ({strongest_positive[1]:+d}), while {strongest_negative[0].lower()} "
        f"is the main drag ({strongest_negative[1]:+d}). {pattern_influence}"
    )

    scorecard_lines = ["Strategy Scorecard", "-----------------"]
    for label, points, reason in criteria_scores:
        scorecard_lines.append(f"{label}: {points:+d} pts | {reason}")
    scorecard_lines.append(f"Total Strategy Score: {strategy_score}/100")

    return scorecard_lines, strategy_score, recommendation_rationale


def load_trade_journal_rows(journal_path: str = TRADE_JOURNAL_FILE) -> List[dict]:
    """Load trade journal rows from CSV, returning an empty list when unavailable."""
    if not os.path.exists(journal_path) or os.path.getsize(journal_path) == 0:
        return []

    with open(journal_path, "r", newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def _write_trade_journal_rows(rows: List[dict], journal_path: str = TRADE_JOURNAL_FILE) -> None:
    """Rewrite the trade journal with the provided rows."""
    with open(journal_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=TRADE_JOURNAL_HEADERS)
        writer.writeheader()
        for row in rows:
            normalized_row = {header: row.get(header, "") for header in TRADE_JOURNAL_HEADERS}
            writer.writerow(normalized_row)


def _parse_trade_journal_currency(value: object) -> float:
    """Parse currency strings like '£1,234.56' or '-£120.00' into floats."""
    if value is None:
        return 0.0

    raw = str(value).strip()
    if not raw:
        return 0.0

    negative_by_parentheses = raw.startswith("(") and raw.endswith(")")
    if negative_by_parentheses:
        raw = raw[1:-1]

    normalized = raw.replace("£", "").replace(",", "").replace(" ", "")
    try:
        parsed_value = float(normalized)
    except ValueError:
        return 0.0

    if negative_by_parentheses:
        return -abs(parsed_value)
    return parsed_value


def _parse_trade_journal_percent(value: object) -> float:
    """Parse percent strings like '2.75%' into floats."""
    if value is None:
        return 0.0

    raw = str(value).strip()
    if not raw:
        return 0.0

    normalized = raw.replace("%", "").replace(",", "").replace(" ", "")
    try:
        return float(normalized)
    except ValueError:
        return 0.0


def _parse_trade_journal_units(value: object) -> float:
    """Parse stored position sizes into float units."""
    if value is None:
        return 0.0

    raw = str(value).strip()
    if not raw:
        return 0.0

    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


def _parse_trade_journal_timestamp(value: object) -> datetime | None:
    """Parse stored journal timestamps using the scanner's standard format."""
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _format_trade_duration(started_at: datetime | None, ended_at: datetime | None) -> str:
    """Return a compact human-readable trade duration."""
    if started_at is None or ended_at is None or ended_at < started_at:
        return ""

    total_seconds = int((ended_at - started_at).total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _find_market_entry_by_coin_name(data: List[dict], coin_name: str) -> dict | None:
    """Return the current market snapshot entry for a journal coin name."""
    for display_name, coin_id in COINS:
        if display_name != coin_name:
            continue
        return next((entry for entry in data if entry.get("id") == coin_id), None)
    return None


def _get_entry_price_from_trade_row(row: dict) -> float:
    """Return the stored entry price for a journal row."""
    return _parse_trade_journal_currency(row.get("Entry Price") or row.get("Current Price"))


def _get_trade_cost_basis(row: dict) -> float:
    """Return the capital committed at entry for a journal row."""
    return _get_entry_price_from_trade_row(row) * _parse_trade_journal_units(row.get("Position Size"))


def _validate_paper_starting_balance(starting_balance: float) -> float:
    """Validate and normalize the configured paper starting balance."""
    normalized_balance = float(starting_balance)
    if normalized_balance < 0:
        raise ValueError("starting_balance must be zero or greater")
    return normalized_balance


def _validate_paper_position_size_pct(position_size_pct: float) -> float:
    """Validate and normalize the configured per-trade allocation percentage."""
    normalized_pct = float(position_size_pct)
    if normalized_pct <= 0 or normalized_pct > 1:
        raise ValueError("position_size_pct must be greater than 0 and at most 1")
    return normalized_pct


def calculate_position_allocation(
    available_cash: float,
    position_size_pct: float = DEFAULT_PAPER_POSITION_SIZE_PCT,
) -> float:
    """Return the amount of available cash to allocate to the next paper trade."""
    normalized_available_cash = max(0.0, float(available_cash))
    normalized_pct = _validate_paper_position_size_pct(position_size_pct)
    return normalized_available_cash * normalized_pct


def calculate_portfolio_snapshot(
    data: List[dict] | None = None,
    trade_rows: List[dict] | None = None,
    journal_path: str = TRADE_JOURNAL_FILE,
    starting_balance: float = DEFAULT_PAPER_STARTING_BALANCE,
) -> dict:
    """Build a reusable virtual-portfolio snapshot from journal rows and current prices."""
    normalized_starting_balance = _validate_paper_starting_balance(starting_balance)
    market_data = data or []
    rows = load_trade_journal_rows(journal_path) if trade_rows is None else trade_rows
    gbp_rate = get_usd_to_gbp_rate()

    available_cash = normalized_starting_balance
    invested_capital = 0.0
    current_portfolio_value = 0.0
    realized_profit_loss = 0.0
    open_trade_count = 0
    closed_trade_count = 0

    for row in rows:
        position_units = _parse_trade_journal_units(row.get("Position Size"))
        if position_units <= 0:
            continue

        entry_price = _get_entry_price_from_trade_row(row)
        invested_amount = entry_price * position_units
        status = str(row.get("Trade Status") or "").strip()

        if _trade_status_is_open(status):
            open_trade_count += 1
            available_cash -= invested_amount
            invested_capital += invested_amount

            market_entry = _find_market_entry_by_coin_name(market_data, row.get("Coin", ""))
            current_price = entry_price
            if market_entry is not None:
                current_price = _get_current_price(market_entry) * gbp_rate
            current_portfolio_value += current_price * position_units
        elif status == TRADE_STATUS_CLOSED:
            closed_trade_count += 1
            exit_price = _parse_trade_journal_currency(row.get("Exit Price"))
            available_cash -= invested_amount
            available_cash += exit_price * position_units
            realized_profit_loss += (exit_price - entry_price) * position_units

    unrealized_profit_loss = current_portfolio_value - invested_capital
    total_equity = available_cash + current_portfolio_value

    return {
        "starting_balance": normalized_starting_balance,
        "available_cash": available_cash,
        "invested_capital": invested_capital,
        "current_portfolio_value": current_portfolio_value,
        "total_equity": total_equity,
        "realized_profit_loss": realized_profit_loss,
        "unrealized_profit_loss": unrealized_profit_loss,
        "open_trade_count": open_trade_count,
        "closed_trade_count": closed_trade_count,
    }


def _build_trade_journal_row(
    context: dict,
    strategy_score: int,
    recommendation_rationale: str,
    timestamp_value: datetime,
    position_size_units: float,
) -> dict:
    """Build the persisted journal row for a newly opened paper trade."""
    rsi_value = format_rsi(context["entry"])
    current_price_gbp = context["current_price_gbp"]
    entry_zone = f"£{context['entry_zone_low']:,.2f} - £{context['entry_zone_high']:,.2f}"

    return {
        "Date/Time": timestamp_value.strftime("%Y-%m-%d %H:%M:%S"),
        "Coin": context["display_name"],
        "Opportunity Score": str(context["score"]),
        "Strategy Score": str(strategy_score),
        "Suggested Action": context["suggested_action"],
        "Confidence": f"{context['confidence']}%",
        "Current Price": f"£{current_price_gbp:,.2f}",
        "Entry Price": f"£{current_price_gbp:,.2f}",
        "Entry Zone": entry_zone,
        "Stop Loss": f"£{context['stop_loss']:,.2f}",
        "Take Profit 1": f"£{context['take_profit_1']:,.2f}",
        "Take Profit 2": f"£{context['take_profit_2']:,.2f}",
        "Risk/Reward Ratio": f"{context['risk_reward_ratio']:.2f}",
        "Position Size": f"{position_size_units:.8f}",
        "Risk Level": context["risk_level"],
        "Trend": context["trend"],
        "RSI": rsi_value,
        "Volume": context["volume_status"],
        "Recommendation Rationale": recommendation_rationale,
        "Trade Status": TRADE_STATUS_OPEN,
        "Exit Price": "",
        "Exit Time": "",
        "Exit Reason": EXIT_REASON_DEFAULT,
        "Profit/Loss (£)": "£0.00",
        "Profit/Loss (%)": "0.00%",
        "Trade Duration": "",
        "Notes": "",
    }


def _dedupe_trade_journal_row(row: dict) -> tuple:
    """Build an in-memory dedupe key for a pending paper trade row."""
    return (
        row["Coin"],
        row["Opportunity Score"],
        row["Strategy Score"],
        row["Suggested Action"],
        row["Confidence"],
        row["Current Price"],
        row["Entry Price"],
        row["Entry Zone"],
        row["Stop Loss"],
        row["Take Profit 1"],
        row["Take Profit 2"],
        row["Risk/Reward Ratio"],
        row["Position Size"],
        row["Risk Level"],
        row["Trend"],
        row["RSI"],
        row["Volume"],
        row["Recommendation Rationale"],
        row["Trade Status"],
        row["Exit Price"],
        row["Exit Time"],
        row["Exit Reason"],
        row["Profit/Loss (£)"],
        row["Profit/Loss (%)"],
        row["Trade Duration"],
        row["Notes"],
    )


def _trade_status_is_open(value: object) -> bool:
    """Return whether a stored trade status still represents an open paper trade."""
    return str(value or "").strip() in {TRADE_STATUS_DEFAULT, TRADE_STATUS_OPEN}


def _has_open_trade_for_coin(trade_rows: List[dict], coin_name: str) -> bool:
    """Return whether the specified coin already has an open paper trade."""
    return any(row.get("Coin") == coin_name and _trade_status_is_open(row.get("Trade Status")) for row in trade_rows)


def _evaluate_trade_exit(current_price_gbp: float, stop_loss: float, take_profit_1: float, take_profit_2: float) -> tuple[float, str] | None:
    """Return the exit price and reason when an open trade should be closed."""
    if current_price_gbp <= stop_loss:
        return stop_loss, EXIT_REASON_STOP_LOSS
    if current_price_gbp >= take_profit_2:
        return take_profit_2, EXIT_REASON_TAKE_PROFIT_2
    if current_price_gbp >= take_profit_1:
        return take_profit_1, EXIT_REASON_TAKE_PROFIT_1
    return None


def update_open_paper_trades(
    data: List[dict],
    journal_path: str = TRADE_JOURNAL_FILE,
    timestamp: datetime | None = None,
) -> int:
    """Close open paper trades whose stop-loss or take-profit has been reached."""
    trade_rows = load_trade_journal_rows(journal_path)
    if not trade_rows:
        return 0

    timestamp_value = timestamp or datetime.now()
    gbp_rate = get_usd_to_gbp_rate()
    closed_trade_count = 0
    rows_changed = False

    for row in trade_rows:
        if not _trade_status_is_open(row.get("Trade Status")):
            continue

        market_entry = _find_market_entry_by_coin_name(data, row.get("Coin", ""))
        if market_entry is None:
            continue

        current_price_gbp = _get_current_price(market_entry) * gbp_rate
        stop_loss = _parse_trade_journal_currency(row.get("Stop Loss"))
        take_profit_1 = _parse_trade_journal_currency(row.get("Take Profit 1"))
        take_profit_2 = _parse_trade_journal_currency(row.get("Take Profit 2"))
        entry_price = _get_entry_price_from_trade_row(row)
        position_size = _parse_trade_journal_units(row.get("Position Size"))

        exit_result = _evaluate_trade_exit(current_price_gbp, stop_loss, take_profit_1, take_profit_2)
        if exit_result is None:
            continue

        exit_price, exit_reason = exit_result
        profit_loss_value = position_size * (exit_price - entry_price)
        profit_loss_percent = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
        opened_at = _parse_trade_journal_timestamp(row.get("Date/Time"))

        row["Trade Status"] = TRADE_STATUS_CLOSED
        row["Exit Price"] = f"£{exit_price:,.2f}"
        row["Exit Time"] = timestamp_value.strftime("%Y-%m-%d %H:%M:%S")
        row["Exit Reason"] = exit_reason
        row["Profit/Loss (£)"] = f"£{profit_loss_value:,.2f}"
        row["Profit/Loss (%)"] = f"{profit_loss_percent:.2f}%"
        row["Trade Duration"] = _format_trade_duration(opened_at, timestamp_value)
        closed_trade_count += 1
        rows_changed = True

    if rows_changed:
        _ensure_trade_journal_schema(journal_path)
        _write_trade_journal_rows(trade_rows, journal_path=journal_path)

    return closed_trade_count


def _extract_trade_profit_values(trade_rows: List[dict]) -> List[float]:
    """Return normalized Profit/Loss (£) values from journal rows."""
    return [_parse_trade_journal_currency(row.get("Profit/Loss (£)")) for row in trade_rows]


def _extract_trade_return_values(trade_rows: List[dict]) -> List[float]:
    """Return normalized Profit/Loss (%) values from journal rows."""
    return [_parse_trade_journal_percent(row.get("Profit/Loss (%)")) for row in trade_rows]


def calculate_total_trades(trade_rows: List[dict]) -> int:
    """Return the total number of journaled trades."""
    return len(trade_rows)


def calculate_winning_trades(trade_rows: List[dict]) -> int:
    """Return the number of trades with positive Profit/Loss (£)."""
    profits = _extract_trade_profit_values(trade_rows)
    return sum(1 for value in profits if value > 0)


def calculate_losing_trades(trade_rows: List[dict]) -> int:
    """Return the number of trades with negative Profit/Loss (£)."""
    profits = _extract_trade_profit_values(trade_rows)
    return sum(1 for value in profits if value < 0)


def calculate_win_rate(trade_rows: List[dict]) -> float:
    """Return win rate as a percentage using only winning and losing trades."""
    winning_trades = calculate_winning_trades(trade_rows)
    losing_trades = calculate_losing_trades(trade_rows)
    closed_trades = winning_trades + losing_trades
    if closed_trades == 0:
        return 0.0
    return (winning_trades / closed_trades) * 100


def calculate_average_return(trade_rows: List[dict]) -> float:
    """Return average Profit/Loss (%) across all journal rows."""
    returns = _extract_trade_return_values(trade_rows)
    if not returns:
        return 0.0
    return sum(returns) / len(returns)


def calculate_best_trade(trade_rows: List[dict]) -> float:
    """Return the best single-trade Profit/Loss (£)."""
    profits = _extract_trade_profit_values(trade_rows)
    if not profits:
        return 0.0
    return max(profits)


def calculate_worst_trade(trade_rows: List[dict]) -> float:
    """Return the worst single-trade Profit/Loss (£)."""
    profits = _extract_trade_profit_values(trade_rows)
    if not profits:
        return 0.0
    return min(profits)


def calculate_cumulative_profit_loss(trade_rows: List[dict]) -> float:
    """Return the cumulative Profit/Loss (£) over all journal rows."""
    profits = _extract_trade_profit_values(trade_rows)
    return sum(profits)


def calculate_profit_factor(trade_rows: List[dict]) -> float:
    """Return profit factor = gross profit / absolute gross loss."""
    profits = _extract_trade_profit_values(trade_rows)
    gross_profit = sum(value for value in profits if value > 0)
    gross_loss = sum(value for value in profits if value < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / abs(gross_loss)


def calculate_performance_statistics(trade_rows: List[dict]) -> dict:
    """Build a complete statistics snapshot from trade journal rows."""
    return {
        "total_trades": calculate_total_trades(trade_rows),
        "winning_trades": calculate_winning_trades(trade_rows),
        "losing_trades": calculate_losing_trades(trade_rows),
        "win_rate": calculate_win_rate(trade_rows),
        "average_return": calculate_average_return(trade_rows),
        "best_trade": calculate_best_trade(trade_rows),
        "worst_trade": calculate_worst_trade(trade_rows),
        "cumulative_profit_loss": calculate_cumulative_profit_loss(trade_rows),
        "profit_factor": calculate_profit_factor(trade_rows),
    }


def _ensure_trade_journal_schema(journal_path: str) -> bool:
    """Ensure the journal CSV exists with the latest headers; migrate older headers when needed."""
    if not os.path.exists(journal_path) or os.path.getsize(journal_path) == 0:
        return True

    with open(journal_path, "r", newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        existing_headers = reader.fieldnames or []
        if existing_headers == TRADE_JOURNAL_HEADERS:
            return False

        existing_rows = list(reader)

    with open(journal_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=TRADE_JOURNAL_HEADERS)
        writer.writeheader()
        for existing_row in existing_rows:
            migrated_row = {
                header: existing_row.get(header, "")
                for header in TRADE_JOURNAL_HEADERS
            }
            writer.writerow(migrated_row)

    return True


def record_trade_journal_entry(
    data: List[dict],
    journal_path: str = TRADE_JOURNAL_FILE,
    seen_entries: set[tuple] | None = None,
    timestamp: datetime | None = None,
    starting_balance: float = DEFAULT_PAPER_STARTING_BALANCE,
    position_size_pct: float = DEFAULT_PAPER_POSITION_SIZE_PCT,
    paper_trade_manager: PaperTradeManager | None = None,
) -> bool:
    """Open a paper trade only when the manager approves a ranked opportunity."""
    manager = paper_trade_manager or PaperTradeManager()
    selected_opportunity: dict | None = None
    ranked_opportunities = _build_ranked_opportunities_for_trade_decision(data)
    for opportunity in ranked_opportunities:
        if manager.should_open_trade(opportunity) and selected_opportunity is None:
            selected_opportunity = opportunity

    if selected_opportunity is None:
        return False

    context = _get_opportunity_context(
        data,
        display_name=str(selected_opportunity["display_name"]),
        coin_id=str(selected_opportunity["coin_id"]),
        score=int(selected_opportunity["score"]),
        rank_position=int(selected_opportunity["rank"]),
    )
    if context is None:
        return False

    if context.get("suggested_action") != "BUY":
        return False

    _, strategy_score, recommendation_rationale = _build_strategy_scorecard(context)

    timestamp_value = timestamp or datetime.now()
    file_exists = os.path.exists(journal_path)
    needs_header = not file_exists or os.path.getsize(journal_path) == 0
    trade_rows: List[dict] = []
    if not needs_header:
        _ensure_trade_journal_schema(journal_path)
        trade_rows = load_trade_journal_rows(journal_path)
        if _has_open_trade_for_coin(trade_rows, context["display_name"]):
            return False

    portfolio_snapshot = calculate_portfolio_snapshot(
        data=data,
        trade_rows=trade_rows,
        journal_path=journal_path,
        starting_balance=starting_balance,
    )
    allocation_amount = calculate_position_allocation(
        portfolio_snapshot["available_cash"],
        position_size_pct=position_size_pct,
    )
    entry_price_gbp = float(context["current_price_gbp"])
    if allocation_amount <= 0 or entry_price_gbp <= 0:
        return False

    position_size_units = allocation_amount / entry_price_gbp
    if position_size_units <= 0:
        return False

    row = _build_trade_journal_row(
        context,
        strategy_score,
        recommendation_rationale,
        timestamp_value,
        position_size_units,
    )
    dedupe_key = _dedupe_trade_journal_row(row)

    if seen_entries is not None:
        if dedupe_key in seen_entries:
            return False
        seen_entries.add(dedupe_key)

    with open(journal_path, "a", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=TRADE_JOURNAL_HEADERS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)

    return True


def process_paper_trades(
    data: List[dict],
    journal_path: str = TRADE_JOURNAL_FILE,
    seen_entries: set[tuple] | None = None,
    timestamp: datetime | None = None,
    starting_balance: float = DEFAULT_PAPER_STARTING_BALANCE,
    position_size_pct: float = DEFAULT_PAPER_POSITION_SIZE_PCT,
    paper_trade_manager: PaperTradeManager | None = None,
) -> dict:
    """Advance the paper-trading engine for a scan by closing then opening trades."""
    closed_trades = update_open_paper_trades(data, journal_path=journal_path, timestamp=timestamp)
    opened_trade = record_trade_journal_entry(
        data,
        journal_path=journal_path,
        seen_entries=seen_entries,
        timestamp=timestamp,
        starting_balance=starting_balance,
        position_size_pct=position_size_pct,
        paper_trade_manager=paper_trade_manager,
    )
    trade_rows = load_trade_journal_rows(journal_path)
    return {
        "opened_trade": opened_trade,
        "closed_trades": closed_trades,
        "performance_statistics": calculate_performance_statistics(trade_rows),
        "portfolio_snapshot": calculate_portfolio_snapshot(
            data=data,
            trade_rows=trade_rows,
            journal_path=journal_path,
            starting_balance=starting_balance,
        ),
    }


def build_top_opportunity_analysis(data: List[dict]) -> str:
    """Return a short analysis for the highest-ranked opportunity."""
    context = _get_top_opportunity_context(data)
    if context is None:
        return ""

    strategy_scorecard_lines, strategy_score, recommendation_rationale = _build_strategy_scorecard(context)
    trend_status = get_trend_status_label(context["trend"])
    signal_status = get_rating_status_label(context["rating"])
    volume_status = get_volume_status_label(context["volume_status"])
    level_status = get_level_status_label(context["support_resistance_status"])
    action_status = get_action_status_label(context["suggested_action"])
    candlestick_line = f"Candlestick Pattern: {context['candlestick_pattern']}"
    candlestick_explanation_line = (
        f"Candlestick Insight: {context['candlestick_pattern_explanation']}"
        if context["candlestick_pattern"] != "None Detected" and context["candlestick_pattern_explanation"]
        else None
    )

    analysis_lines = [
        "[bold cyan]🔵 INFO | ATLAS ONE ANALYSIS[/bold cyan]",
        f"Coin: {context['display_name']}",
        f"Opportunity Score: {context['score']}",
        f"Trend: {context['trend']} ({trend_status})",
        f"Signal: {context['rating']} ({signal_status})",
        f"RSI: {format_rsi(context['entry'])}",
        f"Nearest Support: {format_gbp_currency(context['support_level'])}",
        f"Nearest Resistance: {format_gbp_currency(context['resistance_level'])}",
        f"Price vs Levels: {context['support_resistance_status']} ({level_status})",
        candlestick_line,
        f"Multi-timeframe: {context['mtf_trend']} ({context['mtf_score']}/100)",
        (
            "Timeframes: "
            f"15m {context['timeframe_15m'].get('trend')} ({context['timeframe_15m'].get('change_percent', 0.0):+.2f}%), "
            f"1h {context['timeframe_1h'].get('trend')} ({context['timeframe_1h'].get('change_percent', 0.0):+.2f}%), "
            f"4h {context['timeframe_4h'].get('trend')} ({context['timeframe_4h'].get('change_percent', 0.0):+.2f}%)"
        ),
        f"Volume: {context['volume_status']} ({volume_status})",
        f"Why support/resistance matters: {context['support_resistance_explanation']}",
        f"Why it is ranked first: {context['explanation']}",
        f"Suggested Action: {context['suggested_action']} ({action_status})",
        f"Confidence: {context['confidence']}%",
        "",
        *strategy_scorecard_lines,
        f"Recommendation Rationale: {recommendation_rationale}",
        f"Recommendation Basis: Opportunity score remains {context['score']}; strategy scorecard reads {strategy_score}/100.",
    ]
    if candlestick_explanation_line is not None:
        analysis_lines.insert(10, candlestick_explanation_line)

    return "\n".join(analysis_lines)


def build_trade_plan(data: List[dict]) -> str:
    """Return the trade plan for the highest-ranked opportunity."""
    context = _get_top_opportunity_context(data)
    if context is None:
        return ""

    return "\n".join(
        [
            "🔵 INFO | Trade Plan",
            "----------",
            f"Current Price: £{context['current_price_gbp']:,.2f}",
            f"Entry Zone: £{context['entry_zone_low']:,.2f} - £{context['entry_zone_high']:,.2f}",
            f"Stop Loss: £{context['stop_loss']:,.2f}",
            f"Take Profit 1: £{context['take_profit_1']:,.2f}",
            f"Take Profit 2: £{context['take_profit_2']:,.2f}",
            f"Action Bias: {context['suggested_action']} ({get_action_status_label(context['suggested_action'])})",
            f"Risk Level: {context['risk_level']}",
        ]
    )


def build_position_size_calculator(data: List[dict]) -> str:
    """Return the position size calculator for the highest-ranked opportunity."""
    context = _get_top_opportunity_context(data)
    if context is None:
        return ""

    return "\n".join(
        [
            "🔵 INFO | Position Size Calculator",
            "------------------------",
            f"Account Size: £{context['account_size']:,.2f}",
            f"Risk Per Trade: {context['risk_per_trade'] * 100:.0f}%",
            f"Maximum £ Risk: £{context['max_risk']:,.2f}",
            f"Suggested Position Size: {context['suggested_position_size']:,.2f}",
            f"Risk/Reward Ratio: {context['risk_reward_ratio']:,.2f}",
            f"Estimated Profit at Take Profit 1: £{context['estimated_profit_tp1']:,.2f}",
            f"Estimated Profit at Take Profit 2: £{context['estimated_profit_tp2']:,.2f}",
            f"Maximum Loss if Stop Loss is hit: £{context['max_loss_if_stop_hit']:,.2f}",
        ]
    )


def _format_open_positions_dashboard_table(data: List[dict], trade_rows: List[dict]) -> str:
    """Build a display-only open positions table for the portfolio dashboard."""
    open_rows = [row for row in trade_rows if _trade_status_is_open(row.get("Trade Status"))]
    if not open_rows:
        return "No open paper positions."

    gbp_rate = get_usd_to_gbp_rate()
    display_rows: List[List[str]] = []
    for row in open_rows:
        quantity = _parse_trade_journal_units(row.get("Position Size"))
        if quantity <= 0:
            continue

        coin_name = str(row.get("Coin") or "Unknown")
        entry_price = _get_entry_price_from_trade_row(row)
        market_entry = _find_market_entry_by_coin_name(data, coin_name)
        current_price = entry_price
        if market_entry is not None:
            current_price = _get_current_price(market_entry) * gbp_rate

        unrealized_profit_loss = (current_price - entry_price) * quantity
        display_rows.append(
            [
                coin_name,
                f"{quantity:,.8f}",
                format_gbp_currency(entry_price),
                format_gbp_currency(current_price),
                format_gbp_currency(unrealized_profit_loss),
            ]
        )

    if not display_rows:
        return "No open paper positions."

    headers = ["Coin", "Quantity", "Entry Price", "Current Price", "Unrealised P/L"]
    widths = [len(header) for header in headers]
    for values in display_rows:
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(value))

    def _format_row(values: List[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    divider = "-+-".join("-" * width for width in widths)
    lines = [_format_row(headers), divider]
    lines.extend(_format_row(values) for values in display_rows)
    return "\n".join(lines)


def build_portfolio_dashboard(
    data: List[dict],
    journal_path: str = TRADE_JOURNAL_FILE,
    starting_balance: float = DEFAULT_PAPER_STARTING_BALANCE,
) -> str:
    """Return the virtual portfolio dashboard using persisted paper-trade state."""
    trade_rows = load_trade_journal_rows(journal_path)
    snapshot = calculate_portfolio_snapshot(
        data=data,
        trade_rows=trade_rows,
        journal_path=journal_path,
        starting_balance=starting_balance,
    )
    open_positions_table = _format_open_positions_dashboard_table(data, trade_rows)

    return "\n".join(
        [
            "[bold cyan]🔵 INFO | PORTFOLIO DASHBOARD[/bold cyan]",
            f"Starting Balance: {format_gbp_currency(snapshot['starting_balance'])}",
            f"Available Cash: {format_gbp_currency(snapshot['available_cash'])}",
            f"Invested Capital: {format_gbp_currency(snapshot['invested_capital'])}",
            f"Total Equity: {format_gbp_currency(snapshot['total_equity'])}",
            f"Realised P/L: {format_gbp_currency(snapshot['realized_profit_loss'])}",
            f"Unrealised P/L: {format_gbp_currency(snapshot['unrealized_profit_loss'])}",
            f"Open Positions: {int(snapshot['open_trade_count'])}",
            "",
            "Open Positions",
            "--------------",
            open_positions_table,
        ]
    )


def format_opportunity_score(score: int) -> Text:
    """Return a Rich Text object with green/yellow/red styling for opportunity score."""
    text = Text(str(score))
    if score >= 80:
        text.stylize("green bold")
    elif score >= 50:
        text.stylize("yellow bold")
    else:
        text.stylize("red bold")
    return text


def get_signal(score: int) -> str:
    """Translate the opportunity score into a buy/sell signal for the table."""
    if score >= 80:
        return "BUY"
    if score < 30:
        return "SELL"
    return ""


def get_rating(score: int, trend: str) -> str:
    """Translate the opportunity score and trend into a rating label."""
    normalized_trend = trend.lower()

    if normalized_trend == "bullish":
        if score >= 80:
            return "Strong Buy"
        if score >= 50:
            return "Buy"
        return "Hold"

    if normalized_trend == "bearish":
        if score <= 25:
            return "Strong Sell"
        if score <= 40:
            return "Sell"
        return "Hold"

    if score >= 70:
        return "Buy"
    if score <= 30:
        return "Sell"
    return "Hold"


def format_rating(rating: str) -> Text:
    """Return a Rich Text object with color styling for rating labels."""
    status_label = get_rating_status_label(rating)
    text = Text(f"{rating}\n{status_label}")
    if rating == "Strong Buy":
        text.stylize("bold green")
    elif rating == "Buy":
        text.stylize("green")
    elif rating == "Hold":
        text.stylize("yellow")
    elif rating == "Sell":
        text.stylize("red")
    elif rating == "Strong Sell":
        text.stylize("bold red")
    return text


def get_trend(entry: dict) -> str:
    """Classify a coin as Bullish, Bearish, or sideways from recent price movement."""
    price_change_24h = float(entry.get("price_change_percentage_24h_in_currency") or 0.0)
    price_change_7d = float(entry.get("price_change_percentage_7d_in_currency") or 0.0)

    if price_change_24h > 0 and price_change_7d > 0:
        return "Bullish"
    if price_change_24h < 0 and price_change_7d < 0:
        return "Bearish"
    return "sideways"


def format_trend(trend: str) -> Text:
    """Return a Rich Text object with green/red/yellow styling for trend labels."""
    text = Text(trend)
    if trend == "Bullish":
        text.stylize("green bold")
    elif trend == "Bearish":
        text.stylize("red bold")
    else:
        text.stylize("yellow bold")
    return text


def build_table(data: List[dict]) -> Table:
    """Create a Rich table from the fetched CoinGecko payload."""
    table = Table(
        header_style="bold cyan",
        expand=True,
        show_lines=True,
    )

    table.add_column("Coin", style="bold white", no_wrap=True)
    table.add_column("Price", justify="right")
    table.add_column("1h", justify="right")
    table.add_column("24h", justify="right")
    table.add_column("7d", justify="right")
    table.add_column("Opportunity", justify="center")
    table.add_column("Trend", justify="center")
    table.add_column("Signal", justify="center")
    table.add_column("Volume", justify="center")
    table.add_column("RSI", justify="center")
    table.add_column("Market Cap", justify="right")
    table.add_column("24h Volume", justify="right")

    for display_name, coin_id, score in rank_opportunity(data):
        entry = next((entry for entry in data if entry.get("id") == coin_id), None)
        if entry is None:
            continue

        trend = get_trend(entry)
        rating = get_rating(score, trend)

        table.add_row(
            display_name,
            f"${entry.get('current_price', 0):,.2f}",
            format_change(entry.get("price_change_percentage_1h_in_currency")),
            format_change(entry.get("price_change_percentage_24h_in_currency")),
            format_change(entry.get("price_change_percentage_7d_in_currency")),
            format_opportunity_score(score),
            format_trend(trend),
            format_rating(rating),
            f"{get_volume_status(entry)}\n{get_volume_status_label(get_volume_status(entry))}",
            format_rsi(entry),
            format_currency(entry.get("market_cap")),
            format_currency(entry.get("total_volume")),
        )

    return table


def print_opportunities_table(console: Console, data: List[dict]) -> None:
    """Render the opportunities table once using the fully analyzed market data."""
    console.print(build_table(data))


def log_request_audit(console: Console, cache: RateLimitedCache) -> None:
    """Print request/caching audit metrics for the current scan."""
    if not DEBUG:
        return

    stats = cache.get_request_stats()

    console.print("[bold cyan]REQUEST AUDIT[/bold cyan]")
    console.print(f"Total API requests made: {stats['total_api_requests_made']}")
    console.print(f"Cached responses reused: {stats['cached_responses_reused']}")
    console.print(f"Duplicate requests avoided: {stats['duplicate_requests_avoided']}")

    scan_counters = stats.get("scan_debug_counters") or {}
    if scan_counters:
        console.print("Scan counters:")
        console.print(f"- total_coins_scanned: {scan_counters.get('total_coins_scanned', 0)}")
        console.print(f"- sparkline_resolutions: {scan_counters.get('sparkline_resolutions', 0)}")
        console.print(f"- memory_cache_hits: {scan_counters.get('memory_cache_hits', 0)}")
        console.print(f"- persistent_cache_hits: {scan_counters.get('persistent_cache_hits', 0)}")
        console.print(f"- history_fetches: {scan_counters.get('history_fetches', 0)}")
        console.print(f"- total_market_chart_requests: {scan_counters.get('total_market_chart_requests', 0)}")

    source_counts = stats.get("source_request_counts") or {}
    if source_counts:
        console.print("Request sources:")
        for source, count in sorted(source_counts.items()):
            console.print(f"- {source}: {count}")

    request_audit_log = stats.get("request_audit_log") or []
    if request_audit_log:
        console.print("Request log:")
        for request_entry in request_audit_log:
            console.print(
                f"- #{request_entry['request_number']} {request_entry['source']} -> {request_entry['endpoint']}"
            )

    endpoint_counts = stats.get("endpoint_request_counts") or {}
    if endpoint_counts:
        for endpoint, count in sorted(endpoint_counts.items()):
            console.print(f"- {endpoint}: {count}")


def summarize_coin_gecko_requests(cache: RateLimitedCache) -> List[dict]:
    """Summarize the CoinGecko requests made during the current scan."""
    stats = cache.get_request_stats()
    request_log = stats.get("request_audit_log") or []
    summary: List[dict] = []

    purpose_map = {
        "scanner.market_data": "Load the scan-wide market snapshot used by the table, indicators, support/resistance and trade plan.",
        "support_resistance.market_chart": "Fetch historical price history for support/resistance swing analysis.",
        "rsi.historical_prices": "Fetch historical prices for RSI calculation.",
        "multi_timeframe.intraday_prices": "Fetch intraday prices for multi-timeframe momentum analysis.",
    }

    in_memory_map = {
        "scanner.market_data": "Yes - reused by the table, RSI, support/resistance, multi-timeframe analysis and trade plan.",
        "support_resistance.market_chart": "No - used only for support/resistance candle history, then cached.",
        "rsi.historical_prices": "No - used only for RSI historical-price enrichment, then cached.",
        "multi_timeframe.intraday_prices": "No - used only for intraday momentum analysis, then cached.",
    }

    for request_entry in request_log:
        source = request_entry.get("source", "unknown")
        if source.startswith("support_resistance.market_chart"):
            purpose_key = "support_resistance.market_chart"
        elif source.startswith("rsi.historical_prices"):
            purpose_key = "rsi.historical_prices"
        elif source.startswith("multi_timeframe.intraday_prices"):
            purpose_key = "multi_timeframe.intraday_prices"
        else:
            purpose_key = source

        summary.append(
            {
                "endpoint": request_entry.get("endpoint", ""),
                "source": source,
                "purpose": purpose_map.get(purpose_key, "Unknown purpose."),
                "already_in_memory": in_memory_map.get(purpose_key, "Unknown"),
            }
        )

    return summary

       
def main() -> None:
    """Run the scanner until interrupted or a test iteration count is reached."""
    logging.basicConfig(level=logging.INFO if DEBUG else logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description="Display live crypto prices with Rich")
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=30,
        help="Seconds between refreshes (default: 30)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Optional number of refreshes before exiting",
    )
    args = parser.parse_args()

    console = Console()
    console.print("[bold green]Fetching market data from CoinGecko...[/bold green]")

    # Initialize the shared cache for both market data and historical prices
    cache = RateLimitedCache()
    latest_market_data: List[dict] = []
    scan_journal_entries: set[tuple] = set()
    
    try:
        # Fetch initial market data and enrich with indicators.
        market_data = fetch_market_data(cache)
        latest_market_data = enrich_market_data_with_indicators(market_data, cache=cache)
    except requests.RequestException as exc:
        console.print(f"[bold red]Could not fetch data: {exc}[/bold red]")
        log_request_audit(console, cache)
        return

    console.print(build_portfolio_dashboard(latest_market_data))
    console.print()
    console.print("[bold magenta]Cryptocurrency Scanner[/bold magenta]")
    console.print("[bold cyan]TOP OPPORTUNITIES[/bold cyan]")
    print_opportunities_table(console, latest_market_data)
    console.print()
    console.print(build_top_opportunity_analysis(latest_market_data))
    try:
        process_paper_trades(latest_market_data, seen_entries=scan_journal_entries)
    except OSError as exc:
        logger.warning("Could not write trade journal entry: %s", exc)
    console.print()
    console.print(build_trade_plan(latest_market_data))
    console.print()
    console.print(build_position_size_calculator(latest_market_data))
    console.print()

    # iterations=0 means "single scan only": generate report and exit without background refreshes.
    if args.iterations == 0:
        log_request_audit(console, cache)
        return

    total_iterations = 1_000_000 if args.iterations is None else args.iterations

    try:
        for iteration in range(total_iterations):
            # Reuse the initial market snapshot on iteration 0 to avoid a duplicate request.
            market_data = get_market_data_for_iteration(
                iteration,
                latest_market_data,
                lambda: fetch_market_data(cache),
                None,
                cache,
            )

            # Skip a redundant indicator pass on iteration 0; data is already enriched.
            if iteration > 0:
                try:
                    # Enrich with indicators (reuses cached historical/intraday prices when available).
                    latest_market_data = enrich_market_data_with_indicators(market_data, cache=cache)
                    process_paper_trades(latest_market_data, seen_entries=scan_journal_entries)
                except requests.RequestException as exc:
                    console.print(f"[bold red]Could not fetch data: {exc}[/bold red]")
                    if not latest_market_data:
                        if args.iterations is not None and iteration + 1 >= args.iterations:
                            break
                        time.sleep(args.refresh_interval)
                        continue

            if args.iterations is not None and iteration + 1 >= args.iterations:
                break

            time.sleep(args.refresh_interval)
    finally:
        log_request_audit(console, cache)


if __name__ == "__main__":
    main()
