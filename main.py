#!/usr/bin/env python3
"""BTC race bot — long-only cbBTC/USDC dip-buy and hold.

Strategy (top 1-3 pattern from the 18 Nov race):
  * EMA 8 / EMA 21 on 1-minute closes; wait 25 bars for the EMAs to form.
  * FIRST BUY inside the first 240 scored bars (~4 h): on the first
    confirmed dip (dip >= 0.4% below the 24-bar high AND rebound >= 0.15%
    above the 24-bar low) or on a momentum cross-up (EMA8 crosses above
    EMA21). All-in, 99.9% of cash.
  * HOLD: no small take-profit. Sell ONLY on stop loss -2.5%, on an EMA
    cross-down while down >= 1%, or in the last 10 bars if up >= 2%.
  * RE-BUY only on a deep dip (>= 0.8% below the 24-bar high AND
    rebound >= 0.3% above the 24-bar low).
  * Cap: at most 9 trades (filled buys) over the whole race.

Protocol: agent-fun/bars-replay/v1, NDJSON over stdin/stdout. One `hello`
at the start (no reply), one batch per minute answered with exactly one
reply of type "reply" echoing its seq, then `done` and exit. Prices,
cash and quantities are integer units at the scales hello announces
(price 5, quote 6, quantity 8); orders name the symbol, a client id and
max_slippage_bps. Position and cash are re-read from every batch's
account, so the bot always acts on the fills that really happened.

Deterministic: standard library only, no randomness, no wall clock, no
network, no file or environment reads. The same price series always
produces the same orders, byte for byte.
"""

import json
import sys

# --- strategy parameters ---
EMA_FAST = 8
EMA_SLOW = 21
EMA_FORM_BARS = 25
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
ALL_IN_NUM = 999         # buy 99.9% of cash; the rest covers fill costs
ALL_IN_DEN = 1000
TOTAL_BARS = 1440
NO_BUY_TAIL = 15         # never open a new position in the last 15 bars
MAX_SLIPPAGE_BPS = 50
SYMBOL = "CBBTC"


