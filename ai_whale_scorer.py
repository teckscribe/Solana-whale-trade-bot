"""
ai_whale_scorer.py
=============================================================================
AI Whale Scoring & Vetting Engine for Solana Copy-Trading
(Adapted from FOMO Robinhood Radar scoring architecture)

Evaluates smart money wallets using LLMs (Gemini, Claude, OpenAI) or an offline
deterministic heuristic gauntlet. Categorizes traders by style (swing, scalper,
holder, sniper) and flags toxic patterns (toxic_dev, bot, mev, wash, one-hit).
=============================================================================
"""

import os
import sys
import json
import time
import logging
import argparse
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, asdict

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("AIWhaleScorer")

_DIR = os.path.dirname(os.path.abspath(__file__))
WHITELIST_FILE = os.path.join(_DIR, "whitelist.json")
DISCOVERY_FILE = os.path.join(_DIR, "discovery_db.json")

SYSTEM_PROMPT = """You are a quantitative crypto risk analyst vetting Solana traders for an automated copy-trading portfolio.
You receive performance statistics for ONE whale wallet. Evaluate their repeatable edge vs toxic risk.
Respond with ONLY a strict JSON object matching this exact schema, no markdown code blocks, no prose:
{
  "score": <int 0-100>,
  "status": "WHITELIST" | "WATCH" | "BLACKLIST",
  "style": ["swing" | "scalper" | "holder" | "sniper" | "copy-follower"],
  "red_flags": ["toxic_dev" | "bot" | "mev" | "wash" | "one-hit" | "low_winrate"],
  "summary": "<2-3 sentences explaining rationale>",
  "confidence": <float 0.0-1.0>
}

Evaluation Rules:
1. "toxic_dev" (CRITICAL): If the wallet has tags like 'top_dev' or dev behavior, they create and dump tokens. Immediate score < 20, status BLACKLIST.
2. "bot" / "mev" (CRITICAL): High trade frequencies (>200 trades/week) or tags like 'mev', 'sniper', 'padre', 'banana', 'trojan'. Immediate score < 30, status BLACKLIST. Copy-trading sub-second MEV bots guarantees 100% slippage loss.
3. "one-hit": Huge headline PnL on tiny trade counts (<5 trades). Often 1 lucky insider pump that cannot be reproduced. Status WATCH.
4. "wash": High volume, hundreds of trades, but negative or zero profit. Status BLACKLIST.
5. "swing" / "holder" (PREFERRED): Moderate trade frequency (10-100 trades/7d), high win rate (>=65%), positive realized profit, smart money tags. Score >= 75, status WHITELIST.
Score thresholds: >= 70 -> WHITELIST; 45-69 -> WATCH; < 45 -> BLACKLIST."""


@dataclass
class WhaleScore:
    wallet: str
    score: int
    status: str
    style: List[str]
    red_flags: List[str]
    summary: str
    confidence: float
    method: str  # "gemini", "claude", "openai", "heuristic", "imported"

    def to_dict(self) -> dict:
        return asdict(self)


