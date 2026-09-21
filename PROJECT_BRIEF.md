# Solana Whale Trade Bot (WTB)
## Technical Architecture, Engineering & Operations Manual
*A Comprehensive, Textbook-Grade Reference for the Solana High-Frequency Copy-Trading Engine*

---

## Table of Contents

- [Part I: System Overview & Core Philosophy](#part-i-system-overview--core-philosophy)
  - [1. The Solana Copy-Trading Problem](#1-the-solana-copy-trading-problem)
  - [2. The TRAM (Transaction RAM) Architecture](#2-the-tram-transaction-ram-architecture)
  - [3. End-to-End System Dataflow Diagram](#3-end-to-end-system-dataflow-diagram)
- [Part II: Codebase Breakdown — Chapter by Chapter](#part-ii-codebase-breakdown--chapter-by-chapter)
  - [Chapter 1: `main.py` — The Master Supervisor & Lifecycle Manager](#chapter-1-mainpy--the-master-supervisor--lifecycle-manager)
  - [Chapter 2: `scanner.py` — High-Throughput Solana WebSocket Ingestion](#chapter-2-scannerpy--high-throughput-solana-websocket-ingestion)
  - [Chapter 3: `decoder.py` — On-Chain Transaction Parsing & Indexing Lag Defense](#chapter-3-decoderpy--on-chain-transaction-parsing--indexing-lag-defense)
  - [Chapter 4: `trade_brain.py` — The Execution Core & TRAM Memory Engine](#chapter-4-trade_brainpy--the-execution-core--tram-memory-engine)
  - [Chapter 5: `position_state.py` — Finite State Machine & Trailing Stop Engine](#chapter-5-position_statepy--finite-state-machine--trailing-stop-engine)
  - [Chapter 6: `jupiter_api.py` — DEX Aggregator, Dynamic Quoting & Price Oracles](#chapter-6-jupiter_apipy--dex-aggregator-dynamic-quoting--price-oracles)
  - [Chapter 7: `gmgn_trader.py` — Direct GMGN Routing & Execution](#chapter-7-gmgn_traderpy--direct-gmgn-routing--execution)
  - [Chapter 8: `jito_bundle.py` — MEV Protection & Dynamic Tip Submission](#chapter-8-jito_bundlepy--mev-protection--dynamic-tip-submission)
  - [Chapter 9: `momentum_filter.py` & `ml_engine.py` — Signal Filtering & Machine Learning](#chapter-9-momentum_filterpy--ml_enginepy--signal-filtering--machine-learning)
  - [Chapter 10: `whale_manager.py` & Discovery Engines — Smart Money Intelligence](#chapter-10-whale_managerpy--discovery-engines--smart-money-intelligence)
  - [Chapter 11: `settings_manager.py` — Single-Source-of-Truth Hot-Reloading Configuration](#chapter-11-settings_managerpy--single-source-of-truth-hot-reloading-configuration)
  - [Chapter 12: `connection_pool.py` — Persistent HTTP Sessions & Rate Budgeting](#chapter-12-connection_poolpy--persistent-http-sessions--rate-budgeting)
  - [Chapter 13: `telegram_bot.py` & `telegram_notifier.py` — Operations Desk & Push Telemetry](#chapter-13-telegram_botpy--telegram_notifierpy--operations-desk--push-telemetry)
  - [Chapter 14: `web_server.py` & Frontend — Institutional Web Terminal](#chapter-14-web_serverpy--frontend--institutional-web-terminal)
  - [Chapter 15: `backtest_engine.py` — Historical Simulation & Strategy Verification](#chapter-15-backtest_enginepy--historical-simulation--strategy-verification)
- [Part III: System Wiring & Component Interconnection Map](#part-iii-system-wiring--component-interconnection-map)
- [Part IV: Mathematical Models & Risk Formulas](#part-iv-mathematical-models--risk-formulas)
- [Part V: Operational Runbook & Production Deployment](#part-v-operational-runbook--production-deployment)

---

# Part I: System Overview & Core Philosophy

### 1. The Solana Copy-Trading Problem

Copy-trading on the Solana blockchain differs fundamentally from traditional equity or centralized crypto copy-trading. Solana produces blocks every 400 milliseconds, and decentralized exchanges (DEXs) like Raydium, Orca, and Pump.fun run constant-product Automated Market Maker (AMM) bonding curves. 

A naive copy-trader fails due to four compounding micro-structural hurdles:
1. **The Indexing Lag Gap**: When a whale executes a swap, WebSocket nodes broadcast the raw signature before standard RPC nodes have indexed the full transaction body. A direct RPC call immediately after signature receipt returns `null` or `Transaction not found`.
2. **The AMM Slippage & Price Impact Curve**: Whales move markets. If a whale buys $10,000 of an illiquid meme coin, the price spikes. If a bot buys after the whale without checking liquidity, it buys the top of the candle and suffers immediate retracement.
3. **Fixed Network Cost Drag**: Creating an Associated Token Account (ATA) on Solana requires a 0.00204 SOL refundable rent deposit, plus priority fees and Jito MEV tips. On small trade sizes ($2-$5), fixed overhead consumes 10% to 15% of the position immediately upon entry.
4. **Toxic Whales (MEV Arbitragers & Token Deployers)**: A large fraction of high-volume Solana wallets are MEV arbitrage bots (which close trades in the exact same slot) or token developers (`top_dev`) who pump and dump their own tokens. Blindly copying them guarantees 100% loss.

WTB (Solana Whale Trade Bot) is engineered specifically to eliminate these four failure modes.

---

### 2. The TRAM (Transaction RAM) Architecture

Traditional trading bots write position updates, wallet balances, and trade states to disk (`.json` or SQLite) on every polling loop. On Solana, where prices fluctuate second-by-second across multiple active positions, disk I/O causes blocking latencies, file corruption under concurrency, and race conditions.

WTB adopts the **TRAM (Transaction RAM) Architecture**:
- **Hot-Path Zero Disk I/O**: All active trades, unrealized P&L calculations, high-water marks, and paper balances live in volatile RAM guarded by asynchronous locks (`asyncio.Lock` / `Threading.RLock`).
- **Asynchronous State Flushing**: A background task flushes RAM snapshots to disk (`live_trades.json`, `paper_wallet.json`, `data/settings.json`) every 2 seconds via atomic temporary-file replacement (`os.replace`).
- **Crash Resilience**: On startup, `trade_brain.restore_active_trades()` reloads open positions from disk, verifies their age, and re-populates the in-memory state. If a position was opened within the allowable resume window (`MAX_RESUME_AGE_HOURS`), monitoring resumes seamlessly without orphaned orders.

---

### 3. End-to-End System Dataflow Diagram

```
                              ┌───────────────────────────────────┐
                              │     Solana Mainnet Blockchain     │
                              └───────────────┬───────────────────┘
                                              │ WSS logsSubscribe
                                              ▼
┌──────────────────────┐        ┌───────────────────────────┐
│     discovery.py     │───────▶│        scanner.py         │
│  gmgn_discovery.py   │ Updates│  (Whale Signature Filter) │
└──────────────────────┘        └─────────────┬─────────────┘
                                              │ Signature + Whale Pubkey
                                              ▼
                                ┌───────────────────────────┐
                                │        decoder.py         │◀── ConnectionPool
                                │ (Exp. Backoff + Balances) │
                                └─────────────┬─────────────┘
                                              │ Decoded Trade Payload
                                              ▼
                                ┌───────────────────────────┐
                                │      trade_brain.py       │◀── settings_manager.py
                                │   (TRAM State Engine)     │
                                └──────┬──────────────┬─────┘
                                       │              │
                   ┌───────────────────┘              └───────────────────┐
                   ▼                                                      ▼
     [ Risk & Quality Filters ]                             [ Execution Modules ]
     ├── momentum_filter.py (5m surge)                      ├── jupiter_api.py (Quotes/Pricing)
     ├── ml_engine.py (Confidence check)                    ├── gmgn_trader.py (Direct Swaps)
     └── Liquidity & Market Cap Checks                      └── jito_bundle.py (MEV Tip Bundles)
                   │                                                      │
                   └───────────────────┬──────────────────────────────────┘
                                       ▼
                         ┌───────────────────────────┐
                         │     position_state.py     │
                         │ (FSM & Trailing Stop Run) │
                         └─────────────┬─────────────┘
                                       │ Real-time Telemetry & Events
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
      ┌───────────────────────────┐         ┌───────────────────────────┐
      │       web_server.py       │         │      telegram_bot.py      │
      │  (FastAPI Terminal / UI)  │         │  (Ops Desk / Push Alerts) │
      └───────────────────────────┘         └───────────────────────────┘
```

---

# Part II: Codebase Breakdown — Chapter by Chapter

---

## Chapter 1: `main.py` — The Master Supervisor & Lifecycle Manager

### 1.1 Purpose & Role
`main.py` is the central orchestration and lifecycle supervision hub. It boots the asynchronous event loop, instantiates subsystem tasks, monitors task liveness, writes operating-system heartbeats, and guarantees clean, atomic state flushing upon termination.

### 1.2 Key Functions & Structure
- **`main()`**: The root coroutine. Restores orphaned trades into RAM, preloads token metadata, initializes the `WhaleScanner`, and spawns asynchronous worker tasks.
- **`_supervise(named_tasks: dict)`**: A non-blocking supervisor that awaits `asyncio.wait(..., return_when=FIRST_COMPLETED)`. It differentiates between **CORE_TASKS** (`scanner`, `discovery`) and non-core tasks (`web_dashboard`). If a non-core task fails, it logs an error and continues trading; if a core task dies, it triggers a controlled shutdown so systemd can restart the entire service.
- **`write_heartbeat(status: str)` & `heartbeat_loop()`**: Periodically writes `{"pid": os.getpid(), "status": "running", "timestamp": time.time()}` to `data/bot_heartbeat.json`. This provides the FastAPI web dashboard and external watchdogs with sub-second health status.
- **Graceful Shutdown Hook**: The `finally` block of `main()` calls `write_heartbeat("stopped")`, flushes in-memory TRAM state to disk (`flush_all_state_now()`), and closes connection pools (`connection_pool.close_all()`).

### 1.3 Wiring & Interconnections
- **Calls**: `scanner.start()`, `discovery_loop()`, `json_sync_loop()`, `ml_retrain_loop()`, `trade_brain.restore_active_trades()`, `trade_brain.flush_all_state_now()`.
- **Invoked By**: Systemd unit `wtb.service` via `/home/psms/ubuntu/program_files/wtb/venv/bin/python3 main.py`.

---

## Chapter 2: `scanner.py` — High-Throughput Solana WebSocket Ingestion

### 2.1 Purpose & Role
`scanner.py` establishes and maintains a persistent, low-latency WebSocket connection (`SOLANA_WSS_URL`) to listen for on-chain transactions signed by whitelisted whale wallets.

### 2.2 Working Mechanics
- **`WhaleScanner` Class**: Manages the WebSocket lifecycle. It uses `logsSubscribe` with a `mentions` filter targeting whale public keys.
- **Dynamic Wallet Subscription**: Rather than tearing down the connection when new whales are whitelisted, `update_wallets(new_wallets)` dynamically recalibrates the active filter set.
- **Heartbeat / Keepalive**: Implements an aggressive WebSocket ping/pong protocol every 20 seconds. If an RPC provider drops silent frames or encounters network partition, the scanner detects socket stall, closes the dead socket, and initiates exponential reconnect backoff.
- **De-duplication**: Uses an in-memory sliding set of recent transaction signatures (`seen_signatures`) capped at 10,000 entries to prevent duplicate event dispatching when transactions touch multiple monitored accounts.

### 2.3 Wiring
- **Inputs**: Whitelist wallet array from `whale_manager.py`.
- **Outputs**: Dispatches `(signature: str, wallet: str)` to `handle_new_transaction()` in `main.py`.

---

## Chapter 3: `decoder.py` — On-Chain Transaction Parsing & Indexing Lag Defense

### 3.1 Purpose & Role
`decoder.py` converts a raw Solana transaction signature into a structured trade payload: identifying whether the whale bought or sold, the specific SPL token mint, decimals, token amounts, and SOL value.

### 3.2 The Indexing Lag Problem & Retry Backoff
When Solana's WebSocket sends a signature, the transaction is confirmed in a slot, but the RPC node's JSON-RPC database frequently lags behind by 500ms to 2000ms. Calling `getTransaction` immediately yields `null`.

`decoder.py` solves this using **Multi-Stage Exponential Backoff with Jitter**:
- `MAX_RETRIES = 10`
- `INITIAL_SLEEP = 1.5` (Initial indexing buffer)
- `BASE_BACKOFF = 1.5` (Exponential growth multiplier)
- `JITTER_RANGE = 0.5` (Random jitter to prevent RPC hammering)

If the primary private RPC continues to return `null` after multiple retries, `decoder.py` seamlessly fails over to `create_fallback_rpc_client()` (Solana Public Mainnet RPC) to fetch the transaction body before abandoning the signal.

### 3.3 Balance Parsing Engine
- **`decode_transaction(signature, whale_wallet)`**: Fetches `getTransaction(..., max_supported_transaction_version=0, encoding="jsonParsed")`.
- Inspects `meta.preTokenBalances` and `meta.postTokenBalances`.
- Identifies net delta:
  $$\Delta 	ext{Token} = 	ext{postAmount} - 	ext{preAmount}$$
  $$\Delta 	ext{SOL} = 	ext{preLamports} - 	ext{postLamports}$$
- If $\Delta 	ext{Token} > 0$ and $\Delta 	ext{SOL} < 0$, the transaction is classified as a **BUY**.
- Normalizes token amounts using on-chain decimals extracted directly from `tokenAmount.decimals`.

### 3.4 Signer Provenance Verification (Anti-Spoofing)
Adapted from the FOMO Radar provenance principles, `decoder.py` inspects `message.account_keys` and transaction header permissions:
- Confirms the target `whale_wallet` was an **authorized transaction signer** (`message.is_signer(whale_index)`).
- Rejects unsolicited airdrops, third-party router injections, or multi-hop liquidity routings where the whale was merely a token recipient without signing authority.

### 3.5 Telemetry
Tracks `total_attempts`, `success`, `failed_after_retries`, and `fallback_success` in `_decode_stats`, queryable via `get_decode_stats()` for system monitoring.

---

## Chapter 4: `trade_brain.py` — The Execution Core & TRAM Memory Engine

### 4.1 Purpose & Role
`trade_brain.py` is the largest and most critical module (88 KB). It houses the **TRAM in-memory state engine**, performs risk pre-flight checks, derives execution sizing, launches autonomous position monitors, and settles books upon exit.

### 4.2 Core Data Structures: `TradingState` (TRAM)
```python
class TradingState:
    active: Dict[str, Position]    # Live open positions keyed by token mint
    processing_tokens: Set[str]    # Deduplication lock to prevent re-entrant buys
    wallet_balance: float          # Available free cash (USD)
    initial_balance: float         # Starting portfolio baseline (USD)
    closed_trades: List[dict]      # Volatile FIFO queue of exited trades
    token_buy_history: Dict[str, list]  # Rolling window for wave detection
    seeded_blacklist: Set[str]     # Auto-quarantined seeder wave targets
    _lock: asyncio.Lock            # Concurrency barrier
```
- **Zero Disk Latency**: Reading open exposure (`get_open_exposure_usd()`), allocating capital (`debit_balance()`), and updating prices happen entirely in memory.
- **Periodic Snapshot Loop (`_live_dashboard_loop`)**: Runs every 2 seconds. Flushes `STATE.snapshot_active()` to `live_trades.json` or `paper_trades.json` and appends completed trades to `ml_training_data.jsonl` using append-only atomic writes.

### 4.3 Trade Sizing & Fee Mathematics
`trade_brain.py` computes position size dynamically:
$$\text{Trade Size} = \min\left(\text{wallet\_balance} \times \frac{\text{ALLOCATION\_PCT}}{100}, \text{MAX\_EXPOSURE}\right)$$
It calculates fixed round-trip network costs:
$$\text{Fixed Cost (SOL)} = 2 \times (\text{SOL\_BASE\_TX\_FEE} + \text{PRIORITY\_FEE\_SOL} + \text{TIP\_FEE\_SOL}) + \text{ATA\_RENT\_SOL}$$
On a $25 paper wallet with 15% allocation ($3.75), fixed costs represent over 10% of the trade. The strategy requires `ALLOCATION_PCT` $\ge 40-50\%$ to keep fixed overhead below $2.5\%$.

### 4.4 Position Monitor Loop (`monitor_position`)
When a trade is executed, an independent asynchronous task (`monitor_position`) is spawned for that specific token:
1. Polls real-time price from `jupiter_api.get_live_price(token)` every `POLL_INTERVAL` seconds.
2. Updates `Position` object in TRAM state.
3. Evaluates exits:
   - **Take-Profit**: `profit_pct >= TAKE_PROFIT_PCT`
   - **Stop-Loss**: `profit_pct <= STOP_LOSS_PCT`
   - **Trailing-Stop**: Triggered by retracement from high-water mark.
   - **Timeout**: `elapsed >= MAX_HOLD_SECONDS` (if `TIMEOUT_ENABLED` is true).
   - **Dead Volume**: 10 minutes of zero price movement.
   - **External Panic Sell**: Watches `panic.json` file.
4. On exit trigger: Calls `close_trade()` (paper) or `execute_gmgn_sell_all()` (live).

### 4.5 Anti-Seeder Wave Attack Defense
Adapted from `fomo-robinhood-radar`'s `provenance.py`:
- **Attack Vector**: Scam deployers script sequential micro-buys ($0.01–$0.15 SOL) across 2+ whitelisted whale addresses within 30 to 300 seconds to trigger copy-bot consensus.
- **Detector (`detect_seeder_wave`)**: Tracks rolling buy sizes and inter-arrival times per token. If $\ge 2$ whitelisted wallets enter with small/uniform amounts ($\le 0.20$ SOL) or tight queue gaps ($\le 45$s), the token is classified as a programmatic seeder attack.
- **Autonomous Quarantining**: Immediately rejects the trade and adds the mint to `data/seeded_blacklist.json`. Future trades on this mint are rejected with zero overhead.

---

## Chapter 5: `position_state.py` — Finite State Machine & Trailing Stop Engine

### 5.1 Purpose & Role
`position_state.py` provides formal Finite State Machine (FSM) guarantees for every open position, preventing race conditions, accidental double-sells, or stale order execution.

### 5.2 State Machine Graph
```
   [ OPEN ] ──────────────(Profit >= Activation %)─────────────▶ [ TRAILING_PROFIT ]
      │                                                                  │
      │ (Stop-Loss / Take-Profit / Timeout)                              │ (Retracement >= Callback %)
      ▼                                                                  ▼
 [ PENDING_EXIT ] ◀──────────────────────────────────────────────────────┘
      │
      ├──(Sell Order Confirmed)──▶ [ CLOSED ]
      └──(Sell Order Failed)─────▶ [ OPEN ] (Revert exit)
```

### 5.3 High-Water Mark & Trailing Stop Algorithm
- **Peak Tracking**: When `current_price > high_water_mark_price`, the peak updates.
- **Activation**: If `profit_pct >= trailing_activation_pct` (e.g., +8.0%), `trailing_stop_active` becomes `True`, transitioning the position to `TRAILING_PROFIT`.
- **Retracement Trigger**:
  $$	ext{Retracement \%} = rac{	ext{high\_water\_mark\_price} - 	ext{current\_price}}{	ext{high\_water\_mark\_price}} 	imes 100$$
  If $	ext{Retracement \%} \ge 	ext{trailing\_callback\_pct}$ (e.g., 4.0%), an exit is triggered immediately to bank gains.

---

## Chapter 6: `jupiter_api.py` — DEX Aggregator, Dynamic Quoting & Price Oracles

### 6.1 Purpose & Role
Integrates Jupiter Aggregator V6 to fetch optimal multi-hop swap routes, compute real executable fill prices (including AMM impact), retrieve token decimals, and query real-time SOL/USD exchange rates.

### 6.2 The Executable Fill vs. Mid-Price Distinction
- **`get_live_price(token_address)`**: Queries DexScreener mid-market price. Cheap, fast, and polled every 2 seconds to decide **WHEN** to exit.
- **`get_swap_quote(input_mint, output_mint, amount_lamports)`**: Queries the real Jupiter V6 routing API. It computes actual token output accounting for pool liquidity, slippage, and AMM constant-product price impact.
- **`price_from_quote(...)`**: Derives the true executable entry price:
  $$	ext{Executable Price} = rac{	ext{SOL In} 	imes 	ext{SOL Price (USD)}}{	ext{Tokens Out}}$$
  This prevents the bot from filling at an imaginary mid-market price when buying large sizes in thin pools.

### 6.3 Decimals Cache & Preloading
Decimals are required to convert integer token lamports into float quantities. `get_token_decimals(token_address)` queries RPC metadata with a persistent in-memory cache (`_decimals_cache`). On startup, `preload_token_metadata()` pre-populates decimals for all restored active positions, eliminating cold-start RPC latency.

---

## Chapter 7: `gmgn_trader.py` — Direct GMGN Routing & Execution

### 7.1 Purpose & Role
When `TRADE_MODE` is set to `LIVE`, `gmgn_trader.py` handles live transaction execution through the GMGN router, optimized for sub-second memecoin trading on Solana.

### 7.2 Working Mechanics
- **`execute_gmgn_buy(token_address, amount_sol)`**: Constructs the buy transaction payload, injects slippage parameters, applies anti-MEV protection via Jito, signs the transaction with the user's private key, and submits to the network.
- **`execute_gmgn_sell_all(wallet_address, token_address)`**: Queries the wallet's real on-chain SPL token balance and dispatches a 100% liquidation swap transaction.
- **Confirmation Tracker**: Polls signature status using connection-pooled RPC endpoints until finalized on-chain or timed out.

---

## Chapter 8: `jito_bundle.py` — MEV Protection & Dynamic Tip Submission

### 8.1 Purpose & Role
Solana meme-coin trading is heavily plagued by malicious MEV searchers running sandwich attacks and front-running bots. `jito_bundle.py` routes live transactions through **Jito Block Engines** as private bundles rather than public mempool gossip.

### 8.2 Architecture & Dynamic Tip Floors
- **Multi-Region Endpoints**: Connects to Jito block engines across `mainnet`, `amsterdam`, `frankfurt`, `ny`, and `tokyo`.
- **Dynamic Tip Floor API**: Queries Jito's live `/api/v1/bundles/tip_floor` endpoint. Rather than paying a fixed static tip, `get_dynamic_tip_lamports(percentile)` retrieves real-time network tip percentiles:
  - `p25` (Economy)
  - `p50` (Standard / Recommended)
  - `p75` (Aggressive / High Congestion)
  - `p95` (Ultra-Fast Priority)
- **Atomic Bundling**: Assembles the trade transaction and tip transfer instruction into an atomic bundle. Either both succeed, or the entire bundle is dropped, completely neutralizing sandwich losses.

---

## Chapter 9: `momentum_filter.py` & `ml_engine.py` — Signal Filtering & Machine Learning

### 9.1 Momentum Filter (`momentum_filter.py`)
Protects against buying tokens at the top of an over-extended pump candle.
- Inspects 5-minute price change (`m5_pump_pct`), 1-hour volume, and buy/sell transaction count ratios from DexScreener.
- If `m5_pump_pct > MAX_M5_PUMP_PCT` (e.g. 80%), the trade is rejected:
  ```python
  if m5_pump > MAX_M5_PUMP_PCT:
      log.warning(f"Momentum Filter REJECTED {token[:8]}: +{m5_pump:.1f}% in 5m exceeds max {MAX_M5_PUMP_PCT}%")
      return False
  ```

### 9.2 Machine Learning Classifier (`ml_engine.py`)
- **Model**: Scikit-Learn `RandomForestClassifier`.
- **Dataset**: Trained on historical closed trade outcomes in `ml_training_data.json`.
- **Feature Vector**:
  $$X = [	ext{whale\_win\_rate}, 	ext{whale\_historical\_trades}, 	ext{trade\_size\_usd}, 	ext{hour\_of\_day}, 	ext{day\_of\_week}]$$
- **Prediction (`predict_trade`)**: Outputs probability $P(	ext{Win})$. If $P(	ext{Win}) 	imes 100 < 	ext{ML\_CONFIDENCE}$ (e.g. 40%), the trade is vetoed before capital is allocated.

---

## Chapter 10: `whale_manager.py` & Discovery Engines — Smart Money Intelligence

### 10.1 Whale Database & Cache Hierarchy
Manages whale categorization:
- **`WHITELIST`**: Whales actively approved for copy-trading.
- **`NEUTRAL`**: Discovered wallets undergoing incubation/monitoring.
- **`BLACKLIST`**: Wallets permanently banned (bots, devs, failed performers).

### 10.2 Activity Aging & Auto-Pruning
Whales that stop trading or lose profitability are automatically pruned:
- Every trade updates `last_active`.
- `get_active_whales(max_age_hours=24)` returns only wallets active within the specified time window, preventing the WebSocket scanner from overloading connections on inactive addresses.

### 10.3 Discovery Engines
- **`discovery.py`**: Scrapes recent high-volume Raydium pools on Solana RPC to discover wallets achieving $>5\times$ returns.
- **`gmgn_discovery.py`**: Queries GMGN's Smart Money API. Filters out wallets matching banned tags:
  ```python
  BANNED_TAGS = {'arbitrager', 'top_dev', 'sniper', 'smart_degen', 'kol', 'pump_dump'}
  ```
  Wallets passing the filter are queued and sent via Telegram for user approval.

### 10.4 AI Whale Scoring & Vetting Engine (`ai_whale_scorer.py`)
Adapted from `fomo-robinhood-radar`'s `pipeline/score.py`:
- **Architecture**: Dual-mode auditor that evaluates smart money performance using either LLMs (Google Gemini API via `gemini-2.5-flash`) or an offline deterministic heuristic gauntlet.
- **Core Metrics Audited**: 7-day win rate, 7-day trade cadence, realized profit, hold patterns, and metadata tags.
- **Red Flag Identification**:
  - `toxic_dev`: Token deployers who pump and dump their own mints (hard cap $\le 20$ score, immediate `BLACKLIST`).
  - `mev` / `bot`: High-frequency scalpers ($>200$ trades/7d or bot tags) causing toxic slippage (hard cap $\le 30$ score, immediate `BLACKLIST`).
  - `one-hit`: Enormous headline PnL on $<5$ total trades (demoted to `WATCH`).
- **Autonomous Whitelist Pruning**: `whale_manager.audit_whitelist(auto_prune=True)` evaluates all active whitelisted wallets and automatically demotes any wallet scoring $<45$ to `discovery_db.json` with `BLACKLIST` status.
- **Offline Human-in-the-Loop CLI**:
  - `python ai_whale_scorer.py --export pending.json`: Exports compact prompt payloads for pasting into web LLM chats (ChatGPT/Claude/Gemini Web) at $0 cost.
  - `python ai_whale_scorer.py --import-file scored.json`: Applies manual or web LLM evaluation results directly back into the database.

---

## Chapter 11: `settings_manager.py` — Single-Source-of-Truth Hot-Reloading Configuration

### 11.1 Purpose & Role
Provides centralized, validated, hot-reloadable configuration across all processes without requiring service restarts.

### 11.2 Specification & Validation Engine
Every setting is declared in `SPEC` with type coercion, minimum/maximum bounds, and categories:
```python
_s("STOP_LOSS_PCT", "float", -12.0, min_val=-50.0, max_val=-0.5, group="Risk")
_s("ALLOCATION_PCT", "float", 50.0, min_val=1.0, max_val=100.0, group="Risk")
```
- **Coercion**: String/bool/float conversions are strictly enforced.
- **Storage**: Persisted to `data/settings.json` and mirrored to `.env`.
- **Audit Logging**: Every change is appended to `data/settings_history.jsonl` with timestamp, source (`web_dashboard`, `telegram`, `cli`), old value, and new value.

### 11.3 1-Second Cache Invalidation
`settings_manager` checks `os.stat(SETTINGS_FILE)` every 1.0 second. If the file modification time (`st_mtime`) changes (e.g. user edited via `nano`), its internal RAM cache invalidates immediately. The trading engine, web server, and Telegram bot pick up updates within 1 to 2 seconds.

---

## Chapter 12: `connection_pool.py` — Persistent HTTP Sessions & Rate Budgeting

### 12.1 Purpose & Role
Prevents TCP socket exhaustion, eliminates SSL handshake overhead, and enforces requests-per-minute (RPM) rate limits across external APIs.

### 12.2 Rate Budget System (`RateBudget`)
Implements sliding-window token bucket throttling:
```python
class RateBudget:
    max_requests_per_minute: int
    soft_ceiling_pct: float = 0.8  # 80% soft limit for background tasks
```
- **`acquire(priority='normal')`**: Normal priority calls throttle when reaching 80% of budget to reserve buffer space for critical execution calls.
- **`acquire(priority='high')`**: Sell and stop-loss execution calls access 100% of hard limit.

### 12.3 Managed Budgets & Persistent Clients
- **Budgets**: `jupiter` (600 RPM), `dexscreener` (300 RPM), `rpc` (1200 RPM), `coingecko` (50 RPM).
- **Session Pooling**: Manages a shared `httpx.AsyncClient` with `limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)`.

---

## Chapter 13: `telegram_bot.py` & `telegram_notifier.py` — Operations Desk & Push Telemetry

### 13.1 Telegram Operations Desk (`telegram_bot.py`)
An interactive command-and-control bot utilizing `python-telegram-bot`:
- **`/status`**: Real-time portfolio valuation, active trade counts, cash balance, and bot uptime.
- **`/positions`**: Visual listing of all open trades with entry price, current price, and floating P&L.
- **`/settings` & `/set <key> <val>`**: View and modify hot-reloadable parameters on the fly.
- **`/mode`**: Toggle between `PAPER` and `LIVE` trading modes.
- **`/reset`**: Reset simulated paper wallet balance to starting baseline.
- **Inline Discovery Buttons**: Displays newly discovered GMGN smart money wallets with `[ Approve Whitelist ]` or `[ Reject ]` buttons.

### 13.2 Push Alerting System (`telegram_notifier.py`)
Dispatches rich HTML notifications to the administrator channel:
- 🚀 **Buy Signal Alerts**: Whale wallet, token address, trade size, and DexScreener links.
- 🎯 **Take-Profit / Trailing Stop Alerts**: Banked dollar profit, return percentage, and hold time.
- 🛑 **Stop-Loss Alerts**: Exit trigger reason and liquidated loss amount.
- 🚨 **System Crash & Error Warnings**: Core task failure diagnostics.

---

## Chapter 14: `web_server.py` & Frontend — Institutional Web Terminal

### 14.1 Backend Architecture (`web_server.py`)
A high-speed FastAPI service running on port 8101:
- **Authentication**: Ngrok / Public access protected by Bearer token header / session cookie (`.web_auth_token`).
- **Endpoints**:
  - `GET /api/dashboard`: Mode, portfolio valuation, cash, realized net P&L, floating P&L, active trades list, and scanner liveness.
  - `GET /api/history`: Audited ledger of closed trades, win rate, and CSV export streaming.
  - `GET /api/whales`: Whitelist and discovery roster with copy-trading win rates.
  - `GET /api/settings` & `POST /api/settings`: Read and update configuration keys.
  - `GET /api/log-info` & `GET /api/download/bot_debug.log`: Diagnostic log inspector and streaming download.

### 14.2 Scanner Health & Liveness Detector
`web_server.py` evaluates true scanner status through a multi-tier probe (`get_scanner_status()`):
1. **Heartbeat File**: Verifies `data/bot_heartbeat.json` timestamp freshness (< 12 seconds) and verifies OS PID liveness via `os.kill(pid, 0)`.
2. **Systemd Probe**: Queries `systemctl is-active --quiet wtb` directly.
3. **Process Probe**: Scans active process table for `main.py`.

### 14.3 Frontend Single-Page Application (SPA)
Located in `static/` (`index.html`, `style.css`, `app.js`):
- **Tab 1: Live Trading Desk**: Portfolio cards (Cash + Open breakdown), active position cards, table view toggle, and real-time P&L tickers.
- **Tab 2: Trade History & P&L**: Closed trade ledger with UTC timestamps, holding durations, exit triggers, and 1-click CSV export.
- **Tab 3: Whale Analyze**: Wallet directory with win rates, tracked swaps, copy profit, and direct Solscan / GMGN links.
- **Tab 4: Settings & Log**: In-line parameter editor with ✏️ pen-icon confirmation modals, hot-reloading diagnostics, and log download.
- **Dynamic Header Status**: Real-time pulsing green `Online` pill when scanner is running; turns solid red `Offline` when stopped.

---

## Chapter 15: `backtest_engine.py` — Historical Simulation & Strategy Verification

### 15.1 Purpose & Role
Enables quantitative backtesting of strategy parameters without risking capital by replaying historical trade datasets against alternative Take-Profit, Stop-Loss, and Trailing-Stop configurations.

### 15.2 Working Mechanics
- Ingests `ml_training_data.json` or custom simulated price paths.
- Reconstructs execution fills including configurable DEX slippage and fixed Solana network fees.
- Simulates tick-by-tick price evolution and high-water mark trailing stops.
- Computes comprehensive portfolio performance analytics:
  - Total Return ($ and %)
  - Maximum Drawdown (MDD %)
  - Profit Factor ($rac{\sum 	ext{Gains}}{\sum 	ext{Losses}}$)
  - Expectancy Per Trade ($)

---

# Part III: System Wiring & Component Interconnection Map

### Component Dependency & Communication Matrix

| Source Component | Calls / Depends On | Communication Method | Data Exchanged |
| :--- | :--- | :--- | :--- |
| **`main.py`** | `scanner.py`, `decoder.py`, `trade_brain.py` | Asynchronous function calls | Signatures, trade signals, task handles |
| **`scanner.py`** | `whale_manager.py`, Solana WSS | WebSocket / In-memory | Subscription filters, raw tx signatures |
| **`decoder.py`** | `connection_pool.py`, Solana RPC | JSON-RPC (HTTP) | Parsed transaction logs, token balance deltas |
| **`trade_brain.py`** | `position_state.py`, `jupiter_api.py`, `settings_manager.py` | In-memory TRAM / Async Lock | Position objects, quotes, risk parameters |
| **`gmgn_trader.py`** | `jito_bundle.py`, GMGN API | HTTPS POST / Signed TX | Swap routes, private MEV tip bundles |
| **`web_server.py`** | `settings_manager.py`, JSON files | Local disk / Process probe | Heartbeat status, JSON snapshots, setting updates |
| **`telegram_bot.py`** | `settings_manager.py`, `whale_manager.py` | Async function calls / JSON | Commands, whale approvals, wallet resets |

---

# Part IV: Mathematical Models & Risk Formulas

### 1. The Fixed Network Fee Drag & Break-Even Hurdle

Every trade cycle on Solana has fixed non-negotiable costs:
- $\text{ATA Rent} = 0.00204\text{ SOL}$
- $\text{Base Gas} = 0.000005\text{ SOL}$
- $\text{Priority Fee} = 0.00001\text{ SOL}$
- $\text{Jito Tip} = 0.00001\text{ SOL}$
- $\text{Round-Trip Fixed SOL} = 2 \times (\text{Gas} + \text{Priority} + \text{Tip}) + \text{Rent} \approx 0.00209\text{ SOL}$

At $150/SOL, fixed cost $C_{\text{fixed}} \approx \$0.31\text{ USD}$.

$$\text{Fixed Drag \%} = \frac{C_{\text{fixed}}}{\text{Trade Size}} \times 100$$
$$\text{Total Break-Even Hurdle \%} = \text{Fixed Drag \%} + 2 \times \text{DEX Fee \%}$$

| Trade Size | Fixed Drag (%) | Swap Fee (2% Round-Trip) | Required Gain to Break Even |
| :---: | :---: | :---: | :---: |
| **$2.00** | 15.5% | 2.0% | **+17.5%** |
| **$5.00** | 6.2% | 2.0% | **+8.2%** |
| **$12.50** | 2.4% | 2.0% | **+4.4%** |
| **$50.00** | 0.6% | 2.0% | **+2.6%** |
| **$100.00** | 0.3% | 2.0% | **+2.3%** |

*Engineering Rule*: A trade size below $10.00 on Solana creates a mathematically hostile break-even hurdle exceeding 5%. `ALLOCATION_PCT` must be calibrated to ensure trade size $\ge \$12.50$.

---

### 2. Stop-Loss vs. AMM Bid-Ask Spread Dynamics

On constant-product AMM bonding curves ($x \cdot y = k$):
$$\text{Price}_{\text{Ask}} = \text{Price}_{\text{Mid}} \times (1 + \text{Slippage} + \text{Impact} + \text{Fee})$$

On low-cap meme coins, the effective spread between the executable buy quote and the mid-market price is **3% to 8%**.
If $\text{STOP\_LOSS\_PCT} = -5.0\%$:
$$\text{Immediate Return at Entry} = \frac{\text{Price}_{\text{Mid}} - \text{Price}_{\text{Ask}}}{\text{Price}_{\text{Ask}}} \approx -4\%\text{ to }-7\%$$
Because the initial mark-to-market is already at or below -5%, the bot triggers an immediate stop-loss exit within 1 to 2 loops (2-5 seconds).
*Calibrated Rule*: $\text{STOP\_LOSS\_PCT}$ must be set to $\le -12.0\%$ to absorb normal AMM entry variance.

---

# Part V: Operational Runbook & Production Deployment

### 1. Systemd Service Architecture

The system runs as four decoupled systemd daemon units on Ubuntu Linux:

```
                  ┌─────────────────────────────────────┐
                  │          systemd init (1)           │
                  └──────────────────┬──────────────────┘
            ┌────────────────┬───────┴────────┬────────────────┐
            ▼                ▼                ▼                ▼
     [ wtb.service ]  [ wtb-web.service ]  [ wtb-bot.service ]  [ wtb-ngrok.service ]
       (main.py)      (web_server.py)    (telegram_bot.py)      (ngrok tunnel)
```

1. **`wtb.service`**: The core trading engine and scanner.
2. **`wtb-web.service`**: The FastAPI backend serving the web terminal on port 8101.
3. **`wtb-bot.service`**: The Telegram bot interface and push alerting worker.
4. **`wtb-ngrok.service`**: The secure public reverse proxy exposing port 8101.

### 2. Common Administration Commands

#### Service Management
```bash
# Check status of all bot components
sudo systemctl status wtb wtb-web wtb-bot wtb-ngrok --no-pager

# Restart entire bot stack after code updates
sudo systemctl restart wtb wtb-web wtb-bot

# Stop scanner only (web terminal will reflect "Offline")
sudo systemctl stop wtb
```

#### Diagnostic Log Monitoring
```bash
# Stream live logs from trading engine
journalctl -u wtb -f -o cat

# View last 100 log lines of web server
journalctl -u wtb-web -n 100 --no-pager

# Stream Telegram bot logs
journalctl -u wtb-bot -f
```

#### Updating Codebase
```bash
cd ~/ubuntu/program_files/wtb
git checkout -- whitelist.json   # Discard local runtime activity timestamps
git pull origin main             # Pull verified release
sudo systemctl restart wtb wtb-web wtb-bot
```

---
*Document Version: 5.0 (Institutional Desk Edition)*  
*Maintained by: Advanced Algorithmic Agent Architecture Team*
