# crypto-scanner

A terminal-based cryptocurrency scanner that uses the free CoinGecko API to display a colourful Rich table for popular coins.

## Requirements

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

```bash
python crypto_scanner.py
```

Optional arguments:

```bash
python crypto_scanner.py --refresh-interval 30 --iterations 5
```

The script refreshes the table every 30 seconds by default and shows:
- Bitcoin, Ethereum, XRP, Solana, BNB, Cardano, Dogecoin, Avalanche, Chainlink and Polkadot
- Current price
- 1-hour, 24-hour and 7-day percentage changes
- Market cap and volume