def _heuristic_score(wallet: str, data: dict) -> WhaleScore:
    """
    Deterministic rule-based scoring gauntlet when no LLM API key is present.
    Mirror of the LLM prompt rules for zero-cost offline evaluation.
    """
    winrate = float(data.get("winrate_7d", data.get("win_rate", 0.0)))
    trades = int(data.get("trades_7d", data.get("total_trades_7d", data.get("trades_cnt", 0))))
    profit = float(data.get("profit_7d", data.get("realized_profit", 0.0)))
    tags = [str(t).strip().lower() for t in data.get("tags", [])]

    score = 60
    red_flags = []
    style = []

    # 1. Toxic Dev check
    if any(t in tags for t in ["top_dev", "dev", "token_creator", "deployer"]):
        red_flags.append("toxic_dev")
        score -= 50

    # 2. MEV / Bot check
    if any(t in tags for t in ["mev", "sniper", "banana", "padre", "trojan", "maestro"]):
        red_flags.append("mev")
        red_flags.append("bot")
        score -= 40
    elif trades > 250:
        red_flags.append("bot")
        score -= 30
    elif trades > 150:
        score -= 10

    # 3. Winrate evaluation
    if winrate >= 75.0:
        score += 20
    elif winrate >= 65.0:
        score += 10
    elif winrate < 45.0 and winrate > 0.0:
        red_flags.append("low_winrate")
        score -= 25
    elif winrate == 0.0 and trades > 10:
        red_flags.append("low_winrate")
        score -= 30

    # 4. Profitability
    if profit >= 20000.0:
        score += 15
    elif profit >= 5000.0:
        score += 10
    elif profit < 0.0:
        score -= 20

    # 5. One-hit wonder check
    if trades < 6 and profit > 10000.0:
        red_flags.append("one-hit")
        score -= 15

    # 6. Style classification
    if "bot" in red_flags:
        style.append("scalper")
    elif trades <= 60 and winrate >= 60.0:
        style.append("swing")
        style.append("holder")
    elif trades > 60:
        style.append("scalper")
    else:
        style.append("copy-follower")

    # Smart degen bonus
    if any(t in tags for t in ["smart_degen", "smart_money"]):
        score += 10

    # Hard ceiling caps on critical toxicity
    if "toxic_dev" in red_flags:
        score = min(score, 20)
    elif "mev" in red_flags or "bot" in red_flags:
        score = min(score, 30)

    score = max(0, min(100, score))

    if "toxic_dev" in red_flags or "mev" in red_flags or score < 45:
        status = "BLACKLIST"
    elif score >= 70:
        status = "WHITELIST"
    else:
        status = "WATCH"

    summary = (
        f"Winrate {winrate:.1f}%, {trades} trades, profit ${profit:,.0f}. "
        f"Tags: {tags or 'None'}. "
        f"{'Flagged: ' + ', '.join(red_flags) if red_flags else 'Consistent organic performance.'}"
    )

    return WhaleScore(
        wallet=wallet,
        score=score,
        status=status,
        style=style,
        red_flags=red_flags,
        summary=summary,
        confidence=0.85,
        method="heuristic",
    )