def ema_alpha(n):
    return 2.0 / (n + 1.0)


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Bot:
    def __init__(self):
        self.price_scale = 5
        self.quote_scale = 6
        self.qty_scale = 8
        self.warmup_bars = 0
        self.max_weight_bps = 10000
        self.min_notional = 1000000
        self.bars_seen = 0
        self.closes = []
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None
        self.cash_units = 1000000000
        self.equity_units = 1000000000
        self.qty_units = 0
        self.entry_raw = None
        self.last_fill_price = None
        self.peak_raw = None
        self.filled_buys = set()
        self.order_seq = 0

    @property
    def scored_index(self):
        """Bars of the scored window seen so far (warm-up bars excluded)."""
        return self.bars_seen - self.warmup_bars

    @property
    def in_position(self):
        return self.qty_units > 0

    def on_hello(self, msg):
        syms = msg.get("symbols") or []
        if syms:
            self.price_scale = to_int(syms[0].get("price_scale"), 5)
            self.qty_scale = to_int(syms[0].get("qty_scale"), 8)
        cap = msg.get("capital") or {}
        self.quote_scale = to_int(cap.get("quote_scale"), 6)
        self.cash_units = to_int(cap.get("quote_units"), 1000000000)
        self.equity_units = self.cash_units
        caps = msg.get("caps") or {}
        self.max_weight_bps = to_int(caps.get("max_weight_bps"), 10000)
        fees = msg.get("fees") or {}
        self.min_notional = to_int(fees.get("min_notional_quote_units"),
                                   1000000)
        self.warmup_bars = to_int(msg.get("warmup_bars"), 0)

    def on_batch(self, msg):
        """Sync state from fills + account, feed the bar, return orders."""
        self._apply_fills(msg.get("fills") or [])
        self._sync_account(msg.get("account"))
        bar = self._last_bar(msg.get("bars") or [])
        if bar is None:
            return []
        close_raw = to_int(bar.get("c"), 0)
        if close_raw <= 0:
            return []
        return self._on_close(close_raw)

    def _last_bar(self, bars):
        for bar in reversed(bars):
            if isinstance(bar, dict) and bar.get("symbol", SYMBOL) == SYMBOL:
                return bar
        for bar in reversed(bars):
            if isinstance(bar, dict):
                return bar
        return None

    def _apply_fills(self, fills):
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            if fill.get("status") != "filled":
                continue
            cid = fill.get("client_id")
            price = to_int(fill.get("price"), 0)
            if fill.get("side") == "buy":
                if cid is not None:
                    self.filled_buys.add(cid)
                if price > 0:
                    self.last_fill_price = price
            elif fill.get("side") == "sell":
                if price > 0:
                    self.last_fill_price = price

    def _sync_account(self, account):
        if not isinstance(account, dict):
            return
        cash = to_int(account.get("cash_quote_units"), -1)
        if cash >= 0:
            self.cash_units = cash
        equity = to_int(account.get("equity_quote_units"), -1)
        if equity >= 0:
            self.equity_units = equity
        qty = 0
        cost = 0
        for pos in account.get("positions") or []:
            if isinstance(pos, dict) and pos.get("symbol", SYMBOL) == SYMBOL:
                qty += to_int(pos.get("qty"), 0)
                cost += to_int(pos.get("cost_quote_units"), 0)
        self.qty_units = qty
        if qty > 0 and cost > 0:
            # Entry price in raw price units: cost_usd / qty_btc, rescaled.
            # cost_usd = cost / 10**quote_scale, qty_btc = qty / 10**qty_scale,
            # price_raw = price_usd * 10**price_scale, all in integers.
            self.entry_raw = (cost * (10 ** (self.qty_scale
                                             + self.price_scale))
                              // qty // (10 ** self.quote_scale))
        elif qty > 0 and self.last_fill_price:
            self.entry_raw = self.last_fill_price
        elif qty <= 0:
            self.entry_raw = None
            self.peak_raw = None

    def _on_close(self, close_raw):
        self.bars_seen += 1
        self.closes.append(close_raw)
        window = self.closes[-WINDOW_BARS:]

        # Update EMAs, keeping the previous values for cross detection.
        self.prev_fast = self.ema_fast
        self.prev_slow = self.ema_slow
        if self.ema_fast is None:
            self.ema_fast = float(close_raw)
            self.ema_slow = float(close_raw)
        else:
            self.ema_fast += ema_alpha(EMA_FAST) * (close_raw - self.ema_fast)
            self.ema_slow += ema_alpha(EMA_SLOW) * (close_raw - self.ema_slow)

        if self.bars_seen <= EMA_FORM_BARS:
            return []
        if self.scored_index <= 0:
            # Still in the warm-up: build indicators, do not trade.
            return []

        high24 = max(window)
        low24 = min(window)
        dip = (high24 - close_raw) / high24 if high24 > 0 else 0.0
        rebound = (close_raw - low24) / low24 if low24 > 0 else 0.0
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
            return self._maybe_sell(close_raw, crossed_down)
        return self._maybe_buy(close_raw, dip, rebound, crossed_up)

    def _maybe_sell(self, close_raw, crossed_down):
        if self.entry_raw is None or self.entry_raw <= 0:
            return []
        pnl = close_raw / self.entry_raw - 1.0
        if self.peak_raw is None or close_raw > self.peak_raw:
            self.peak_raw = close_raw
        last_bars = self.scored_index > TOTAL_BARS - FINAL_BARS
        stop_hit = pnl <= STOP_LOSS
        trend_broken = crossed_down and pnl <= TREND_BREAK_LOSS
        final_exit = last_bars and pnl >= FINAL_PROFIT
        if stop_hit or trend_broken or final_exit:
            return [self._sell_all()]
        return []

    def _sell_all(self):
        self.order_seq += 1
        return {"side": "sell",
                "client_id": "s%d" % self.order_seq,
                "symbol": SYMBOL,
                "qty": str(self.qty_units),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _maybe_buy(self, close_raw, dip, rebound, crossed_up):
        if len(self.filled_buys) >= MAX_TRADES:
            return []
        if self.scored_index > TOTAL_BARS - NO_BUY_TAIL:
            return []
        first_window = self.scored_index <= BUY_WINDOW_BARS
        if not self.filled_buys and not first_window:
            # No position yet and the early window is gone: never chase.
            return []
        if not self.filled_buys:
            confirmed_dip = dip >= DIP_BUY and rebound >= REBOUND_BUY
            if not (confirmed_dip or crossed_up):
                return []
        else:
            # Re-buy only on a deeper dip with a stronger rebound.
            if not (dip >= DIP_REBUY and rebound >= REBOUND_REBUY):
                return []
        return [self._buy_all()]

    def _buy_all(self):
        by_cash = self.cash_units * ALL_IN_NUM // ALL_IN_DEN
        by_weight = self.equity_units * self.max_weight_bps // 10000
        by_weight = by_weight * ALL_IN_NUM // ALL_IN_DEN
        notional = by_cash if by_cash < by_weight else by_weight
        if notional < self.min_notional:
            return None
        self.order_seq += 1
        return {"side": "buy",
                "client_id": "b%d" % self.order_seq,
                "symbol": SYMBOL,
                "notional_quote_units": str(notional),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}


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
            bot.on_hello(msg)
            continue
        if mtype == "done":
            break
        if mtype != "batch":
            continue
        seq = msg.get("seq")
        if seq is None:
            seq = bot.bars_seen
        orders = [o for o in bot.on_batch(msg) if o is not None]
        reply = {"type": "reply", "seq": seq, "orders": orders}
        out.write(json.dumps(reply) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
