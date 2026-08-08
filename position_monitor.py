"""
Position Monitor -- trailing stop for all open Pacifica positions.

Dual-layer protection:
  1. Native resting stop-loss via POST /positions/tpsl (set_position_tpsl).
     Survives container downtime. Updated when the trailing peak moves.

  2. Polling backstop loop (every MONITOR_INTERVAL_SECONDS).
     Catches edge cases the native order misses.

For longs:  trails TRAILING_STOP_PCT below peak.
For shorts: trails TRAILING_STOP_PCT above trough.

Per-coin overrides: set {COIN}_TRAILING_STOP_PCT=0.04

Additional:
  STOP_LOSS_PCT    -- close if price moves X% against entry
  MAX_HOLD_SECONDS -- auto-close after N seconds
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0"))
MAX_HOLD_SECONDS = int(os.environ.get("MAX_HOLD_SECONDS", "0"))


class PositionMonitor:
    def __init__(
        self,
        trader,
        notifier,
        cooldown_until: dict = None,
        cooldown_seconds: int = 0,
        armed: dict = None,
        armed_lock=None,
        rearm_after_stop: bool = False,
        rearm_delay_seconds: int = 3600,
        trade_log_callback=None,
    ):
        self.trader = trader
        self.notifier = notifier
        self.trail_pct = float(os.environ.get("TRAILING_STOP_PCT", "0.05"))
        self.interval = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))

        self._best: dict = {}
        self._entry_price: dict = {}
        self._entry_time: dict = {}
        self._stop_orders: dict = {}

        self._cooldown_until = cooldown_until or {}
        self._cooldown_seconds = cooldown_seconds

        self._armed = armed
        self._armed_lock = armed_lock
        self._rearm_after_stop = rearm_after_stop
        self._rearm_delay_seconds = rearm_delay_seconds
        self._trade_log_callback = trade_log_callback

        self._closing: set = set()
        self._stop = threading.Event()

    def start(self):
        if self.trail_pct <= 0 and STOP_LOSS_PCT <= 0 and MAX_HOLD_SECONDS <= 0:
            logger.info("Position monitor disabled (no trailing / stop-loss / max-hold)")
            return
        logger.info(
            f"Position monitor started | trailing={self.trail_pct * 100:.1f}% | "
            f"stop-from-entry={STOP_LOSS_PCT * 100:.1f}% | "
            f"max-hold={'off' if MAX_HOLD_SECONDS == 0 else str(MAX_HOLD_SECONDS) + 's'} | "
            f"interval={self.interval}s"
        )
        threading.Thread(target=self._run, daemon=True).start()

    def _trail_pct_for(self, coin: str) -> float:
        val = os.environ.get(f"{coin.upper()}_TRAILING_STOP_PCT")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return self.trail_pct

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"Monitor error: {e}")

    def _place_native_stop(self, coin: str, side: str, stop_price: float, size: float = 0):
        is_long = side == "long"
        return self.trader.place_stop_loss(coin, is_long=is_long, stop_price=stop_price, size=size)

    def _cancel_native_stop(self, coin: str, side: str):
        key = (coin, side)
        if key in self._stop_orders:
            try:
                self.trader.cancel_all_stops(coin, side)
            except Exception:
                pass
            self._stop_orders.pop(key, None)

    def _update_native_stop(self, coin: str, side: str, new_stop_px: float, size: float = 0):
        self._cancel_native_stop(coin, side)
        oid = self._place_native_stop(coin, side, new_stop_px, size)
        if oid is not None:
            self._stop_orders[(coin, side)] = oid

    def _check_all(self):
        try:
            positions = self.trader.get_positions()
        except Exception as e:
            logger.error(f"Monitor: failed to fetch positions: {e}")
            return

        active_keys = set()

        for pos in positions:
            coin = pos["coin"]
            side = pos["side"]
            size = pos.get("size", 0)
            entry_px = pos.get("entry_price", 0)
            key = (coin, side)
            active_keys.add(key)
            trail_pct = self._trail_pct_for(coin)

            if key in self._closing:
                continue

            try:
                price = self.trader.get_price(coin)
            except Exception as e:
                logger.warning(f"[{coin}] price fetch failed: {e}")
                continue

            if key not in self._best:
                self._best[key] = price
                self._entry_price[key] = entry_px if entry_px else price
                self._entry_time[key] = time.time()

                if trail_pct > 0:
                    stop_px = (
                        price * (1 - trail_pct) if side == "long" else price * (1 + trail_pct)
                    )
                    logger.info(
                        f"[{coin}] {side.upper()} detected -- trailing {trail_pct * 100:.1f}% | "
                        f"ref={price:.4f} | stop={stop_px:.4f}"
                    )
                    oid = self._place_native_stop(coin, side, stop_px, size)
                    if oid:
                        self._stop_orders[key] = oid
                continue

            if MAX_HOLD_SECONDS > 0:
                held = time.time() - self._entry_time.get(key, time.time())
                if held > MAX_HOLD_SECONDS:
                    logger.warning(f"[{coin}] Max hold time reached ({held / 3600:.1f}h) -- closing")
                    self._force_close(coin, side, "max_hold")
                    continue

            if STOP_LOSS_PCT > 0:
                entry = self._entry_price.get(key, price)
                if side == "long" and price <= entry * (1 - STOP_LOSS_PCT):
                    logger.warning(f"[{coin}] Stop-loss from entry hit ({STOP_LOSS_PCT * 100:.1f}%)")
                    self._force_close(coin, side, "stop_from_entry")
                    continue
                if side == "short" and price >= entry * (1 + STOP_LOSS_PCT):
                    logger.warning(f"[{coin}] Stop-loss from entry hit ({STOP_LOSS_PCT * 100:.1f}%)")
                    self._force_close(coin, side, "stop_from_entry")
                    continue

            if trail_pct <= 0:
                continue

            best = self._best[key]
            if side == "long":
                if price > best:
                    self._best[key] = price
                    new_stop = price * (1 - trail_pct)
                    self._update_native_stop(coin, side, new_stop, size)
                    logger.debug(f"[{coin}] Long trail updated | peak={price:.4f} | stop={new_stop:.4f}")
                else:
                    stop_level = best * (1 - trail_pct)
                    if price <= stop_level:
                        logger.warning(
                            f"[{coin}] Trailing stop hit (long) | peak={best:.4f} | price={price:.4f}"
                        )
                        self._force_close(coin, side, "trailing_stop")
            else:
                if price < best:
                    self._best[key] = price
                    new_stop = price * (1 + trail_pct)
                    self._update_native_stop(coin, side, new_stop, size)
                    logger.debug(
                        f"[{coin}] Short trail updated | trough={price:.4f} | stop={new_stop:.4f}"
                    )
                else:
                    stop_level = best * (1 + trail_pct)
                    if price >= stop_level:
                        logger.warning(
                            f"[{coin}] Trailing stop hit (short) | trough={best:.4f} | price={price:.4f}"
                        )
                        self._force_close(coin, side, "trailing_stop")

        for key in list(self._best.keys()):
            if key not in active_keys:
                self._best.pop(key, None)
                self._entry_price.pop(key, None)
                self._entry_time.pop(key, None)
                self._cancel_native_stop(*key)
                self._stop_orders.pop(key, None)

    def _force_close(self, coin: str, side: str, reason: str):
        key = (coin, side)
        if key in self._closing:
            return
        self._closing.add(key)
        try:
            if side == "long":
                result = self.trader.close_long(coin, pct=1.0)
            else:
                result = self.trader.close_short(coin, pct=1.0)

            msg = f"[{coin}] Force-closed {side.upper()} ({reason}) | {result}"
            logger.info(msg)
            self.notifier.send(msg)
            if self._trade_log_callback:
                self._trade_log_callback(coin, f"close_{side}_{reason}", result)

            if self._cooldown_seconds > 0:
                self._cooldown_until[coin] = time.time() + self._cooldown_seconds

            if self._rearm_after_stop and self._armed is not None and self._armed_lock:

                def _rearm():
                    time.sleep(self._rearm_delay_seconds)
                    with self._armed_lock:
                        direction = "buy" if side == "long" else "sell"
                        self._armed[coin] = {"direction": direction, "armed_at": time.time()}
                    self.notifier.send(f"[{coin}] Re-armed {direction} after stop-out")

                threading.Thread(target=_rearm, daemon=True).start()

        except Exception as e:
            logger.error(f"[{coin}] Force close failed: {e}")
        finally:
            self._closing.discard(key)
            self._best.pop(key, None)
            self._entry_price.pop(key, None)
            self._entry_time.pop(key, None)
            self._cancel_native_stop(coin, side)