def _call_gemini(wallet: str, context: dict, api_key: str) -> Optional[WhaleScore]:
    """Calls Google Gemini API (gemini-2.5-flash) for qualitative whale evaluation."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": f"Evaluate this Solana whale wallet:\n{json.dumps(context, indent=2)}"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        }
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning(f"Gemini API returned status {resp.status_code}: {resp.text[:120]}")
                return None
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw_text.strip())
            return WhaleScore(
                wallet=wallet,
                score=int(parsed.get("score", 50)),
                status=str(parsed.get("status", "WATCH")).upper(),
                style=list(parsed.get("style", [])),
                red_flags=list(parsed.get("red_flags", [])),
                summary=str(parsed.get("summary", "")),
                confidence=float(parsed.get("confidence", 0.9)),
                method="gemini",
            )
    except Exception as e:
        log.warning(f"Gemini evaluation failed for {wallet[:8]}: {e}")
        return None


def score_wallet(wallet: str, data: dict, force_heuristic: bool = False) -> WhaleScore:
    """
    Evaluates a single wallet. Uses Gemini LLM if GEMINI_API_KEY is present,
    otherwise uses the deterministic heuristic gauntlet.
    """
    context = {
        "wallet": wallet,
        "winrate_7d": float(data.get("winrate_7d", data.get("win_rate", 0.0))),
        "trades_7d": int(data.get("trades_7d", data.get("total_trades_7d", data.get("trades_cnt", 0)))),
        "profit_7d_usd": float(data.get("profit_7d", data.get("realized_profit", 0.0))),
        "tags": data.get("tags", []),
        "sol_balance": float(data.get("sol_balance", 0.0)),
    }

    if not force_heuristic:
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_key:
            res = _call_gemini(wallet, context, gemini_key)
            if res:
                return res

    return _heuristic_score(wallet, data)


def audit_all_whales(whitelist_path: str = WHITELIST_FILE, auto_prune: bool = True) -> Dict[str, Any]:
    """
    Audits every whitelisted whale in whitelist.json.
    If auto_prune is True, demotes wallets scoring < 45 or with toxic tags to discovery_db.json
    under status 'BLACKLIST'.
    """
    import whale_manager

    if not os.path.exists(whitelist_path):
        return {"error": f"Whitelist file not found: {whitelist_path}"}

    with open(whitelist_path, "r") as f:
        whitelist = json.load(f)

    results = []
    pruned = []
    retained = []

    log.info(f"Auditing {len(whitelist)} whitelisted whales...")

    for wallet, data in whitelist.items():
        score_res = score_wallet(wallet, data)
        results.append(score_res.to_dict())

        if score_res.status == "BLACKLIST" or score_res.score < 45:
            pruned.append(score_res)
            if auto_prune:
                log.warning(
                    f"🚨 Pruning toxic whale {wallet[:8]} (Score: {score_res.score}, "
                    f"Flags: {score_res.red_flags}). Demoting to BLACKLIST."
                )
                whale_manager.set_whale_status(wallet, "BLACKLIST")
        else:
            retained.append(score_res)

    return {
        "total_evaluated": len(whitelist),
        "retained_count": len(retained),
        "pruned_count": len(pruned),
        "retained": [r.to_dict() for r in retained],
        "pruned": [p.to_dict() for p in pruned],
        "scores": results,
    }


def export_pending(out_path: str = "pending_whales.json"):
    """
    Exports all whitelisted whales into a compact JSON context for copy-pasting
    into ChatGPT / Claude / Gemini Web without requiring an API key.
    """
    if not os.path.exists(WHITELIST_FILE):
        print(f"Error: {WHITELIST_FILE} does not exist.")
        return

    with open(WHITELIST_FILE, "r") as f:
        whitelist = json.load(f)

    bundle = {
        "instructions": "Audit these Solana wallets according to the prompt schema in SYSTEM_PROMPT. Return a JSON array of evaluated objects.",
        "system_prompt": SYSTEM_PROMPT,
        "wallets": [
            {
                "wallet": w,
                "winrate_7d": d.get("winrate_7d", 0.0),
                "trades_7d": d.get("trades_7d", 0),
                "profit_7d": d.get("profit_7d", 0.0),
                "tags": d.get("tags", []),
            }
            for w, d in whitelist.items()
        ]
    }

    with open(out_path, "w") as f:
        json.dump(bundle, f, indent=2)

    print(f"Exported {len(bundle['wallets'])} whales to {out_path}.")
    print("You can paste this file into ChatGPT/Claude web chat and save the response to import.")


def import_scored(in_path: str = "scored_whales.json", auto_prune: bool = True):
    """
    Imports LLM-scored results from a JSON file and applies verdicts.
    """
    import whale_manager

    if not os.path.exists(in_path):
        print(f"Error: {in_path} does not exist.")
        return

    with open(in_path, "r") as f:
        scored = json.load(f)

    if isinstance(scored, dict) and "wallets" in scored:
        scored = scored["wallets"]

    applied = 0
    pruned = 0
    for entry in scored:
        w = entry.get("wallet")
        score = entry.get("score", 50)
        status = str(entry.get("status", "WATCH")).upper()
        if not w:
            continue

        if status == "BLACKLIST" or score < 45:
            if auto_prune:
                whale_manager.set_whale_status(w, "BLACKLIST")
                pruned += 1
        applied += 1

    print(f"Applied {applied} scored whales from {in_path} ({pruned} pruned to BLACKLIST).")


def main():
    parser = argparse.ArgumentParser(description="WTB AI Whale Scoring & Vetting Engine")
    parser.add_argument("--audit", action="store_true", help="Run audit on all whitelisted whales")
    parser.add_argument("--no-prune", action="store_true", help="Audit without automatically blacklisting low scores")
    parser.add_argument("--export", type=str, metavar="FILE", help="Export pending whales to JSON for web LLM chat")
    parser.add_argument("--import-file", type=str, metavar="FILE", help="Import evaluated results from JSON file")

    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.export:
        export_pending(args.export)
    elif args.import_file:
        import_scored(args.import_file, auto_prune=not args.no_prune)
    elif args.audit or len(sys.argv) == 1:
        rep = audit_all_whales(auto_prune=not args.no_prune)
        print("\n" + "=" * 60)
        print(f"WHALE AUDIT COMPLETE: {rep.get('total_evaluated', 0)} Evaluated")
        print(f"  [+] Retained (WHITELIST/WATCH): {rep.get('retained_count', 0)}")
        print(f"  [!] Pruned (BLACKLIST): {rep.get('pruned_count', 0)}")
        print("=" * 60)
        if rep.get("pruned"):
            print("\nPRUNED WALLETS:")
            for p in rep["pruned"]:
                print(f"  - {p['wallet'][:12]}... Score: {p['score']} | Flags: {p['red_flags']} | {p['summary'][:60]}")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
