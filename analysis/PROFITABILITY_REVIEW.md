# WTB profitability review

Reviewed September 30, 2026. Local evidence covers recorded trades from September 18–25 and available logs through September 29.

## Assessment

The project provides a useful foundation for observing Solana wallets and testing copy trades, but it has not demonstrated a repeatable trading edge. The best next investment is reliable execution, consistent accounting, and prospective measurement of which wallets remain profitable after the bot's delay and costs. Increasing allocation or enabling the current ML filter would not establish that edge.

This review did not change trading code, settings, the whitelist, or running services. The whitelist already had local changes. Analysis files are the only additions.

## What the bot actually does

`scanner.py` receives wallet transaction signatures; `decoder.py` fetches confirmed transactions and infers swaps from balance changes, with a signer provenance check. `trade_brain.py` screens whitelisted buys, checks token metadata and Jupiter buy/sell routes, sizes positions, and starts monitors. Paper entries and exits use Jupiter quotes. Live execution uses GMGN CLI and its condition orders. DexScreener prices drive local exit triggers. Completed trades are buffered and written to JSONL and a JSON mirror. The web and Telegram interfaces manage configuration and monitoring.

Useful existing controls include duplicate-token protection, explicit token decimals, buy price-impact and pool-share caps, sell-route checks, provenance checks, whale-sell exits, and a position state machine. Their presence does not establish that the selected signals are profitable.

## Recorded results

Source: `ml_training_data.jsonl`. Reproduce with `.venv\Scripts\python.exe analysis\profitability_snapshot.py`.

| Metric | Recorded result |
|---|---:|
| Completed trades | 159 |
| Trading mode | All PAPER |
| Confirmed real-fill records | 0 |
| Winners / losers | 27 / 132 |
| Win rate | 16.98% |
| Sum of recorded net P&L | -$265.87 |
| Profit factor: dollar gains / dollar losses | 0.388 |
| Mean return per trade | -5.96% |
| Median return per trade | -5.52% |
| Trades marked TAKE_PROFIT that lost money | 22 of 39 |

These results mix settings, position sizes, and balance resets. The loss is not a return on today's configured $50 balance. They also include earlier logic; current defenses must be evaluated separately.

The last six trades, on September 24–25, total +$5.70. That sample is too small to establish an improvement. The current whitelist contains 43 wallets; only three have observations in this dataset, totaling eight historical trades. The large historical losers are already absent from that whitelist. Current wallet selection is therefore mostly unvalidated by the bot's own recorded outcomes.

The September 28 archived log and the inspected current log contain no decoded swap signals or new paper entries, while recording many provenance rejections. This shows an inactive signal funnel in those files; it does not prove the provenance filter is wrong. Test it against known genuine swaps and injected transfers before changing it.

## Priorities

### 1. Fix live order lifecycle before using real capital

In `trade_brain.py:572`, several branches of `monitor_gmgn_position()` call the close recorder and stop monitoring even when `execute_gmgn_sell_all()` has not confirmed. This occurs in fallback exits, timeout exits, whale-sell exits, and panic exits. The wallet can retain a position after the bot removes it from its exposure accounting.

Other related risks:

- `restore_active_trades()` drops positions older than the configured two-hour resume limit without reconciling on-chain holdings (`trade_brain.py:335`). Age does not prove a live position has closed.
- A buy that exceeds its confirmation wait is abandoned locally, with an alert claiming no capital was lost. A timeout cannot establish that; reconcile pending order IDs and transaction signatures.
- `_run_gmgn_cli()` retries the command through a second launch after any exception, including a timeout (`gmgn_trader.py:51`). For swaps, the first process may already have submitted the trade. Reconcile before resubmitting, and terminate/reap timed-out subprocesses correctly.

Implement explicit submitted, pending, confirmed, failed, and reconciliation-needed states. Keep exposure reserved until the chain proves settlement. Persist order intents and identifiers so restart recovery can discover late fills. Add mocked tests for unconfirmed sells, late buys, subprocess timeouts, and restart recovery.

### 2. Make P&L and exit decisions use consistent quantities

Paper triggers use DexScreener mid prices, while settlement uses Jupiter executable quotes. Historical TAKE_PROFIT losses demonstrate the practical importance of this distinction. Current phantom-profit checks improve reporting, but refusing a trailing exit solely because it would realize a loss can leave a deteriorating position open.

