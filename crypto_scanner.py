#!/usr/bin/env python3
"""Live cryptocurrency scanner powered by the free CoinGecko API."""

import argparse
import time
from typing import List, Tuple

import requests
from rich.console import Console
from rich.live import Live
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


def fetch_market_data() -> List[dict]:
    """Fetch the latest market data for the selected coins from CoinGecko."""
    params = {
        "vs_currency": "usd",
        "ids": ",".join(coin_id for _, coin_id in COINS),
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "sparkline": False,
        "price_change_percentage": "1h,24h,7d",
    }

    response = requests.get(API_URL, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def format_currency(value: float | None) -> str:
    """Format large currency values with commas and a dollar sign."""
    if value is None:
        return "N/A"
    return f"${value:,.0f}"


def format_change(value: float | None) -> Text:
    """Return a Rich Text object with green/red styling for percentage changes."""
    if value is None:
        value = 0.0

    text = Text(f"{value:+.2f}%")
    text.stylize("green" if value >= 0 else "red")
    return text


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

    normalized_24h = price_change_24h / max_positive_24h if max_positive_24h else 0.0
    normalized_7d = price_change_7d / max_positive_7d if max_positive_7d else 0.0
    normalized_volume = total_volume / max_volume if max_volume else 0.0
    normalized_market_cap = market_cap / max_market_cap if max_market_cap else 0.0

    score = (
        0.35 * normalized_24h
        + 0.35 * normalized_7d
        + 0.15 * normalized_volume
        + 0.15 * normalized_market_cap
    ) * 100
    return min(100, max(0, round(score)))


def rank_opportunity(data: List[dict]) -> List[Tuple[str, str, int]]:
    """Return the tracked coins sorted by opportunity score descending."""
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

    ranked.sort(key=lambda item: item[2], reverse=True)
    return ranked


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


def format_signal(signal: str) -> Text:
    """Return a Rich Text object with green or red styling for BUY/SELL signals."""
    text = Text(signal)
    if signal == "BUY":
        text.stylize("green bold")
    elif signal == "SELL":
        text.stylize("red bold")
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
        title="Cryptocurrency Scanner",
        header_style="bold cyan",
        title_style="bold magenta",
        expand=True,
    )

    table.add_column("Coin", style="bold white", no_wrap=True)
    table.add_column("Price", justify="right")
    table.add_column("1h", justify="right")
    table.add_column("24h", justify="right")
    table.add_column("7d", justify="right")
    table.add_column("Opportunity", justify="center")
    table.add_column("Trend", justify="center")
    table.add_column("Buy Signal", justify="center")
    table.add_column("Sell Signal", justify="center")
    table.add_column("Market Cap", justify="right")
    table.add_column("Volume", justify="right")

    for display_name, coin_id, score in rank_opportunity(data):
        entry = next((entry for entry in data if entry.get("id") == coin_id), None)
        if entry is None:
            continue

        buy_signal = format_signal(get_signal(score)) if score >= 80 else format_signal("")
        sell_signal = format_signal(get_signal(score)) if score < 30 else format_signal("")

        table.add_row(
            display_name,
            f"${entry.get('current_price', 0):,.2f}",
            format_change(entry.get("price_change_percentage_1h_in_currency")),
            format_change(entry.get("price_change_percentage_24h_in_currency")),
            format_change(entry.get("price_change_percentage_7d_in_currency")),
            format_opportunity_score(score),
            format_trend(get_trend(entry)),
            buy_signal,
            sell_signal,
            format_currency(entry.get("market_cap")),
            format_currency(entry.get("total_volume")),
        )

    return table


def main() -> None:
    """Run the scanner until interrupted or a test iteration count is reached."""
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

    with Live(console=console, auto_refresh=False, screen=False) as live:
        for iteration in range(args.iterations or 1_000_000):
            try:
                market_data = fetch_market_data()
            except requests.RequestException as exc:
                console.print(f"[bold red]Could not fetch data: {exc}[/bold red]")
                time.sleep(args.refresh_interval)
                continue

            table = build_table(market_data)
            live.update(table, refresh=True)

            if args.iterations is not None and iteration + 1 >= args.iterations:
                break

            time.sleep(args.refresh_interval)


if __name__ == "__main__":
    main()
