"""
weekly_review.py — Sunday 2AM automated improvement loop for the 7-bot crypto trading system.

Reads the past 7 days of bot logs, calculates per-bot performance, retrains ML models,
auto-approves parameter tweaks, and writes a full performance report.

Usage:
    python system/weekly_review.py
    # Or scheduled via cron: 0 2 * * 0 python /path/to/system/weekly_review.py
"""

import os
import re
import sys
import json
import importlib.util
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ── Colorama ──────────────────────────────────────────────────────────────────
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    C_CYAN    = Fore.CYAN
    C_GREEN   = Fore.GREEN
    C_RED     = Fore.RED
    C_YELLOW  = Fore.YELLOW
    C_MAGENTA = Fore.MAGENTA
    C_RESET   = Style.RESET_ALL
    C_BOLD    = Style.BRIGHT
except ImportError:
    C_CYAN = C_GREEN = C_RED = C_YELLOW = C_MAGENTA = C_RESET = C_BOLD = ""

# ── Path constants ─────────────────────────────────────────────────────────────
THIS_FILE  = Path(__file__).resolve()
BASE_DIR   = THIS_FILE.parent.parent          # two levels up from system/
LOGS_DIR   = BASE_DIR / "logs"
SYSTEM_DIR = BASE_DIR / "system"
ML_DIR     = BASE_DIR / "ml"

PENDING_UPDATES_PATH = SYSTEM_DIR / "pending_updates.json"

# ── Bot registry ───────────────────────────────────────────────────────────────
# Canonical bot name → log filename prefix  (log_<prefix>_YYYYMMDD.txt)
BOTS = {
    "momentum":   "momentum",
    "defi":       "defi",
    "scalper":    "scalper",
    "sentiment":  "sentiment",
    "arbitrage":  "arbitrage",
    "breakout":   "breakout",
    "mean_rev":   "mean_rev",
}

# Default daily cap per bot (USD). Used when suggesting cap adjustments.
DEFAULT_DAILY_CAP = 20.0

# ── Regex patterns for log parsing ────────────────────────────────────────────
# BUY line:  ... BUY ... BTC ... $43,210.50 ...
RE_BUY   = re.compile(r'\bBUY\b', re.IGNORECASE)
# SELL / TARGET / STOP lines
RE_SELL  = re.compile(r'\b(SELL|TARGET|STOP)\b', re.IGNORECASE)
# Coin ticker — 2-8 uppercase letters that look like a symbol
RE_COIN  = re.compile(r'\b([A-Z]{2,8})\b')
# Price  e.g. $43,210.50  or  43210.50
RE_PRICE = re.compile(r'\$?([\d,]+(?:\.\d+)?)')
# P&L  e.g. +$1.24  -$0.87  +1.24  -0.87
RE_PNL   = re.compile(r'([+-])\$?([\d,]+(?:\.\d+)?)')

# Coins to ignore when extracting tickers (common English words that also match)
COIN_BLACKLIST = {
    "BUY", "SELL", "STOP", "TARGET", "AM", "PM", "UTC", "EST",
    "INFO", "WARN", "ERROR", "DEBUG", "LOG", "BOT", "USD", "USDT",
    "USDC", "DAY", "WEEK", "NET", "PNL", "WIN", "LOSS",
}


# ══════════════════════════════════════════════════════════════════════════════
# 1.  LOG READING
# ══════════════════════════════════════════════════════════════════════════════

def date_range_last_7_days() -> list[str]:
    """Return a list of YYYYMMDD strings for the past 7 days (inclusive of today)."""
    today = datetime.utcnow().date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(7)]


def find_log_files(bot_prefix: str, dates: list[str]) -> list[Path]:
    """Locate log files for a bot over the given date strings."""
    found = []
    if not LOGS_DIR.exists():
        return found
    for date_str in dates:
        candidate = LOGS_DIR / f"log_{bot_prefix}_{date_str}.txt"
        if candidate.exists():
            found.append(candidate)
    return found


