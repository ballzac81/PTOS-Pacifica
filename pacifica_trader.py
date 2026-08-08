"""
Pacifica trade execution layer (Solana perps).

Uses the official Pacifica REST signing pattern (Ed25519 via solders):
  - POST /orders/create_market   open / close / reverse
  - POST /positions/tpsl         native stop-loss
  - GET  /positions?account=...  live positions
  - GET  /info/prices            mark prices

Side convention (Pacifica):
  bid = long / buy
  ask = short / sell

Environment:
  SOLANA_PRIVATE_KEY   base58 secret (required)
  PACIFICA_API_URL     default https://api.pacifica.fi/api/v1
  POSITION_SIZE_USDC   fixed notional per trade in USDC (preferred)
  POSITION_SIZE_PCT    fraction of equity (fallback)
  LEVERAGE             leverage multiplier (default 3)
  SLIPPAGE_PERCENT     market order slippage tolerance (default 0.5)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import base58
import requests
from solders.keypair import Keypair
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
DEFAULT_SLIPPAGE = "0.5"
EXPIRY_WINDOW_MS = 5_000


def _retryable(fn):
    return retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )(fn)


def _sort_json_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_json_keys(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json_keys(item) for item in value]
    return value


def _prepare_message(header: dict, payload: dict) -> str:
    data = {**header, "data": payload}
    sorted_data = _sort_json_keys(data)
    return json.dumps(sorted_data, separators=(",", ":"))


class PacificaTrader:
    def __init__(self):
        pk_raw = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
        if not pk_raw or pk_raw.startswith("YOUR_"):
            raise ValueError(
                "SOLANA_PRIVATE_KEY is not set. "
                "Copy .env.example to .env and fill in your Solana private key (base58)."
            )

        self.keypair = self._load_keypair(pk_raw)
        self.address = str(self.keypair.pubkey())

        self.api_url = os.environ.get(
            "PACIFICA_API_URL", "https://api.pacifica.fi/api/v1"
        ).rstrip("/")

        self.size_usdc = float(os.environ.get("POSITION_SIZE_USDC", "0") or 0)
        self.size_pct = float(os.environ.get("POSITION_SIZE_PCT", "0.10"))
        self.leverage = float(os.environ.get("LEVERAGE", "3"))
        self.slippage = os.environ.get("SLIPPAGE_PERCENT", DEFAULT_SLIPPAGE)

        logger.info(
            f"PacificaTrader ready | addr={self.address[:8]}... | "
            f"size_usdc={self.size_usdc or 'pct-mode'} | lev={self.leverage}x"
        )

    # ------------------------------------------------------------------
    # Key loading
    # ------------------------------------------------------------------
    @staticmethod
    def _load_keypair(raw: str) -> Keypair:
        raw = raw.strip()
        if raw.startswith("["):
            arr = json.loads(raw)
            return Keypair.from_bytes(bytes(arr))
        secret = base58.b58decode(raw)
        if len(secret) == 64:
            return Keypair.from_bytes(secret)
        if len(secret) == 32:
            return Keypair.from_seed(secret)
        # solders also accepts full base58 secret key strings
        try:
            return Keypair.from_base58_string(raw)
        except Exception as e:
            raise ValueError(f"Could not decode SOLANA_PRIVATE_KEY: {e}")

    # ------------------------------------------------------------------
    # Signing + HTTP
    # ------------------------------------------------------------------
    def _sign(self, op_type: str, payload: dict) -> dict:
        """Return the common signed request header fields."""
        timestamp = int(time.time() * 1000)
        header = {
            "timestamp": timestamp,
            "expiry_window": EXPIRY_WINDOW_MS,
            "type": op_type,
        }
        message = _prepare_message(header, payload)
        signature = self.keypair.sign_message(message.encode("utf-8"))
        sig_b58 = base58.b58encode(bytes(signature)).decode("ascii")
        return {
            "account": self.address,
            "signature": sig_b58,
            "timestamp": timestamp,
            "expiry_window": EXPIRY_WINDOW_MS,
        }

    def _post_signed(self, path: str, op_type: str, payload: dict) -> dict:
        body = {**self._sign(op_type, payload), **payload}
        url = f"{self.api_url}{path}"
        resp = requests.post(
            url, json=body, headers={"Content-Type": "application/json"}, timeout=20
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {data}")
        if data.get("success") is False or data.get("error"):
            raise RuntimeError(f"Pacifica error: {data.get('error') or data}")
        return data

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.api_url}{path}"
        resp = requests.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Market data / sizing
    # ------------------------------------------------------------------
    @_retryable
    def get_price(self, coin: str) -> float:
        """Best-effort mark / mid price for a symbol."""
        data = self._get("/info/prices")
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            # fallback: try single-symbol style
            items = data if isinstance(data, list) else []
        sym = coin.upper()
        for row in items:
            s = (row.get("symbol") or row.get("s") or "").upper()
            if s == sym or s.startswith(sym):
                for key in ("mark", "mark_price", "mid", "mid_price", "oracle", "price"):
                    if row.get(key) is not None:
                        return float(row[key])
        raise RuntimeError(f"No price found for {coin}")

    def _calc_base_amount(self, coin: str) -> str:
        """Convert POSITION_SIZE_USDC (notional) into base asset amount string."""
        price = self.get_price(coin)
        if price <= 0:
            raise RuntimeError(f"Invalid price for {coin}: {price}")

        if self.size_usdc > 0:
            notional = self.size_usdc * self.leverage
        else:
            # Approximate: use size_pct of a conservative equity floor
            notional = max(10.0, 100.0 * self.size_pct) * self.leverage

        amount = notional / price
        # Reasonable precision; Pacifica expects decimal strings
        if amount >= 1:
            return f"{amount:.4f}"
        if amount >= 0.01:
            return f"{amount:.6f}"
        return f"{amount:.8f}"

    # ------------------------------------------------------------------
    # Public trading API (same surface as Hyperliquid / Flash editions)
    # ------------------------------------------------------------------
    def open_long(self, coin: str) -> str:
        return self._market_order(coin, side="bid", reduce_only=False)

    def open_short(self, coin: str) -> str:
        return self._market_order(coin, side="ask", reduce_only=False)

    def close_long(self, coin: str, pct: float = 1.0) -> str:
        pos = self._find_position(coin, "long")
        if not pos:
            return f"No open long on {coin}"
        amount = self._close_amount(pos, pct)
        return self._market_order(coin, side="ask", reduce_only=True, amount=amount)

    def close_short(self, coin: str, pct: float = 1.0) -> str:
        pos = self._find_position(coin, "short")
        if not pos:
            return f"No open short on {coin}"
        amount = self._close_amount(pos, pct)
        return self._market_order(coin, side="bid", reduce_only=True, amount=amount)

    def reverse_position(self, coin: str) -> str:
        """Close existing side and open the opposite (two market orders)."""
        positions = self.get_positions()
        pos = next((p for p in positions if p["coin"].upper() == coin.upper()), None)
        if not pos:
            return f"No position on {coin} to reverse"

        if pos["side"] == "long":
            close_res = self.close_long(coin, pct=1.0)
            open_res = self.open_short(coin)
            return f"reversed long->short | close={close_res} | open={open_res}"
        else:
            close_res = self.close_short(coin, pct=1.0)
            open_res = self.open_long(coin)
            return f"reversed short->long | close={close_res} | open={open_res}"

    def _close_amount(self, pos: dict, pct: float) -> str:
        size = float(pos.get("size") or 0) * max(0.0, min(1.0, pct))
        if size <= 0:
            raise RuntimeError("Close size is zero")
        if size >= 1:
            return f"{size:.4f}"
        if size >= 0.01:
            return f"{size:.6f}"
        return f"{size:.8f}"

    def _market_order(
        self,
        coin: str,
        side: str,
        reduce_only: bool,
        amount: Optional[str] = None,
    ) -> str:
        try:
            amt = amount or self._calc_base_amount(coin)
            payload = {
                "symbol": coin.upper(),
                "amount": amt,
                "side": side,
                "reduce_only": reduce_only,
                "slippage_percent": str(self.slippage),
                "client_order_id": str(uuid.uuid4()),
            }
            data = self._post_signed(
                "/orders/create_market", "create_market_order", payload
            )
            order_id = None
            if isinstance(data.get("data"), dict):
                order_id = data["data"].get("order_id") or data["data"].get("i")
            logger.info(
                f"[{coin}] market {side} reduce_only={reduce_only} "
                f"amount={amt} | order_id={order_id}"
            )
            return f"market {side} {coin} amount={amt} | order_id={order_id or 'ok'}"
        except Exception as e:
            logger.error(f"[{coin}] market order failed: {e}")
            return f"ERROR: {e}"

    # ------------------------------------------------------------------
    # Native stop-loss (position TP/SL)
    # ------------------------------------------------------------------
    def place_stop_loss(
        self, coin: str, is_long: bool, stop_price: float, size: float = 0
    ) -> Optional[str]:
        """
        Attach a stop-loss to an existing position.
        For a LONG position the exit side is 'ask'.
        For a SHORT position the exit side is 'bid'.
        """
        try:
            exit_side = "ask" if is_long else "bid"
            stop_loss: Dict[str, Any] = {
                "stop_price": f"{stop_price:.6f}".rstrip("0").rstrip("."),
            }
            if size > 0:
                stop_loss["amount"] = f"{size:.8f}".rstrip("0").rstrip(".")

            payload: Dict[str, Any] = {
                "symbol": coin.upper(),
                "side": exit_side,
                "stop_loss": stop_loss,
            }
            data = self._post_signed("/positions/tpsl", "set_position_tpsl", payload)
            logger.info(
                f"[{coin}] Native SL placed @ {stop_price:.4f} "
                f"(exit side={exit_side})"
            )
            return str(data.get("data") or "ok")
        except Exception as e:
            logger.warning(f"[{coin}] place_stop_loss failed: {e}")
            return None

    def cancel_all_stops(self, coin: str, side: str) -> None:
        """Best-effort: cancel open stop orders for the symbol."""
        try:
            # Cancel all orders on the symbol (includes stops where supported)
            payload = {"symbol": coin.upper()}
            self._post_signed("/orders/cancel_all", "cancel_all_orders", payload)
            logger.debug(f"[{coin}] cancel_all_orders sent")
        except Exception as e:
            logger.debug(f"[{coin}] cancel_all_stops: {e}")

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def get_positions(self) -> List[Dict]:
        try:
            data = self._get("/positions", params={"account": self.address})
            raw = data.get("data") if isinstance(data, dict) else data
            if not isinstance(raw, list):
                return []

            positions = []
            for p in raw:
                # Pacifica: bid = long, ask = short
                side_raw = (p.get("side") or "").lower()
                if side_raw in ("bid", "long"):
                    side = "long"
                elif side_raw in ("ask", "short"):
                    side = "short"
                else:
                    continue

                amount = float(p.get("amount") or 0)
                if amount == 0:
                    continue

                entry = float(p.get("entry_price") or 0)
                size_usd = amount * entry if entry else 0.0

                positions.append(
                    {
                        "coin": (p.get("symbol") or "").upper(),
                        "side": side,
                        "size": abs(amount),
                        "size_usd": size_usd,
                        "entry_price": entry,
                        "unrealized_pnl": float(
                            p.get("unrealized_pnl") or p.get("upnl") or 0
                        ),
                        "leverage": p.get("leverage"),
                        "liquidation_px": p.get("liquidation_price"),
                        "raw": p,
                    }
                )
            return positions
        except Exception as e:
            logger.error(f"get_positions failed: {e}")
            return []

    def _find_position(self, coin: str, side: str) -> Optional[Dict]:
        for p in self.get_positions():
            if p["coin"].upper() == coin.upper() and p["side"] == side:
                return p
        return None

    def get_open_stop_orders(self, coin: str) -> list:
        return []

    def cancel_order(self, coin: str, oid: Any) -> None:
        pass

    def get_fills(self, limit: int = 50) -> list:
        return []
