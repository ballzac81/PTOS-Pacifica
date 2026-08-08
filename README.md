# PTOS — Pacifica Edition

A lightweight TradingView webhook sequencer that turns a two-step signal
(buy-signal + trend-up / sell-signal + trend-down) into perpetual-futures
trades on **Pacifica** (Solana).

The bot never holds a position until the trend confirms the direction. It adds
a native position stop-loss on Pacifica plus a polling back-stop, per-coin
trailing-stop overrides, stop-loss-from-entry, max-hold time, flip detection,
HTF bias filtering, cooldowns, and a full trade-log/dashboard.

> **Always start with a small `POSITION_SIZE_USDC` and a dedicated trading wallet
> before using real size. Pacifica is mainnet.**

---

## 1. What PTOS is

- **Purpose** – Execute only “confirmed” directional trades on Pacifica using a 2-step webhook flow (signal + trend confirmation).
- **Core idea** – Signals stay armed until a trend change fires, filtering noise and false entries.
- **Key safety features** – Dual-layer trailing stop (native Pacifica TP/SL + polling monitor), stop-loss from entry, max-hold time, position-size controls, cooldown after close, HTF bias filter, and emergency-close/reset endpoints.

---

## 2. Features

| Feature | Description |
|---------|-------------|
| **2-step confirmation** | Buy-signal arms; trend-up opens long (or flips short). |
| **Multi-signal refresh** | Repeated signals extend the armed window, never cancel it. |
| **No signal expiry** | Window configurable (`WINDOW_SECONDS`). |
| **Per-coin independence** | SOL, BTC, etc. tracked separately. |
| **Dual-layer trailing stop** | Native Pacifica stop-loss + polling back-stop. |
| **Per-coin trailing-stop override** | e.g. `SOL_TRAILING_STOP_PCT=0.04`. |
| **Stop-loss from entry** | Hard exit if price drops X% from entry (`STOP_LOSS_PCT`). |
| **Max hold time** | Auto-close after N seconds (`MAX_HOLD_SECONDS`). |
| **Flip detection** | Close opposite and open new direction. |
| **Async execution** | Webhooks return 202 instantly; trade runs in background, Telegram confirms. |
| **Full trade log** | Persisted JSON (`ptos_trades.json`). |
| **HTF bias filter** | Block longs unless HTF bias = bull, shorts unless bear. |
| **Cooldown after close** | Prevents whipsaw re-entries (`COOLDOWN_SECONDS`). |
| **SELL_MODE toggle** | `short` (default), `close_long`, `open_short`. |
| **Manual overrides** | `/emergency-close`, `/reset`. |
| **Telegram notifications** | All steps, executions, stops, bias changes. |
| **Single Docker container** | Ready for Unraid. |

---

## 3. How it works

**Buy flow:**
```
/buy-signal   ← your buy indicator fires (repeats refresh the window)
     |  bot armed, waiting...
/trend-up     ← trend confirms upward → LONG opened (or SHORT flipped to LONG)
                 returns 202 immediately — watch Telegram for trade confirmation
```

**Sell flow** – analogous with `/sell-signal` + `/trend-down`.

**Trailing stop (dual-layer)**
- **Native stop** – Pacifica `POST /positions/tpsl` (`set_position_tpsl`); survives container restarts.
- **Polling back-stop** – runs every `MONITOR_INTERVAL_SECONDS` (default 30 s) as safety net.
- Both layers trail the highest price (long) / lowest price (short).

**Sell behaviour** controlled by `SELL_MODE`.

---

## 4. Endpoints

| Category | Endpoint | Method | Description |
|----------|----------|--------|-------------|
| **TradingView webhooks** | `/buy-signal` | POST | Arm buy watch |
| | `/trend-up` | POST | Confirm up → open long |
| | `/sell-signal` | POST | Arm sell watch |
| | `/trend-down` | POST | Confirm down → sell / short |
| | `/htf-trend` | POST | Set HTF bias (`bull`/`bear`/`neutral`) |
| **Manual overrides** (need `SECRET_TOKEN`) | `/emergency-close` | POST | Close all positions, disarm, set cooldown |
| | `/reset` | POST | Disarm signals only |
| **Info** | `/status` | GET | Armed state, cooldowns, HTF bias, config |
| | `/positions` | GET | Live Pacifica positions |
| | `/trades` | GET | Bot trade log (JSON) |
| | `/health` | GET | Connectivity + SOL price |
| | `/dashboard` | GET | Simple web UI |

Example panic button:
```bash
curl -X POST http://YOUR_IP:5003/emergency-close \
     -H "X-Webhook-Secret: YOUR_SECRET_TOKEN"
```

---

## 5. Configuration (`.env`)

