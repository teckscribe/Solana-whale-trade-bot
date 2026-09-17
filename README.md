# 🐋 Solana Whale Tracker & High-Frequency Copy-Trading Bot (TRAM Architecture)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Solana](https://img.shields.io/badge/Solana-Mainnet-black?logo=solana)](https://solana.com/)
[![Jupiter DEX](https://img.shields.io/badge/Jupiter-DEX_v6-emerald)](https://jup.ag/)
[![FastAPI](https://img.shields.io/badge/Dashboard-FastAPI-teal?logo=fastapi)](https://fastapi.tiangolo.com/)

A high-performance, asynchronous Solana whale tracking and automated copy-trading bot powered by the **TRAM (True RAM / Zero-Disk Hot Path)** high-frequency trading architecture. 

The bot connects directly to high-speed Solana WebSocket (WSS) and RPC endpoints, continuously decodes on-chain whale swap transactions in real time, filters signals using momentum and machine learning models, and executes copy trades through Jupiter DEX and GMGN with sub-millisecond hot path decision latency.

---

## ⚡ TRAM Architecture (High-Frequency Trading)

Traditional copy-trading bots read and write JSON or SQLite databases directly on the execution critical path (when entering or exiting trades), introducing 50ms–300ms of disk I/O latency. In fast-moving Solana memecoin markets, this delay leads to severe slippage, front-running, and negative PnL.

The **TRAM (True RAM)** architecture eliminates all disk I/O from the trade path:

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 Solana WSS / RPC Stream                │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                                              ▼
                             ┌─────────────────────────────────┐
                             │       Whale Manager (RAM)       │
                             │  O(1) In-Memory frozenset Match │
                             └────────────────┬────────────────┘
                                              │ Whale Swap Detected
                                              ▼
                             ┌─────────────────────────────────┐
                             │    Filters & Risk Engine (RAM)  │
                             │  Momentum + ML + Pool Share Caps│
                             └────────────────┬────────────────┘
                                              │ Approved
                                              ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │ 🚀 TRAM HOT PATH: 0ms Disk I/O                                                         │
 │                                                                                        │
 │      ┌─────────────────────────┐               ┌───────────────────────────┐           │
 │      │   Jupiter Pre-Warmed    │ ────────────> │   TradingState (In-RAM)   │           │
 │      │   Route & Connection    │               │  Atomic Trade Entry/Exit  │           │
 │      │   Pool (Keepalive HTTP) │               │   Zero Lock Contention    │           │
 │      └─────────────────────────┘               └─────────────┬─────────────┘           │
 └──────────────────────────────────────────────────────────────┼─────────────────────────┘
                                                                │
                                      Decoupled Asynchronous    │ Non-blocking
                                      Background Tasks          ▼
                      ┌───────────────────────────────────────────────────────────────────┐
                      │  • Asynchronous Disk Flush Loop (2s interval, atomic replace)     │
                      │  • Append-Only NDJSON Trade Event Journaling                      │
                      │  • Pre-warmed Token Metadata & Mint Decimals Cache                │
                      │  • Token-Bucket Rate Budget Engine (RPC, DexScreener, Jupiter)    │
                      │  • Hot-Reloadable Settings Manager with Audit Trail               │
                      └───────────────────────────────────────────────────────────────────┘
```

### Key TRAM Performance Optimizations
1. **0ms Hot-Path I/O**: `TradingState` maintains all active positions, wallet balances, and trade records in RAM. Entry and exit operations complete instantaneously in memory without waiting for disk writes.
2. **Decoupled Background Flusher**: State persistence is handled asynchronously every 2 seconds by a background task using atomic file replacement (`tempfile.mkstemp` + `os.replace`), preventing state corruption while never blocking execution.
3. **Shared Connection Pooling**: Singleton `httpx.AsyncClient` with keep-alive connection reuse and strict rate-budget token buckets (DexScreener, Jupiter, Solana RPC) prevents handshake latencies and HTTP 429 rate limit penalties.
4. **Pre-warmed Token Metadata**: As soon as a whale swap is detected, token decimals, pricing, and pool routes are proactively fetched and cached in RAM before order execution begins.
5. **Hot-Reload Settings Engine**: Modify trading parameters on the fly via Web Dashboard or Telegram without restarting the bot. Cached stat checks ensure configuration reads take nanoseconds.

---

## 🌟 Core Features

- **Real-Time On-Chain Whale Tracking**: Subscribes to Solana transaction streams via WebSocket (Helius, Alchemy, QuickNode) and parses swap operations across Raydium (AMM v4, CLMM, CPMM), Orca (Whirlpool), Pump.fun, and Jupiter DEX.
- **Automated Whale Discovery (GMGN.ai)**: In addition to tracking whitelisted addresses, the bot automatically discovers and ranks top-performing smart money wallets with high win rates and 7-day PnL.
- **Dual Trading Engine**:
  - **Paper Mode**: Simulates real Solana market conditions including dynamic Raydium/Jupiter pool fees, network gas, ATA (Associated Token Account) rent creation/reclamation, and slippage.
  - **Live Mode**: Submits real on-chain swap transactions via Jupiter Swap API v6 and GMGN priority routing.
- **Institutional-Grade Risk Management**:
  - Max concurrent trade limits.
  - Trailing stop-loss & dynamic multi-stage take-profit.
  - Max pool share caps (prevents buying more than specified % of liquidity).
  - Maximum allowable price impact limits.
  - Maximum trade timeout unwinding.
  - Global portfolio exposure cap.
  - One-click Emergency Panic button (dumps all open positions immediately).
- **ML & Momentum Filtering**:
  - Pre-trade volume & market cap threshold verification.
  - Anti-pump-and-dump checks (rejects tokens experiencing abnormal 5-minute spikes).
  - Machine learning classification engine for predictive entry validation.
- **Interactive Web Dashboard**: Built with FastAPI, featuring real-time wallet statistics, open positions, PnL charts, and live whale activity feeds.
- **Full Telegram Control**: Control the bot, adjust settings, execute manual trades, inspect whale wallets, and receive instant trade alerts via Telegram inline keyboards.

---

## 📁 Project Structure

```
├── connection_pool.py       # Shared HTTP/RPC connection pool & rate budgeting
├── decoder.py               # Solana transaction stream decoder & DEX parser
├── discovery.py             # Wallet discovery & activity tracker
├── generate_ml_data.py      # Historical trade data extraction for ML training
├── gmgn_discovery.py        # GMGN smart money & whale wallet discovery crawler
├── gmgn_trader.py           # GMGN execution integration
├── interactive_scan.py      # CLI interactive whale scanner
├── jupiter_api.py           # Jupiter DEX v6 API client with pre-warming & caching
├── main.py                  # Bot entry point, startup pre-warming & lifecycle manager
├── ml_engine.py             # Machine learning trade validation model
├── momentum_filter.py       # Pre-trade momentum & volume spike filters
├── requirements.txt         # Project dependencies
├── scanner.py               # Token scanner & security checks
├── settings_manager.py      # Hot-reloadable settings engine with audit history
├── static/                  # Web dashboard UI assets (HTML, CSS, JS)
├── telegram_bot.py          # Telegram bot interface with inline keyboard controls
├── telegram_notifier.py     # Asynchronous Telegram alert dispatcher
├── tests/
│   └── test_tram_architecture.py  # Unit tests for TRAM memory & flusher architecture
├── trade_brain.py           # TRAM TradingState engine, order management & risk caps
├── web_server.py            # FastAPI web dashboard & REST API
├── whale_manager.py         # In-memory whale whitelist/blacklist with O(1) lookups
├── whitelist.json           # Curated seed whale wallet registry
├── .env.example             # Configuration template
└── .gitignore               # Secrets and runtime data exclusions
```

---

## 🚀 Quick Start

### 1. Prerequisites
- **Python 3.10+**
- A **Solana RPC and WSS endpoint** (from [Helius](https://helius.dev/), [Alchemy](https://www.alchemy.com/), or [QuickNode](https://www.quicknode.com/))
- A **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))
- *(Optional)* A **GMGN API Key** for enhanced smart-money discovery

### 2. Clone the Repository
```bash
git clone https://github.com/teckscribe/Solana-whale-trade-bot.git
cd Solana-whale-trade-bot
```

### 3. Create Virtual Environment & Install Dependencies
```bash
python -m venv env
# On Linux/macOS:
source env/bin/activate
# On Windows PowerShell:
.\env\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 4. Configure Environment
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Open `.env` and fill in your configuration:
```ini
# Solana Network
SOLANA_RPC_URL=https://solana-mainnet.g.alchemy.com/v2/YOUR_KEY
SOLANA_WSS_URL=wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# Telegram Control
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789

# Trading Mode
TRADE_MODE=PAPER            # Start in PAPER mode to test safely!
PAPER_BALANCE_USD=25.0      # Starting paper balance in USD
ALLOCATION_PCT=20.0         # % of wallet per trade
TAKE_PROFIT_PCT=15.0        # Take profit target %
STOP_LOSS_PCT=-5.0          # Stop loss trigger %
MAX_CONCURRENT_TRADES=5     # Max simultaneous open positions

# Web Dashboard
WEB_PORT=8101
WEB_AUTH_TOKEN=generate_a_secure_token_here
```

### 5. Run Verification Tests
Verify that all TRAM high-frequency components and in-memory hot paths are operating correctly:
```bash
python -m unittest tests/test_tram_architecture.py
```

### 6. Launch the Bot
```bash
python main.py
```

---

## 📊 Web Dashboard & Remote Access

The bot includes a built-in FastAPI dashboard running at `http://localhost:8101`.

To expose the dashboard securely via **ngrok**:
1. Copy `ngrok.yml.example` to `ngrok.yml` and add your ngrok authtoken.
2. Start ngrok:
   ```bash
   ngrok start --config ngrok.yml wtb_dashboard
   ```
3. Access the dashboard from your browser or mobile device using the public URL.

---

## 📱 Telegram Commands

| Command | Description |
| :--- | :--- |
| `/start` | Open the interactive control menu |
| `/status` | View current trading mode, balance, and open positions |
| `/positions` | List open positions with live unrealized PnL |
| `/whales` | Manage whitelisted and blacklisted whale wallets |
| `/settings` | Hot-reload trading parameters (take-profit, stop-loss, allocation) |
| `/panic` | Emergency dump: immediately market-sells all open positions |
| `/stats` | View lifetime win-rate, total profit, and average holding time |

---

## ⚠️ Risk Disclaimer

*Cryptocurrency trading, especially on decentralized Solana exchanges and memecoins, involves substantial financial risk. Market conditions are extremely volatile. This software is provided for educational and research purposes only. Always thoroughly test in `PAPER` mode before committing real capital. The authors and contributors assume no responsibility for financial losses incurred through the use of this software.*

---

## 📄 License

This project is licensed under the MIT License.