Use net liquidation value for risk decisions: expected proceeds from selling the actual quantity, less remaining execution costs, compared with the actual entry cash outflow. Track the trailing high-water mark on the same measure. A risk exit should remain available when liquidation value falls below entry cost.

Separate these fields:

- Entry cash paid, token quantity acquired, and refundable account rent locked.
- Gross exit proceeds and each explicit fee.
- Net realized P&L and remaining inventory.
- Quote-based estimates versus confirmed transaction cashflows.

In 157 records, `real_exit_proceeds_usd - real_entry_cost_usd` differs from `net_profit_usd` by more than one cent. The current paper logic charges entry fees inside net P&L while also describing an entry-fee-inclusive cost in a separate field. This is a schema reconciliation problem; it is not proof of an additional actual cash loss.

`get_swap_quote()` returns `None` for both unavailable routes and transport/API failures. `close_trade()` can turn that into a -100% unsellable loss for non-profit exits. Distinguish API unavailable, no route, stale quote, and confirmed unsellable status. An outage should not silently manufacture a completed trade.

### 3. Validate whales by the follower's returns

GMGN leader win rate and profits are discovery inputs. They do not measure what this bot earns after entering later at a different price.

Record prospective shadow trades for candidate wallets and rank them by net follower return, uncertainty, holding time, entry delay, liquidity, and loss concentration. Use consistent risk or notional normalization so wallet rankings are not dominated by changing paper balances. Treat wallets with few observations as unproven; use pooled estimates and shrink uncertain results toward the overall average.

The historical `6FpZdYmB…` wallet produced 28 copies, one winner, and -$62.54. It is already absent from the current whitelist. That is a useful example of why displayed leader profitability is insufficient, not a recommendation to blacklist it again.

There is a concrete feedback-data gap: `ai_whale_scorer.py:68` and `ml_engine.py:19` read `ml_training_data.json`, which is empty locally; the 159 records are in JSONL. Unify all analytics behind one versioned, deduplicated ledger before relying on automated rankings.

### 4. Replace synthetic-path optimization

`TickSynthesizer` (`backtest_engine.py:158`) constructs an entry-to-peak-to-exit trajectory from completed-trade summaries. The true ordering of drawdowns and peaks is unknown. Some peaks are measured using mid-price triggers while endpoints are executable quotes, adding a second inconsistency.

`BacktestEngine.run()` also treats trades sequentially, disregarding overlapping capital requirements. `max_hold_seconds` and start/end filter fields are declared but not applied in the replay loop. Resizing past trades does not reprice their historical market impact. These limitations make the optimizer unsuitable for selecting live parameters.

Record timestamped, size-specific bid/ask quotes and observed signals, including rejected signals. Replay only information available at each decision time, with realistic delay, overlapping positions, failed orders, and costs. Evaluate on later periods excluded from tuning. Group uncertainty estimates by day and token; many correlated copies are not independent evidence.

### 5. Remove unvalidated consensus privileges

`trade_brain.py:1509` onward skips momentum checks for two-wallet consensus and increases paper allocation up to 50%. Multiple wallets may share an owner, copy one another, or react to the same already-exhausted move. Distinct addresses do not prove independent information.

Apply all hard risk gates to consensus entries. Keep their size unchanged while measuring incremental net performance. Check common funding and trading patterns before treating wallet counts as independent confirmation. Paper and live sizing should use the same rule; live sizing currently uses ordinary `ALLOCATION_PCT` instead of the paper consensus allocation.

### 6. Measure and constrain delay and execution cost

`decoder.py:59` deliberately waits 1.5 seconds before its first RPC attempt and may then retry for much longer. Entry also makes several API calls, and GMGN live execution launches a subprocess. RAM-only state access is only one small part of total signal-to-fill latency.

Record block time/slot, receipt time, decode completion, quote time, submission, and confirmation. Measure median and tail latency, leader-to-follower price deterioration, and how forward returns change with delay. Reject stale buy signals using a tested age limit; retain timely risk-exit handling. Prefer wallets whose profitable holding periods are long enough to survive measured copy delay.

Before entry, use fresh buy and full-size sell quotes to estimate the immediate round-trip loss. Skip trades whose expected advantage cannot cover that loss and a conservative execution allowance. Measure sell-side impact as well as buy-side impact. A sell quote alone is not a complete token-security check; inspect relevant mint/freeze authority and Token-2022 restrictions.

