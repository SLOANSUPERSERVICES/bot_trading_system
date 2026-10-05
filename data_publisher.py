"""
data_publisher.py — reads all bot state files and trading logs,
builds data/status.json and data/sports_status.json, then git commits and pushes.
Run manually or add to trading_scheduler.py every 5 minutes.
"""
import json
import os
import subprocess
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILES = {
    "ALPHA":    os.path.join(BASE_DIR, "state_alpha.json"),
    "MACRO":    os.path.join(BASE_DIR, "state_macro.json"),
    "MOMENTUM": os.path.join(BASE_DIR, "state_momentum.json"),
    "DEFI":     os.path.join(BASE_DIR, "state_defi.json"),
}

LOG_FILES = {
    "ALPHA":    os.path.join(BASE_DIR, "log_alpha.txt"),
    "MACRO":    os.path.join(BASE_DIR, "log_macro.txt"),
    "MOMENTUM": os.path.join(BASE_DIR, "log_momentum.txt"),
    "DEFI":     os.path.join(BASE_DIR, "log_defi.txt"),
}

SPORTS_BETS_FILE    = os.path.join(DATA_DIR, "sports_bets.json")
SPORTS_BANKROLL_FILE = os.path.join(DATA_DIR, "sports_bankroll.json")


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def last_log_lines(path, n=5):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:] if l.strip()]
    except Exception:
        return []


def parse_pnl_from_log(path):
    """Pull last 30 days of daily P&L from log file (looks for lines like '[DATE] SESSION P&L: $X.XX')"""
    daily = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "SESSION P&L" in line or "session_pnl" in line.lower():
                    # Try to extract date and value
                    import re
                    date_m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                    val_m  = re.search(r"[-+]?\$?([\d]+\.[\d]+)", line)
                    if date_m and val_m:
                        d = date_m.group(1)
                        v = float(val_m.group(1))
                        if "-" in line and val_m.group(0).startswith("-"):
                            v = -v
                        daily[d] = round(v, 4)
    except Exception:
        pass
    # Return sorted last 30 entries
    sorted_days = sorted(daily.items())[-30:]
    return [{"date": d, "pnl": v} for d, v in sorted_days]


def build_bot_card(name, state):
    """Build a normalized bot card for the dashboard from a raw state dict."""
    coins = state.get("coins", {})

    total_value   = sum(c.get("value", 0) for c in coins.values())
    total_trades  = sum(c.get("trades", 0) for c in coins.values())
    total_wins    = sum(c.get("wins", 0) for c in coins.values())
    total_losses  = sum(c.get("losses", 0) for c in coins.values())
    session_pnl   = state.get("session_pnl", state.get("daily_pnl", 0))
    daily_total   = state.get("daily_total", state.get("MAX_RISK", 0))

    # Last action across all coins
    last_action = None
    last_ts     = state.get("ts", "")
    for sym, c in coins.items():
        if c.get("last_action"):
            last_action = f"{c['last_action'].upper()} {sym}"

    # ALPHA has a single position instead of per-coin
    position = state.get("position")
    if position and position.get("sym"):
        sym = position["sym"]
        qty = position.get("qty", 0)
        avg = position.get("avg", 0)
        # Try to compute unrealized P&L pct
        total_value = position.get("cost", 0)
        last_action = f"HOLDING {sym}"

    coin_cards = []
    for sym, c in coins.items():
        qty = c.get("qty", 0)
        if qty and qty > 0:
            coin_cards.append({
                "sym":        sym,
                "qty":        qty,
                "avg":        round(c.get("avg", 0), 6),
                "price":      round(c.get("price", 0), 4),
                "value":      round(c.get("value", 0), 4),
                "pnl_pct":    round(c.get("pnl_pct", 0), 3),
                "last_action": c.get("last_action"),
                "budget_left": c.get("budget_left", 0),
            })

    return {
        "name":        name,
        "ts":          last_ts,
        "session_pnl": round(session_pnl, 4),
        "daily_total": round(daily_total, 2),
        "total_value": round(total_value, 4),
        "trades":      total_trades,
        "wins":        total_wins,
        "losses":      total_losses,
        "win_rate":    round(total_wins / max(total_wins + total_losses, 1) * 100, 1),
        "last_action": last_action,
        "open_positions": coin_cards,
    }


def build_status():
    now_ts = datetime.now(timezone.utc).isoformat()
    bots = []
    total_pnl = 0.0
    total_wins = 0
    total_losses = 0

    for name, path in STATE_FILES.items():
        state = load_json(path)
        if not state:
            bots.append({"name": name, "ts": None, "session_pnl": 0, "error": "state not found"})
            continue

        card = build_bot_card(name, state)
        card["recent_logs"] = last_log_lines(LOG_FILES.get(name, ""), 5)
        card["pnl_last_30"] = parse_pnl_from_log(LOG_FILES.get(name, ""))
        bots.append(card)

        total_pnl    += card["session_pnl"]
        total_wins   += card["wins"]
        total_losses += card["losses"]

    # Check runtime config for command status
    runtime_cfg = load_json(os.path.join(BASE_DIR, "runtime_config.json"))

    return {
        "generated_at": now_ts,
        "summary": {
            "total_pnl":    round(total_pnl, 4),
            "total_wins":   total_wins,
            "total_losses": total_losses,
            "active_bots":  len([b for b in bots if b.get("ts")]),
            "paused":       runtime_cfg.get("paused", False),
            "sell_all":     runtime_cfg.get("sell_all", False),
        },
        "bots": bots,
    }


def build_sports_status():
    bets     = load_json(SPORTS_BETS_FILE)
    bankroll = load_json(SPORTS_BANKROLL_FILE)
    if not bets and not bankroll:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    today_bets = [b for b in (bets if isinstance(bets, list) else []) if b.get("date") == today]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bankroll":     bankroll,
        "today_bets":   today_bets,
        "all_bets":     bets if isinstance(bets, list) else [],
    }


def git_push(files_to_add):
    try:
        subprocess.run(["git", "add"] + files_to_add, cwd=BASE_DIR, check=True)
        msg = f"data: update status {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=BASE_DIR, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            print("No changes to push.")
            return
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print(f"✅ Pushed to GitHub: {msg}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Git error: {e}")


def main():
    print("Building status.json...")
    status = build_status()
    status_path = os.path.join(DATA_DIR, "status.json")
    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    print(f"  Total P&L: ${status['summary']['total_pnl']:.4f}")
    print(f"  Wins: {status['summary']['total_wins']}  Losses: {status['summary']['total_losses']}")

    files_to_push = [status_path]

    sports = build_sports_status()
    if sports:
        sports_path = os.path.join(DATA_DIR, "sports_status.json")
        with open(sports_path, "w") as f:
            json.dump(sports, f, indent=2)
        files_to_push.append(sports_path)
        print("  Sports status included.")

    print("Pushing to GitHub...")
    git_push(files_to_push)


if __name__ == "__main__":
    main()
