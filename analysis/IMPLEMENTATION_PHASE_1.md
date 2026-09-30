# Profitability program — phase 1 implementation

Implemented September 30, 2026. This phase improves correctness and loss containment; it does not claim that the strategy is profitable.

## Completed

- Live buys enter `PENDING_ENTRY` as soon as GMGN returns an order ID. Exposure is reserved before confirmation.
- Buy confirmation timeouts retain the position and reconcile the original order. A CLI timeout with no order ID is reconciled from the wallet's token balance.
- Live sells enter `PENDING_EXIT`. The bot records closure only after order confirmation or a zero token balance.
- Sell timeouts retain the original order ID and poll it instead of submitting another sell.
- A sell CLI timeout without an order ID enters `SUBMISSION_UNKNOWN`; the bot will not resubmit automatically and will reconcile by token balance.
- Pending order IDs are written immediately to the active-position file, closing the normal two-second persistence gap.
- Old live positions are restored regardless of age. The configured stale-position horizon applies only to paper positions.
- GMGN subprocess timeouts terminate and reap the child process without starting a second CLI invocation.
- Position persistence now includes entry and exit order IDs and confirmation statuses.
- Whale scoring and ML training share `trade_history.py`. The append-only JSONL ledger is authoritative; legacy JSON is only a fallback.
- The ML gate rejects entries when its model is missing or inference fails. It no longer silently returns 100% confidence.
- Multi-wallet consensus no longer bypasses the momentum check or doubles allocation.
- Paper entry-cost, exit-proceeds, and net-P&L fields now reconcile arithmetically.
- Trailing stops remain risk exits when the executable result is negative. Static take-profit triggers still require an executable net profit.
- Jupiter requests attach `JUPITER_API_KEY` when configured, and the quote endpoint can be overridden with `JUPITER_QUOTE_API`.

## Validation

`python -m unittest discover -s tests` passes 73 tests. New tests cover canonical history loading, live close confirmation, pending-sell reconciliation, unknown sell submissions, unknown buy submissions, and confirmation-field persistence.

No live order was submitted. Runtime settings remain in paper mode. The concurrent omni-whale discovery work in the repository was left intact.

## Remaining work before live trading

1. Add a prospective signal and quote journal with block/receipt/decode/decision/submission/confirmation timestamps.
2. Record rejected opportunities so selection rules can be compared without survivorship bias.
3. Replace the synthetic path backtester with replay over timestamped executable quotes and overlapping portfolio state.
4. Add explicit daily loss, per-position risk, and portfolio exposure controls after the replay data can support sensible thresholds.
5. Validate GMGN response schemas and late-fill behavior against a tightly capped live account before relying on them.