| Section | Variable | Default | Notes |
|---------|----------|---------|-------|
| **Required** | `SOLANA_PRIVATE_KEY` | — | base58 |
| | `SECRET_TOKEN` | — | Random hex string for webhook auth |
| **Network** | `PACIFICA_API_URL` | `https://api.pacifica.fi/api/v1` | |
| **Sizing** | `POSITION_SIZE_USDC` | — | **Preferred** fixed notional |
| | `POSITION_SIZE_PCT` | `0.10` | Approximate alternative |
| | `LEVERAGE` | `3` | Used to convert notional → base size |
| | `SLIPPAGE_PERCENT` | `0.5` | Market order max slippage |
| **Sell** | `SELL_MODE` | `short` | `short` / `close_long` / `open_short` |
| | `SELL_PCT` | `1.0` | |
| **Trailing stop** | `TRAILING_STOP_PCT` | `0.05` | |
| | `{COIN}_TRAILING_STOP_PCT` | — | Per-coin override |
| | `MONITOR_INTERVAL_SECONDS` | `30` | |
| **Stop-loss from entry** | `STOP_LOSS_PCT` | `0` | |
| **Max hold** | `MAX_HOLD_SECONDS` | `0` | |
| **HTF filter** | `HTF_FILTER_ENABLED` | `false` | |
| **Cooldown** | `COOLDOWN_SECONDS` | `0` | |
| **Signal window** | `WINDOW_SECONDS` | `0` | 0 = indefinite |
| **Telegram** | `TELEGRAM_TOKEN` | — | |
| | `TELEGRAM_CHAT_ID` | — | |

---

## 6. File structure

```
PTOS-Pacifica/
├─ .dockerignore
├─ .env.example
├─ .gitignore
├─ Dockerfile
├─ README.md
├─ docker-compose.yml
├─ pacifica_trader.py       # Core trade execution + Ed25519 signing
├─ position_monitor.py      # Dual-layer trailing-stop monitor
├─ signal_tracker.py        # Flask webhooks + state machine
├─ telegram_notify.py       # Telegram alerts
└─ requirements.txt
```

Volumes (via `docker-compose.yml`):
- `/app/data` → persistent trade log + HTF bias

---

## 7. Quick start (Unraid / self-hosted)

```bash
mkdir -p /mnt/user/appdata/ptos-pacifica
cd /mnt/user/appdata/ptos-pacifica

# Copy all project files here, then:
cp .env.example .env
nano .env          # fill in SOLANA_PRIVATE_KEY + SECRET_TOKEN + POSITION_SIZE_USDC

docker compose up -d --build
```

Port mapping: host `5003` → container `5000`.

### One-time Pacifica wallet preparation

1. Create / fund a **dedicated** Solana wallet with USDC (and a little SOL for any on-chain ops).
2. Deposit USDC into Pacifica via the web app: https://app.pacifica.fi (or testnet equivalent).
3. Optionally bind an **API agent key** in the UI if you want the bot to sign with a secondary key (not required for this repo — it signs with the main key).
4. Export the private key (base58) into `SOLANA_PRIVATE_KEY`.

> Never use your main cold wallet. Treat the trading key as hot.

### TradingView alerts

Point your alerts at:
```
http://YOUR_UNRAID_IP:5003/buy-signal
http://YOUR_UNRAID_IP:5003/trend-up
...
```

JSON body example:
```json
{"coin": "SOL", "token": "YOUR_SECRET_TOKEN"}
```
(or use the `X-Webhook-Secret` header).

---

## 8. Important notes specific to Pacifica

- All trade requests are **signed client-side** (Ed25519 via `solders`). The API never sees your private key.
- Side convention: **bid = long**, **ask = short**.
- Position size is calculated as `POSITION_SIZE_USDC × LEVERAGE / mark_price` (base units). Prefer fixed USDC sizing.
- Native stops use `POST /positions/tpsl`. For a long position the exit side is `ask`; for a short it is `bid`.
- Prefer a private Solana RPC only if you later add on-chain deposit helpers; pure trading goes through Pacifica’s REST API.
- Test thoroughly with tiny size first.

### Supported markets

Pacifica lists a wide set of crypto (and some RWA) perps. Send the correct symbol from TradingView:

```json
{"coin": "SOL"}
{"coin": "BTC"}
{"coin": "ETH"}
```

Check live markets on the Pacifica app / `/info` endpoints.

---

## 9. License

MIT (same spirit as the Hyperliquid edition).

---

**Adapted from the original PTOS-Hyperliquid by ballzac81.**  
Pacifica REST API + official signing pattern from [pacifica-fi/python-sdk](https://github.com/pacifica-fi/python-sdk).
