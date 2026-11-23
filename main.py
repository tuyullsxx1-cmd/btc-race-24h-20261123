#!/usr/bin/env python3
"""BTC race bot — long-only cbBTC/USDC dip-buy and hold.

Strategy (top 1-3 pattern from the 18 Nov race):
  * EMA 8 / EMA 21 on 1-minute closes; wait 25 bars for the EMAs to form.
  * FIRST BUY inside the first 240 bars (~4 h): on the first confirmed dip
    (dip >= 0.4% below the 24-bar high AND rebound >= 0.15% above the
    24-bar low) or on a momentum cross-up (EMA8 crosses above EMA21).
    All-in, 99.9% of cash.
  * HOLD: no small take-profit. Sell ONLY on stop loss -2.5%, on an EMA
    cross-down while down >= 1%, or in the last 10 bars if up >= 2%.
  * RE-BUY only on a deep dip (>= 0.8% below the 24-bar high AND
    rebound >= 0.3% above the 24-bar low).
  * Cap: at most 9 trades (buys) over the whole race.

Protocol: agent-fun/bars-replay/v1, NDJSON over stdin/stdout. One `hello`
at the start (no reply), one batch per minute (one price bar) answered
with exactly one reply echoing its seq, then `done` and exit.

Deterministic: standard library only, no randomness, no wall clock, no
network, no file or environment reads. The same price series always
produces the same orders, byte for byte.
"""

import json
import sys

# --- strategy parameters ---
EMA_FAST = 8
EMA_SLOW = 21
WARMUP_BARS = 25
BUY_WINDOW_BARS = 240
WINDOW_BARS = 24
DIP_BUY = 0.004          # 0.4% below the 24-bar high
REBOUND_BUY = 0.0015     # 0.15% above the 24-bar low
DIP_REBUY = 0.008        # 0.8% below the 24-bar high
REBOUND_REBUY = 0.003    # 0.3% above the 24-bar low
STOP_LOSS = -0.025       # sell at -2.5%
TREND_BREAK_LOSS = -0.01  # cross-down exit needs a loss of >= 1%
FINAL_BARS = 10
FINAL_PROFIT = 0.02      # last 10 bars: exit if up >= 2%
MAX_TRADES = 9
ALL_IN = 0.999           # keep 0.1% cash buffer for fill costs
TOTAL_BARS = 1440
NO_BUY_TAIL = 15         # never open a new position in the last 15 bars
MAX_SLIPPAGE_BPS = 50


def ema_alpha(n):
    return 2.0 / (n + 1.0)


class Bot:
    def __init__(self):
        self.bar_index = 0
        self.closes = []
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None
        self.cash = 1000.0
        self.btc = 0.0
        self.entry_price = None
        self.peak_price = None
        self.trades = 0

    @property
    def in_position(self):
        return self.btc > 0.0

    def on_close(self, close):
        """Feed one close; return a list of orders for this bar."""
        self.bar_index += 1
        self.closes.append(close)
        window = self.closes[-WINDOW_BARS:]

        # Update EMAs, keeping the previous values for cross detection.
        self.prev_fast = self.ema_fast
        self.prev_slow = self.ema_slow
        if self.ema_fast is None:
            self.ema_fast = close
            self.ema_slow = close
        else:
            self.ema_fast += ema_alpha(EMA_FAST) * (close - self.ema_fast)
            self.ema_slow += ema_alpha(EMA_SLOW) * (close - self.ema_slow)

        if self.bar_index <= WARMUP_BARS:
            return []

        high24 = max(window)
        low24 = min(window)
        dip = (high24 - close) / high24 if high24 > 0 else 0.0
        rebound = (close - low24) / low24 if low24 > 0 else 0.0
        crossed_up = (
            self.prev_fast is not None
            and self.prev_slow is not None
            and self.prev_fast <= self.prev_slow
            and self.ema_fast > self.ema_slow
        )
        crossed_down = (
            self.prev_fast is not None
            and self.prev_slow is not None
            and self.prev_fast >= self.prev_slow
            and self.ema_fast < self.ema_slow
        )

        if self.in_position:
            return self._maybe_sell(close, crossed_down)
        return self._maybe_buy(close, dip, rebound, crossed_up)

    def _maybe_sell(self, close, crossed_down):
        pnl = close / self.entry_price - 1.0
        if close > self.peak_price:
            self.peak_price = close
        last_bars = self.bar_index > TOTAL_BARS - FINAL_BARS
        stop_hit = pnl <= STOP_LOSS
        trend_broken = crossed_down and pnl <= TREND_BREAK_LOSS
        final_exit = last_bars and pnl >= FINAL_PROFIT
        if stop_hit or trend_broken or final_exit:
            return [self._sell_all(close)]
        return []

    def _sell_all(self, close):
        amount = round(self.btc * close, 2)
        if amount < 0.01:
            amount = round(self.btc * close, 6)
        self.cash += self.btc * close
        self.btc = 0.0
        self.entry_price = None
        self.peak_price = None
        return {"side": "sell", "amount_usdc": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _maybe_buy(self, close, dip, rebound, crossed_up):
        if self.trades >= MAX_TRADES:
            return []
        if self.bar_index > TOTAL_BARS - NO_BUY_TAIL:
            return []
        first_buy_window = self.bar_index <= BUY_WINDOW_BARS
        if self.trades == 0 and not first_buy_window:
            # No position yet and the early window is gone: never chase.
            return []
        if self.trades == 0:
            confirmed_dip = dip >= DIP_BUY and rebound >= REBOUND_BUY
            if not (confirmed_dip or crossed_up):
                return []
        else:
            # Re-buy only on a deeper dip with a stronger rebound.
            if not (dip >= DIP_REBUY and rebound >= REBOUND_REBUY):
                return []
        return [self._buy_all(close)]

    def _buy_all(self, close):
        amount = round(self.cash * ALL_IN, 2)
        if amount < 1.0:
            return None
        spent = amount
        bought = spent / close
        self.cash -= spent
        self.btc += bought
        self.entry_price = close
        self.peak_price = close
        self.trades += 1
        return {"side": "buy", "amount_usdc": amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}


def extract_bar(msg):
    """Tolerant bar extraction: bar may sit in msg['bar'], in the last
    element of msg['bars'], or the message itself may be the bar."""
    bar = msg.get("bar")
    if bar is None and isinstance(msg.get("bars"), list) and msg["bars"]:
        bar = msg["bars"][-1]
    if bar is None:
        bar = msg
    if not isinstance(bar, dict):
        return None
    for key in ("close", "c", "Close", "price"):
        if key in bar and bar[key] is not None:
            try:
                return float(bar[key])
            except (TypeError, ValueError):
                return None
    return None


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type", "")
        if mtype == "hello":
            # Seed stored implicitly: the bot uses no randomness.
            continue
        if mtype == "done":
            break
        seq = msg.get("seq")
        if seq is None:
            seq = bot.bar_index
        close = extract_bar(msg)
        orders = []
        if close is not None and close > 0:
            orders = [o for o in bot.on_close(close) if o is not None]
        reply = {"seq": seq, "orders": orders}
        out.write(json.dumps(reply) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
