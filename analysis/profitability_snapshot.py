"""Read-only summary of recorded WTB outcomes; does not import or run the bot."""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]


def summarize(rows):
    profits = [float(row.get("net_profit_usd") or 0) for row in rows]
    returns = [float(row.get("net_profit_percent") or 0) for row in rows]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(rows), 2) if rows else None,
        "recorded_net_pnl_usd": round(sum(profits), 4),
        "profit_factor": round(sum(wins) / -sum(losses), 4) if losses else None,
        "mean_trade_return_pct": round(mean(returns), 4) if rows else None,
        "median_trade_return_pct": round(median(returns), 4) if rows else None,
        "mean_trade_size_usd": round(mean(float(r["trade_size_usd"]) for r in rows), 4) if rows else None,
    }


def main():
    rows = [json.loads(line) for line in (ROOT / "ml_training_data.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    whitelist = json.loads((ROOT / "whitelist.json").read_text(encoding="utf-8"))
    wallets = defaultdict(list)
    days = defaultdict(list)
    reasons = defaultdict(list)
    for row in rows:
        wallets[row["whale_wallet"]].append(row)
        days[row["timestamp_entry"][:10]].append(row)
        reasons[row["exit_reason"]].append(row)
    result = {
        "interpretation": "Descriptive recorded paper outcomes across changing settings and balances; not audited live returns or an out-of-sample backtest.",
        "period": [min(r["timestamp_entry"] for r in rows), max(r["timestamp_entry"] for r in rows)] if rows else [],
        "summary": summarize(rows),
        "modes": dict(Counter(r.get("trade_mode") for r in rows)),
        "confirmed_fill_records": sum(bool(r.get("fill_data_available")) for r in rows),
        "unique_record_keys": len({(r["timestamp_entry"], r["token_address"], r["whale_wallet"]) for r in rows}),
        "days": {day: summarize(group) for day, group in sorted(days.items())},
        "exit_reasons": {reason: summarize(group) for reason, group in reasons.items()},
        "wallets": {wallet: {**summarize(group), "current_whitelist_status": whitelist.get(wallet, {}).get("status", "ABSENT")} for wallet, group in wallets.items()},
        "take_profit_records_with_losses": sum(r["exit_reason"] == "TAKE_PROFIT" and r["net_profit_usd"] < 0 for r in rows),
        "cashflow_field_mismatches_over_one_cent": sum(
            abs(r["real_exit_proceeds_usd"] - r["real_entry_cost_usd"] - r["net_profit_usd"]) > 0.01
            for r in rows if r.get("real_exit_proceeds_usd") is not None and r.get("real_entry_cost_usd") is not None
        ),
        "booked_fixed_costs_usd": round(sum(float(r.get("fixed_cost_usd") or 0) for r in rows), 4),
        "additional_flat_fee_estimate_usd": round(sum(float(r["trade_size_usd"]) * 2 * float(r.get("fee_pct_per_leg") or 0) / 100 for r in rows), 4),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