def extract_coin(line: str) -> str | None:
    """Pull the most plausible coin ticker from a log line."""
    tokens = RE_COIN.findall(line)
    for tok in tokens:
        if tok not in COIN_BLACKLIST and len(tok) >= 2:
            return tok
    return None


def extract_pnl(line: str) -> float | None:
    """
    Extract a P&L value from a sell/close line.
    Looks for patterns like  +$1.24  -$0.87  P&L: +1.24
    Returns float or None if not found.
    """
    # Prioritise explicit P&L keyword
    pnl_label = re.search(r'P[&/]?L[:\s]+([+-]?\$?[\d,]+(?:\.\d+)?)', line, re.IGNORECASE)
    if pnl_label:
        raw = pnl_label.group(1).replace("$", "").replace(",", "")
        try:
            return float(raw)
        except ValueError:
            pass

    # Fall back to any signed dollar amount
    matches = RE_PNL.findall(line)
    for sign, digits in matches:
        raw = digits.replace(",", "")
        try:
            val = float(raw)
            return val if sign == "+" else -val
        except ValueError:
            continue
    return None


def parse_log_files(paths: list[Path]) -> list[dict]:
    """
    Parse a list of log files into a list of trade records.
    Each record: {"type": "BUY"|"SELL", "coin": str, "pnl": float|None}
    """
    trades = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"{C_YELLOW}[WARN] Cannot read {path}: {exc}{C_RESET}")
            continue

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if RE_BUY.search(line):
                coin = extract_coin(line)
                trades.append({"type": "BUY", "coin": coin, "pnl": None})

            elif RE_SELL.search(line):
                coin = extract_coin(line)
                pnl  = extract_pnl(line)
                trades.append({"type": "SELL", "coin": coin, "pnl": pnl})

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 2.  PERFORMANCE CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def calc_bot_stats(trades: list[dict]) -> dict:
    """
    Compute per-bot performance from parsed trade records.
    Returns a stats dict; all monetary values are floats.
    """
    sell_trades = [t for t in trades if t["type"] == "SELL" and t["pnl"] is not None]

    if not sell_trades:
        return {"no_data": True, "trade_count": 0}

    total_pnl   = sum(t["pnl"] for t in sell_trades)
    winners     = [t for t in sell_trades if t["pnl"] > 0]
    losers      = [t for t in sell_trades if t["pnl"] <= 0]
    win_rate    = len(winners) / len(sell_trades) * 100 if sell_trades else 0.0
    avg_gain    = sum(t["pnl"] for t in winners) / len(winners) if winners else 0.0
    avg_loss    = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0.0

    # Per-coin P&L
    coin_pnl: dict[str, float] = defaultdict(float)
    for t in sell_trades:
        if t["coin"]:
            coin_pnl[t["coin"]] += t["pnl"]

    best_coin  = max(coin_pnl, key=coin_pnl.get) if coin_pnl else "N/A"
    worst_coin = min(coin_pnl, key=coin_pnl.get) if coin_pnl else "N/A"

    return {
        "no_data":     False,
        "trade_count": len(sell_trades),
        "win_rate":    round(win_rate, 1),
        "total_pnl":   round(total_pnl, 2),
        "avg_gain":    round(avg_gain, 2),
        "avg_loss":    round(avg_loss, 2),
        "best_coin":   best_coin,
        "worst_coin":  worst_coin,
        "coin_pnl":    dict(coin_pnl),
    }


def gather_all_stats(dates: list[str]) -> dict[str, dict]:
    """Collect stats for every registered bot."""
    all_stats: dict[str, dict] = {}
    for bot_name, prefix in BOTS.items():
        log_files = find_log_files(prefix, dates)
        if not log_files:
            all_stats[bot_name] = {"no_data": True, "trade_count": 0}
        else:
            trades = parse_log_files(log_files)
            all_stats[bot_name] = calc_bot_stats(trades)
    return all_stats