Paper adds 1% per leg to quote-based fills, assumes account rent is reclaimed, and derives fixed tips from an environment setting while GMGN uses `JITO_TIP_SOL`. Audit which costs are already embedded in quotes and reconcile both engines against transaction cashflows. Track rent as locked capital until it is actually refunded. Do not remove fees merely to improve simulated results.

Jupiter's current v1 quote documentation requires an API key and marks that API as superseded by v2. The shared HTTP client currently supplies no API-key header. Validate authenticated quote health and plan a measured migration; this review did not send a live order or prove a current authentication failure. [Jupiter quote API](https://developers.jup.ag/docs/api-reference/swap/v1/quote)

Jito submission acknowledgement is not confirmation of landing; validate bundle/transaction status. [Jito transaction documentation](https://docs.jito.wtf/lowlatencytxnsend/)

### 7. Use a risk budget instead of allocation alone

Current settings allocate 30% per ordinary paper position, permit two positions, use a -12% stop, and allow 100% portfolio exposure. An ordinary position hitting the nominal stop costs approximately 3.6% of equity before execution costs; a 50% consensus position costs approximately 6%. Gaps and unsellable tokens can lose more.

A research starting point is a planned loss budget of 0.5–1% of equity per trade, a 20–30% total exposure ceiling, and a daily stop on new entries around 2–3% of starting equity. These are proposed controls, not optimized profitable settings. Preserve exit management after new entries are halted.

Position notional should be bounded by equity risk budget divided by stress-adjusted loss fraction, liquidity, and available cash. At $50 equity, a 1% risk budget and 12% stop imply at most about $4.17 before allowing for fees or worse fills. If that size is uneconomic, continue simulation; increasing capital does not fix a negative edge.

### 8. Keep the current ML filter disabled

The model uses only trade size, hour, and weekday; the wallet argument is unused. It excludes timeout trades, uses a random split, and returns 100% confidence when its model is absent or inference fails. It cannot currently justify greater confidence in entries.

Build a chronological baseline first. If enough clean observations accumulate, add features known at entry: executable spread/impact, liquidity, pool age, independent buyer flow, leader holding behavior, delay, and price deterioration. Evaluate net value and probability calibration on future periods, including timeouts and failures. Compare with simple rules before adding complexity.

## Experiment and release plan

1. **Correctness:** resolve order lifecycle, accounting, API error classification, quote authentication, and data-source issues. Add meaningful failure-path tests. Freeze a versioned baseline.
2. **Observation:** collect every eligible/rejected signal and executable quote trajectory. Reconcile the quiet recent signal funnel using known real swaps and negative controls. Keep provenance protections intact unless evidence shows a parsing error.
3. **Prospective comparison:** compare baseline entries, a cost/delay gate, and a wallet-quality gate in shadow mode. Separately compare whale exits and net-value trailing exits on the same recorded paths. Change one hypothesis at a time.
4. **Validation:** seek at least several weeks and hundreds of opportunities across distinct days/tokens; sample size alone is not proof. Require positive untouched-period net expectancy, a confidence interval that supports the claim, acceptable drawdown, and results that survive worse delay/cost assumptions. A net profit factor above 1.2 can be a provisional research gate, not a guarantee. Include hosting/RPC costs in business profitability.
5. **Small live comparison:** only after lifecycle fixes and credible paper evidence, use tightly capped live exposure to measure actual fills versus predictions. Scale only if the advantage survives. If it does not, reject the strategy or wallet cohort rather than repeatedly retuning the same history.

The first deliverable should be a trustworthy record of what the bot could and did earn. Only then can wallet selection and entry/exit experiments tell us whether this particular copy-trading approach deserves capital.

## Phase 1 implementation

The first correctness phase described here has been implemented. See `analysis/IMPLEMENTATION_PHASE_1.md` for the exact changes and remaining work.

## Verification and limits

The original 64 tests passed in an isolated temporary copy with the settings and whitelist fixtures, without copying the project's credentials. After implementation, the expanded suite passes 73 tests in the workspace.

The passing suite does not establish trading profitability or confirm live GMGN behavior. The execution issues above were identified by tracing the code; no live trades were submitted for this review. The decoder test output also includes an invalid synthetic signature, so a passing test count should not replace positive-control validation against genuine transaction fixtures.

`analysis/profitability_snapshot.json` contains the descriptive results. The script only reads local recorded trades and whitelist data and uses the Python standard library. It does not import the bot or change runtime state. No historical equity return or Sharpe ratio is asserted because balances, configuration versions, cash movements, and complete time series are insufficiently reconstructed.
