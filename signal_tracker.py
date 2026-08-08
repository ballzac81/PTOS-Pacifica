"""
PTOS Signal Tracker -- Pacifica Edition (Solana)

Listens for TradingView webhooks and executes perp trades on Pacifica.

BUY flow:
  POST /buy-signal       Arm (or refresh) the buy watch
  POST /trend-up         Trend confirmed up -> open long (or flip short to long)

SELL flow:
  POST /sell-signal      Arm (or refresh) the sell watch
  POST /trend-down       Trend confirmed down -> close long / open short

Manual overrides (require X-Webhook-Secret or token in JSON body):
  POST /emergency-close  Close ALL open positions + disarm + cooldown
  POST /reset            Disarm signals only

  GET  /status           Armed state, cooldowns, HTF bias, config
  GET  /positions        Live Pacifica positions
  GET  /trades           Bot trade log
  GET  /health           Health check
  GET  /dashboard        Simple web UI
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from pacifica_trader import PacificaTrader
from position_monitor import PositionMonitor
from telegram_notify import TelegramNotifier

# -- Config -------------------------------------------------------------------
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", 0))
SELL_MODE = os.environ.get("SELL_MODE", "short")
SELL_PCT = float(os.environ.get("SELL_PCT", "1.0"))
REARM_AFTER_STOP = os.environ.get("REARM_AFTER_STOP", "false").lower() == "true"
REARM_DELAY_SECONDS = int(os.environ.get("REARM_DELAY_SECONDS", "3600"))
HTF_FILTER_ENABLED = os.environ.get("HTF_FILTER_ENABLED", "false").lower() == "true"
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "0"))

# -- Trade log ----------------------------------------------------------------
TRADE_LOG_FILE = "/app/data/ptos_trades.json"
HTF_BIAS_FILE = "/app/data/ptos_htf_bias.json"
_trade_log: list = []
_trade_log_lock = threading.Lock()


def _load_trade_log():
    global _trade_log
    try:
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        if os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, "r") as f:
                _trade_log = json.load(f)
            logging.getLogger(__name__).info(f"Trade log loaded: {len(_trade_log)} entries")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not load trade log: {e}")
        _trade_log = []


def _save_trade_log():
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(_trade_log, f)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not save trade log: {e}")


def _log_trade(coin: str, action: str, result: str):
    entry = {
        "time": int(time.time() * 1000),
        "coin": coin,
        "action": action,
        "result": result,
    }
    with _trade_log_lock:
        _trade_log.insert(0, entry)
        _trade_log[:] = _trade_log[:500]
        _save_trade_log()


_load_trade_log()

htf_bias: dict = {}


def _load_htf_bias():
    global htf_bias
    try:
        if os.path.exists(HTF_BIAS_FILE):
            with open(HTF_BIAS_FILE, "r") as f:
                htf_bias.update(json.load(f))
            logging.getLogger(__name__).info(f"HTF bias loaded: {dict(htf_bias)}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not load HTF bias: {e}")


def _save_htf_bias():
    try:
        with open(HTF_BIAS_FILE, "w") as f:
            json.dump(htf_bias, f)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not save HTF bias: {e}")


# -- Startup validation -------------------------------------------------------
if SELL_MODE not in ("short", "close_long", "open_short"):
    print(f"ERROR: Invalid SELL_MODE '{SELL_MODE}'. Must be: short, close_long, open_short", flush=True)
    sys.exit(1)

if not SECRET_TOKEN:
    print("WARNING: SECRET_TOKEN is not set -- all webhook endpoints are unprotected", flush=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)

trader = PacificaTrader()
notifier = TelegramNotifier()
lock = threading.Lock()

armed: dict = {}
cooldown_until: dict = {}
_load_htf_bias()

monitor = PositionMonitor(
    trader,
    notifier,
    cooldown_until=cooldown_until,
    cooldown_seconds=COOLDOWN_SECONDS,
    armed=armed,
    armed_lock=lock,
    rearm_after_stop=REARM_AFTER_STOP,
    rearm_delay_seconds=REARM_DELAY_SECONDS,
    trade_log_callback=_log_trade,
)
monitor.start()


def _shutdown_handler(signum, frame):
    notifier.send("PTOS-Pacifica container shutting down -- no stop monitoring until restart!")
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown_handler)

size_label = os.environ.get("POSITION_SIZE_USDC") or f"{float(os.environ.get('POSITION_SIZE_PCT', '0.1')) * 100:.0f}%"
notifier.send(
    f"PTOS-Pacifica started | "
    f"trailing stop={float(os.environ.get('TRAILING_STOP_PCT', '0.05')) * 100:.0f}% | "
    f"size={size_label} | "
    f"lev={os.environ.get('LEVERAGE', '3')}x"
)


def _check_secret():
    token = request.headers.get("X-Webhook-Secret") or (request.json or {}).get("token") or ""
    if SECRET_TOKEN and token != SECRET_TOKEN:
        return False
    return True


def _extract_coin():
    data = request.get_json(silent=True) or {}
    coin = (
        data.get("coin") or data.get("symbol") or data.get("ticker") or ""
    ).upper().replace(".P", "").replace("PERP", "").replace("-PERP", "")
    return coin or None


def _in_cooldown(coin: str) -> bool:
    return time.time() < cooldown_until.get(coin, 0)


def _htf_allows(coin: str, direction: str) -> bool:
    if not HTF_FILTER_ENABLED:
        return True
    bias = htf_bias.get(coin, "neutral")
    if direction == "buy" and bias == "bear":
        return False
    if direction == "sell" and bias == "bull":
        return False
    return True


def _arm(coin: str, direction: str):
    with lock:
        armed[coin] = {"direction": direction, "armed_at": time.time()}
    logger.info(f"[{coin}] Armed {direction}")
    notifier.send(f"[{coin}] Armed {direction.upper()}")


def _disarm(coin: str = None):
    with lock:
        if coin:
            armed.pop(coin, None)
        else:
            armed.clear()


def _is_armed(coin: str, direction: str) -> bool:
    with lock:
        entry = armed.get(coin)
        if not entry or entry["direction"] != direction:
            return False
        if WINDOW_SECONDS > 0 and (time.time() - entry["armed_at"]) > WINDOW_SECONDS:
            armed.pop(coin, None)
            return False
        return True


# -- Webhooks -----------------------------------------------------------------
@app.route("/buy-signal", methods=["POST"])
@limiter.limit("30 per minute")
def buy_signal():
    coin = _extract_coin()
    if not coin:
        return jsonify({"error": "missing coin/symbol"}), 400
    _arm(coin, "buy")
    return jsonify({"status": "armed", "coin": coin, "direction": "buy"}), 202


@app.route("/trend-up", methods=["POST"])
@limiter.limit("30 per minute")
def trend_up():
    coin = _extract_coin()
    if not coin:
        return jsonify({"error": "missing coin/symbol"}), 400

    if not _is_armed(coin, "buy"):
        return jsonify({"status": "ignored", "reason": "not armed for buy"}), 200
    if _in_cooldown(coin):
        return jsonify({"status": "ignored", "reason": "cooldown"}), 200
    if not _htf_allows(coin, "buy"):
        notifier.send(f"[{coin}] Blocked by HTF bias (need bull)")
        return jsonify({"status": "blocked", "reason": "htf_bias"}), 200

    def _execute():
        positions = trader.get_positions()
        has_short = any(p["coin"] == coin and p["side"] == "short" for p in positions)
        if has_short:
            result = trader.reverse_position(coin)
            action = "flip_to_long"
        else:
            result = trader.open_long(coin)
            action = "open_long"
        _log_trade(coin, action, result)
        notifier.send(f"[{coin}] {action.upper()} -> {result}")
        _disarm(coin)

    threading.Thread(target=_execute, daemon=True).start()
    return jsonify({"status": "accepted", "coin": coin, "action": "open_long"}), 202


@app.route("/sell-signal", methods=["POST"])
@limiter.limit("30 per minute")
def sell_signal():
    coin = _extract_coin()
    if not coin:
        return jsonify({"error": "missing coin/symbol"}), 400
    _arm(coin, "sell")
    return jsonify({"status": "armed", "coin": coin, "direction": "sell"}), 202


@app.route("/trend-down", methods=["POST"])
@limiter.limit("30 per minute")
def trend_down():
    coin = _extract_coin()
    if not coin:
        return jsonify({"error": "missing coin/symbol"}), 400

    if not _is_armed(coin, "sell"):
        return jsonify({"status": "ignored", "reason": "not armed for sell"}), 200
    if _in_cooldown(coin):
        return jsonify({"status": "ignored", "reason": "cooldown"}), 200
    if not _htf_allows(coin, "sell"):
        notifier.send(f"[{coin}] Blocked by HTF bias (need bear)")
        return jsonify({"status": "blocked", "reason": "htf_bias"}), 200

    def _execute():
        if SELL_MODE == "short":
            positions = trader.get_positions()
            has_long = any(p["coin"] == coin and p["side"] == "long" for p in positions)
            if has_long:
                result = trader.reverse_position(coin)
                action = "flip_to_short"
            else:
                result = trader.open_short(coin)
                action = "open_short"
        elif SELL_MODE == "close_long":
            result = trader.close_long(coin, pct=SELL_PCT)
            action = "close_long"
        else:
            result = trader.open_short(coin)
            action = "open_short"

        _log_trade(coin, action, result)
        notifier.send(f"[{coin}] {action.upper()} -> {result}")
        _disarm(coin)
        if COOLDOWN_SECONDS > 0:
            cooldown_until[coin] = time.time() + COOLDOWN_SECONDS

    threading.Thread(target=_execute, daemon=True).start()
    return jsonify({"status": "accepted", "coin": coin, "action": SELL_MODE}), 202


@app.route("/htf-trend", methods=["POST"])
@limiter.limit("30 per minute")
def htf_trend():
    data = request.get_json(silent=True) or {}
    coin = (data.get("coin") or data.get("symbol") or "").upper()
    bias = (data.get("bias") or data.get("trend") or "").lower()
    if not coin or bias not in ("bull", "bear", "neutral"):
        return jsonify({"error": "need coin + bias (bull|bear|neutral)"}), 400
    htf_bias[coin] = bias
    _save_htf_bias()
    notifier.send(f"[{coin}] HTF bias -> {bias.upper()}")
    return jsonify({"status": "ok", "coin": coin, "bias": bias})


@app.route("/emergency-close", methods=["POST"])
def emergency_close():
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    def _run():
        positions = trader.get_positions()
        results = []
        for p in positions:
            if p["side"] == "long":
                r = trader.close_long(p["coin"])
            else:
                r = trader.close_short(p["coin"])
            results.append(f"{p['coin']} {p['side']}: {r}")
            _log_trade(p["coin"], "emergency_close", r)
        _disarm()
        for c in list(cooldown_until.keys()):
            cooldown_until[c] = time.time() + max(COOLDOWN_SECONDS, 300)
        msg = "EMERGENCY CLOSE\n" + "\n".join(results) if results else "EMERGENCY CLOSE - no open positions"
        notifier.send(msg)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "accepted"}), 202


@app.route("/reset", methods=["POST"])
def reset():
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401
    _disarm()
    notifier.send("All signals disarmed (positions left untouched)")
    return jsonify({"status": "disarmed"})


@app.route("/status")
def status():
    with lock:
        armed_copy = {k: dict(v) for k, v in armed.items()}
    return jsonify(
        {
            "armed": armed_copy,
            "cooldowns": {k: max(0, int(v - time.time())) for k, v in cooldown_until.items()},
            "htf_bias": dict(htf_bias),
            "config": {
                "sell_mode": SELL_MODE,
                "leverage": os.environ.get("LEVERAGE"),
                "trailing_stop_pct": os.environ.get("TRAILING_STOP_PCT"),
                "position_size_usdc": os.environ.get("POSITION_SIZE_USDC"),
                "position_size_pct": os.environ.get("POSITION_SIZE_PCT"),
            },
        }
    )


@app.route("/positions")
def positions():
    return jsonify(trader.get_positions())


@app.route("/trades")
def trades():
    with _trade_log_lock:
        return jsonify(_trade_log[:100])


@app.route("/health")
def health():
    try:
        price = trader.get_price("SOL")
        return jsonify(
            {
                "status": "ok",
                "address": trader.address,
                "sol_price": price,
                "api": trader.api_url,
            }
        )
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.route("/dashboard")
def dashboard():
    positions = trader.get_positions()
    with lock:
        armed_copy = dict(armed)
    html = f"""<!DOCTYPE html>
<html><head><title>PTOS-Pacifica Dashboard</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:system-ui;padding:20px}}
h1{{color:#58a6ff}} table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #30363d;padding:8px;text-align:left}}
th{{background:#161b22}} .green{{color:#3fb950}} .red{{color:#f85149}}
</style></head><body>
<h1>PTOS - Pacifica Edition</h1>
<p>Wallet: <code>{trader.address}</code></p>
<h2>Armed Signals</h2>
<pre>{json.dumps(armed_copy, indent=2)}</pre>
<h2>Open Positions</h2>
<table><tr><th>Coin</th><th>Side</th><th>Size</th><th>Entry</th><th>uPnL</th></tr>
"""
    for p in positions:
        color = "green" if p.get("unrealized_pnl", 0) >= 0 else "red"
        html += (
            f"<tr><td>{p['coin']}</td><td>{p['side']}</td>"
            f"<td>{p.get('size', 0):.4f}</td>"
            f"<td>{p.get('entry_price', 0):.4f}</td>"
            f"<td class='{color}'>{p.get('unrealized_pnl', 0):.2f}</td></tr>"
        )
    html += "</table><p><a href='/status'>/status</a> | <a href='/trades'>/trades</a> | <a href='/health'>/health</a></p></body></html>"
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
