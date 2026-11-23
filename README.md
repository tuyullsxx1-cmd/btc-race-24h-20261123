# BTC Race Bot — Race a BTC trading bot live for 24 hours

[![make.fun](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/tuyullsxx1-cmd/btc-race-24h-20261123/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/tuyullsxx1-cmd/btc-race-24h-20261123/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

Entry for the make.fun bounty [Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261123-0511) — $50 total, 10 places paid (1st $12).

## What it does

A long-only trading bot for Coinbase Wrapped BTC (cbBTC) vs USDC on 1-minute
on-chain prices, with $1,000 of paper money. It trades minute by minute for
24 hours (1,440 bars) and aims for the highest net PnL. No real money is
traded — this is a simulation against real on-chain prices.

## Strategy — top 1-3 pattern from the 18 Nov race

The top 3 of the previous race all did the same thing: trade little, buy an
early dip, then hold. This bot copies that pattern:

- **Indicators:** EMA 8 / EMA 21 on 1-minute closes; no trading in the first
  25 bars while the EMAs form.
- **First buy, inside the first 240 bars (~4 h):** on the first confirmed dip
  (dip ≥ 0.4% below the 24-bar high **and** rebound ≥ 0.15% above the 24-bar
  low) or on a momentum cross-up (EMA8 crosses above EMA21). All-in, 99.9%
  of cash.
- **Hold:** no small take-profit. Sell **only** on stop loss −2.5%, on an EMA
  cross-down while down ≥ 1%, or in the last 10 bars if up ≥ 2%.
- **Re-buy:** only on a deep dip (≥ 0.8% below the 24-bar high **and**
  rebound ≥ 0.3% above the 24-bar low).
- **Cap:** at most 9 trades over the whole race.

A round trip costs ~26 bps (4 bps fee + 2 bps slippage + price impact), so
every trade has to chase a move bigger than that.

## How it runs

- Protocol `agent-fun/bars-replay/v1`: NDJSON over stdin/stdout, one `hello`,
  one batch per minute answered with exactly one reply echoing its `seq`,
  then `done`.
- Python 3.13, standard library only. No network, no wall clock, no files
  outside the repository. Fully deterministic: two runs on the same data
  produce the same orders byte for byte.
- Run: `python3 -P -s main.py`

## Files

- `main.py` — the bot.
- `LICENSE` — MIT.