# ══════════════════════════════════════════════════════════════════════════════
# 3.  ML RETRAIN
# ══════════════════════════════════════════════════════════════════════════════

def retrain_ml() -> tuple[bool, str]:
    """
    Attempt to import and run the ML training module.
    Returns (success: bool, message: str).
    """
    candidates = [
        ML_DIR     / "train_ml.py",
        SYSTEM_DIR / "train_ml.py",
        BASE_DIR   / "train_ml.py",
    ]

    train_path = None
    for c in candidates:
        if c.exists():
            train_path = c
            break

    if train_path is None:
        return False, "train_ml.py not found in ml/, system/, or project root."

    try:
        spec   = importlib.util.spec_from_file_location("train_ml", train_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if hasattr(module, "main") and callable(module.main):
            module.main()
            return True, f"train_ml.main() completed successfully (source: {train_path})"
        else:
            # exec-style fallback already ran the module at import time
            return True, f"train_ml executed (no main() found; module-level code ran) (source: {train_path})"

    except Exception:
        tb = traceback.format_exc()
        return False, f"ML retrain raised an exception:\n{tb}"


# ══════════════════════════════════════════════════════════════════════════════
# 4.  PARAMETER TWEAKS & PERSISTENT COIN TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def load_pending_updates() -> dict:
    """Load existing pending_updates.json or return a blank template."""
    if PENDING_UPDATES_PATH.exists():
        try:
            with PENDING_UPDATES_PATH.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {"auto_approve": [], "human_review": [], "coin_loss_history": {}, "generated_at": ""}


def save_pending_updates(data: dict) -> None:
    """Persist pending_updates.json."""
    PENDING_UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["generated_at"] = datetime.utcnow().isoformat()
    with PENDING_UPDATES_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def build_parameter_tweaks(
    all_stats: dict[str, dict],
    existing: dict,
) -> dict:
    """
    Generate AUTO_APPROVE and HUMAN_REVIEW items.

    Auto-approve rules:
      • win_rate < 45% → reduce daily cap by $2
      • win_rate > 70% → increase daily cap by $2

    Human-review rules:
      • A coin that loses money for 3+ consecutive weeks across any bot
        → flag for removal (HUMAN_REVIEW)
    """
    auto_approve:  list[dict] = []
    human_review:  list[dict] = []

    # ── Cap adjustment suggestions ────────────────────────────────────────────
    for bot_name, stats in all_stats.items():
        if stats.get("no_data"):
            continue

        wr = stats["win_rate"]
        if wr < 45.0:
            auto_approve.append({
                "type":        "CAP_REDUCE",
                "bot":         bot_name,
                "change_usd":  -2.0,
                "reason":      f"Win rate {wr}% < 45% threshold",
                "flag":        "AUTO_APPROVE",
            })
        elif wr > 70.0:
            auto_approve.append({
                "type":        "CAP_INCREASE",
                "bot":         bot_name,
                "change_usd":  +2.0,
                "reason":      f"Win rate {wr}% > 70% threshold",
                "flag":        "AUTO_APPROVE",
            })

    # ── Consecutive-week loss tracking per coin ───────────────────────────────
    # Structure in existing JSON:
    #   "coin_loss_history": { "BTC": 2, "SOL": 1, ... }
    # Each integer = consecutive weeks with negative P&L across ALL bots.
    coin_loss_history: dict[str, int] = existing.get("coin_loss_history", {})

    # Aggregate this week's P&L per coin across all bots
    this_week_coin_pnl: dict[str, float] = defaultdict(float)
    for stats in all_stats.values():
        if stats.get("no_data"):
            continue
        for coin, pnl_val in stats.get("coin_pnl", {}).items():
            this_week_coin_pnl[coin] += pnl_val

    updated_history: dict[str, int] = {}
    for coin, pnl_val in this_week_coin_pnl.items():
        prev_streak = coin_loss_history.get(coin, 0)
        if pnl_val < 0:
            new_streak = prev_streak + 1
        else:
            new_streak = 0          # reset on a profitable week
        updated_history[coin] = new_streak

        if new_streak >= 3:
            human_review.append({
                "type":            "REMOVE_COIN",
                "coin":            coin,
                "consecutive_loss_weeks": new_streak,
                "weekly_pnl":      round(pnl_val, 2),
                "reason":          f"{coin} has lost money for {new_streak} consecutive weeks",
                "flag":            "HUMAN_REVIEW",
            })

    # Preserve history for coins not seen this week (they keep their streak)
    for coin, streak in coin_loss_history.items():
        if coin not in updated_history:
            updated_history[coin] = streak

    return {
        "auto_approve":      auto_approve,
        "human_review":      human_review,
        "coin_loss_history": updated_history,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5.  REPORT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def bot_recommendation(stats: dict) -> str:
    """One-liner recommendation based on stats."""
    if stats.get("no_data"):
        return "No data this week"
    wr = stats["win_rate"]
    pnl = stats["total_pnl"]
    if wr > 70:
        return "Performing well — consider cap increase ✓"
    if wr < 45:
        return "Under-performing — cap reduction suggested"
    if pnl < 0:
        return "Negative P&L — monitor closely"
    return "Stable — no changes needed"


def top_coin_this_week(all_stats: dict[str, dict]) -> tuple[str, float]:
    """Find the single best-performing coin across all bots this week."""
    aggregate: dict[str, float] = defaultdict(float)
    for stats in all_stats.values():
        if stats.get("no_data"):
            continue
        for coin, pnl_val in stats.get("coin_pnl", {}).items():
            aggregate[coin] += pnl_val
    if not aggregate:
        return "N/A", 0.0
    best = max(aggregate, key=aggregate.get)
    return best, round(aggregate[best], 2)


def format_report(
    all_stats:    dict[str, dict],
    tweaks:       dict,
    ml_success:   bool,
    ml_message:   str,
    report_date:  str,
) -> str:
    """Build the full human-readable text report (plain, no ANSI)."""
    divider   = "=" * 72
    sub_div   = "-" * 72
    top_coin, top_pnl = top_coin_this_week(all_stats)

    lines = [
        divider,
        f"  WEEKLY TRADING SYSTEM REVIEW — {report_date}",
        divider,
        "",
        "BOT PERFORMANCE SUMMARY",
        sub_div,
        f"{'Bot':<14} {'Trades':>7} {'Win Rate':>10} {'Total P&L':>12} {'Recommendation'}",
        sub_div,
    ]

    for bot_name, stats in all_stats.items():
        if stats.get("no_data"):
            lines.append(f"{bot_name:<14} {'—':>7} {'No data':>10} {'—':>12}  No data")
        else:
            lines.append(
                f"{bot_name:<14} {stats['trade_count']:>7} "
                f"{stats['win_rate']:>9.1f}% "
                f"${stats['total_pnl']:>10.2f}  "
                f"{bot_recommendation(stats)}"
            )

    lines += [
        sub_div,
        "",
        f"TOP COIN THIS WEEK:  {top_coin}  (net P&L: ${top_pnl:+.2f})",
        "",
        "ML RETRAIN STATUS",
        sub_div,
        f"{'SUCCESS' if ml_success else 'FAILED'}: {ml_message}",
        "",
        "AUTO-APPROVED CHANGES",
        sub_div,
    ]

    if tweaks["auto_approve"]:
        for item in tweaks["auto_approve"]:
            sign = "+" if item["change_usd"] > 0 else ""
            lines.append(
                f"  [{item['flag']}] {item['bot']}: daily cap {sign}${abs(item['change_usd']):.2f}  "
                f"({item['reason']})"
            )
    else:
        lines.append("  None this week.")

    lines += [
        "",
        "ITEMS FOR HUMAN REVIEW",
        sub_div,
    ]

    if tweaks["human_review"]:
        for item in tweaks["human_review"]:
            lines.append(
                f"  [{item['flag']}] {item.get('coin', '?')}: {item['reason']}  "
                f"(weekly P&L: ${item.get('weekly_pnl', 0):+.2f}, "
                f"streak: {item.get('consecutive_loss_weeks', 0)} weeks)"
            )
    else:
        lines.append("  None this week.")

    lines += ["", divider, ""]
    return "\n".join(lines)


def print_colored_report(
    all_stats:   dict[str, dict],
    tweaks:      dict,
    ml_success:  bool,
    ml_message:  str,
    report_date: str,
) -> None:
    """Print the report to stdout with colorama color coding."""
    divider  = C_CYAN + C_BOLD + "=" * 72 + C_RESET
    sub_div  = C_CYAN + "-" * 72 + C_RESET
    top_coin, top_pnl = top_coin_this_week(all_stats)

    print(divider)
    print(C_CYAN + C_BOLD + f"  WEEKLY TRADING SYSTEM REVIEW — {report_date}" + C_RESET)
    print(divider)
    print()
    print(C_CYAN + C_BOLD + "BOT PERFORMANCE SUMMARY" + C_RESET)
    print(sub_div)
    print(f"{'Bot':<14} {'Trades':>7} {'Win Rate':>10} {'Total P&L':>12}  Recommendation")
    print(sub_div)

    for bot_name, stats in all_stats.items():
        if stats.get("no_data"):
            print(f"{bot_name:<14} {'—':>7} {'No data':>10} {'—':>12}  No data")
        else:
            pnl_str = f"${stats['total_pnl']:>10.2f}"
            rec_str = bot_recommendation(stats)
            color = C_GREEN if stats["total_pnl"] >= 0 else C_RED
            print(
                color +
                f"{bot_name:<14} {stats['trade_count']:>7} "
                f"{stats['win_rate']:>9.1f}% "
                f"{pnl_str}  {rec_str}" +
                C_RESET
            )

    print(sub_div)
    print()
    top_color = C_GREEN if top_pnl >= 0 else C_RED
    print(f"TOP COIN THIS WEEK:  {top_color}{top_coin}  (net P&L: ${top_pnl:+.2f}){C_RESET}")
    print()

    # ML status
    print(C_CYAN + C_BOLD + "ML RETRAIN STATUS" + C_RESET)
    print(sub_div)
    ml_color = C_MAGENTA if ml_success else C_RED
    status   = "SUCCESS" if ml_success else "FAILED"
    print(ml_color + f"{status}: {ml_message}" + C_RESET)
    print()

    # Auto-approve
    print(C_CYAN + C_BOLD + "AUTO-APPROVED CHANGES" + C_RESET)
    print(sub_div)
    if tweaks["auto_approve"]:
        for item in tweaks["auto_approve"]:
            sign = "+" if item["change_usd"] > 0 else ""
            print(
                C_GREEN +
                f"  [{item['flag']}] {item['bot']}: daily cap "
                f"{sign}${abs(item['change_usd']):.2f}  ({item['reason']})" +
                C_RESET
            )
    else:
        print("  None this week.")
    print()

    # Human review
    print(C_CYAN + C_BOLD + "ITEMS FOR HUMAN REVIEW" + C_RESET)
    print(sub_div)
    if tweaks["human_review"]:
        for item in tweaks["human_review"]:
            print(
                C_YELLOW +
                f"  [{item['flag']}] {item.get('coin', '?')}: {item['reason']}  "
                f"(weekly P&L: ${item.get('weekly_pnl', 0):+.2f}, "
                f"streak: {item.get('consecutive_loss_weeks', 0)} weeks)" +
                C_RESET
            )
    else:
        print("  None this week.")

    print()
    print(divider)
    print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    run_start    = datetime.utcnow()
    report_date  = run_start.strftime("%Y-%m-%d %H:%M UTC")
    report_tag   = run_start.strftime("%Y%m%d")

    print(C_CYAN + C_BOLD + "\n[Weekly Review] Starting automated improvement loop..." + C_RESET)
    print(C_CYAN + f"Base directory : {BASE_DIR}" + C_RESET)
    print(C_CYAN + f"Logs directory : {LOGS_DIR}" + C_RESET)
    print()

    # ── Step 1: read logs ──────────────────────────────────────────────────────
    dates     = date_range_last_7_days()
    print(C_CYAN + f"[1/5] Reading logs for dates: {', '.join(dates)}" + C_RESET)
    all_stats = gather_all_stats(dates)

    bots_with_data = sum(1 for s in all_stats.values() if not s.get("no_data"))
    print(f"      Bots with data this week: {bots_with_data}/{len(BOTS)}")

    # ── Step 2: compute performance ────────────────────────────────────────────
    print(C_CYAN + "[2/5] Performance stats computed." + C_RESET)

    # ── Step 3: ML retrain ────────────────────────────────────────────────────
    print(C_CYAN + "[3/5] Attempting ML model retrain..." + C_RESET)
    ml_success, ml_message = retrain_ml()
    ml_color = C_MAGENTA if ml_success else C_RED
    print(ml_color + f"      {ml_message.splitlines()[0]}" + C_RESET)

    # ── Step 4: parameter tweaks ──────────────────────────────────────────────
    print(C_CYAN + "[4/5] Evaluating parameter tweaks..." + C_RESET)
    existing_updates = load_pending_updates()
    tweaks           = build_parameter_tweaks(all_stats, existing_updates)

    save_dict = {
        "auto_approve":      tweaks["auto_approve"],
        "human_review":      tweaks["human_review"],
        "coin_loss_history": tweaks["coin_loss_history"],
    }
    save_pending_updates(save_dict)
    print(f"      Auto-approve items : {len(tweaks['auto_approve'])}")
    print(f"      Human-review items : {len(tweaks['human_review'])}")
    print(f"      Saved → {PENDING_UPDATES_PATH}")

    # ── Step 5: print & save report ───────────────────────────────────────────
    print(C_CYAN + "[5/5] Generating report..." + C_RESET)
    print()

    print_colored_report(all_stats, tweaks, ml_success, ml_message, report_date)

    plain_report = format_report(all_stats, tweaks, ml_success, ml_message, report_date)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LOGS_DIR / f"weekly_report_{report_tag}.txt"
    report_path.write_text(plain_report, encoding="utf-8")
    print(C_CYAN + f"Report saved → {report_path}" + C_RESET)

    # ── Per-bot detail (verbose) ───────────────────────────────────────────────
    print()
    print(C_CYAN + C_BOLD + "PER-BOT DETAIL" + C_RESET)
    print(C_CYAN + "-" * 72 + C_RESET)
    for bot_name, stats in all_stats.items():
        if stats.get("no_data"):
            print(f"  {bot_name:<14}  No log files found for this week.")
        else:
            color = C_GREEN if stats["total_pnl"] >= 0 else C_RED
            print(
                color +
                f"  {bot_name:<14}  "
                f"trades={stats['trade_count']}  "
                f"win={stats['win_rate']}%  "
                f"P&L=${stats['total_pnl']:+.2f}  "
                f"avg_gain=${stats['avg_gain']:+.2f}  "
                f"avg_loss=${stats['avg_loss']:+.2f}  "
                f"best={stats['best_coin']}  worst={stats['worst_coin']}" +
                C_RESET
            )
    print()
    elapsed = (datetime.utcnow() - run_start).total_seconds()
    print(C_CYAN + f"[Weekly Review] Completed in {elapsed:.1f}s." + C_RESET)


if __name__ == "__main__":
    main()
