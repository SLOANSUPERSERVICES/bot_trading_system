"""
bot_command_reader.py — reads bot_commands.json and applies instructions to scalper configs.
Called by trading_scheduler.py or run standalone. Modifies scalper state/config at runtime
by writing to a shared runtime_config.json that each scalper checks at the top of its loop.
"""
import json
import os
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMMANDS_FILE = os.path.join(BASE_DIR, "bot_commands.json")
RUNTIME_CONFIG = os.path.join(BASE_DIR, "runtime_config.json")
COMMAND_LOG = os.path.join(BASE_DIR, "command_log.txt")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(COMMAND_LOG, "a") as f:
        f.write(line + "\n")


def load_runtime_config():
    if os.path.exists(RUNTIME_CONFIG):
        with open(RUNTIME_CONFIG) as f:
            return json.load(f)
    return {
        "paused": False,
        "sell_all": False,
        "rsi_override": None,
        "stop_pct_override": None,
        "allowed_coins": None,
        "tranche_override": None,
        "notes": []
    }


def save_runtime_config(cfg):
    with open(RUNTIME_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def parse_command(text: str, cfg: dict) -> str:
    """Parse a natural-language command and update cfg in place. Returns description of action taken."""
    t = text.lower().strip()
    actions = []

    # Pause / resume buying
    if any(x in t for x in ["pause", "stop buying", "halt buying", "no new buys"]):
        cfg["paused"] = True
        actions.append("Buying PAUSED — bots will not open new positions")
    elif any(x in t for x in ["resume", "unpause", "start buying", "re-enable"]):
        cfg["paused"] = False
        cfg["sell_all"] = False
        actions.append("Buying RESUMED")

    # Sell everything
    if any(x in t for x in ["sell everything", "sell all", "exit all", "close all", "liquidate"]):
        cfg["sell_all"] = True
        cfg["paused"] = True
        actions.append("SELL ALL triggered — bots will exit all positions and pause buying")

    # RSI threshold
    rsi_match = re.search(r"rsi\s*(?:to|=|at|threshold)?\s*(\d+)", t)
    if rsi_match:
        val = int(rsi_match.group(1))
        if 10 <= val <= 50:
            cfg["rsi_override"] = val
            actions.append(f"RSI buy threshold set to {val}")

    # Tighter / looser stops
    if any(x in t for x in ["tighter stop", "tighten stop", "tighter risk", "smaller stop"]):
        cfg["stop_pct_override"] = 0.010  # 1%
        actions.append("Stop-loss tightened to 1%")
    elif any(x in t for x in ["looser stop", "widen stop", "wider stop", "relax stop"]):
        cfg["stop_pct_override"] = 0.025  # 2.5%
        actions.append("Stop-loss loosened to 2.5%")
    elif any(x in t for x in ["normal stop", "default stop", "reset stop"]):
        cfg["stop_pct_override"] = None
        actions.append("Stop-loss reset to per-coin defaults")

    # Coin filter
    coin_filter_match = re.search(r"(btc|eth|sol|avax|link|pol)[^\w]*(?:and|[+,&])[^\w]*(btc|eth|sol|avax|link|pol)\s*only", t)
    if coin_filter_match:
        coins = [coin_filter_match.group(1).upper(), coin_filter_match.group(2).upper()]
        cfg["allowed_coins"] = coins
        actions.append(f"Coin filter set — only trading: {', '.join(coins)}")
    elif re.search(r"\b(btc|eth|sol|avax|link|pol)\s+only\b", t):
        m = re.search(r"\b(btc|eth|sol|avax|link|pol)\s+only\b", t)
        cfg["allowed_coins"] = [m.group(1).upper()]
        actions.append(f"Coin filter set — only trading: {m.group(1).upper()}")
    elif any(x in t for x in ["all coins", "all crypto", "all assets", "remove filter", "reset coins"]):
        cfg["allowed_coins"] = None
        actions.append("Coin filter cleared — trading all coins")

    # Tranche size
    tranche_match = re.search(r"tranche\s*(?:to|=|at)?\s*\$?(\d+(?:\.\d+)?)", t)
    if tranche_match:
        val = float(tranche_match.group(1))
        if 3.0 <= val <= 50.0:
            cfg["tranche_override"] = val
            actions.append(f"Tranche size set to ${val:.2f}")

    if not actions:
        return f"Command not recognized: '{text}'"

    return "; ".join(actions)


def process_commands():
    if not os.path.exists(COMMANDS_FILE):
        return  # No commands pending

    with open(COMMANDS_FILE) as f:
        data = json.load(f)

    commands = data.get("commands", [])
    if not commands:
        os.remove(COMMANDS_FILE)
        return

    cfg = load_runtime_config()
    cfg.setdefault("notes", [])

    for cmd in commands:
        text = cmd.get("text", "").strip()
        if not text:
            continue
        result = parse_command(text, cfg)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"CMD: '{text}' → {result}")
        cfg["notes"].append({"ts": ts, "cmd": text, "result": result})

    # Keep only last 20 notes
    cfg["notes"] = cfg["notes"][-20:]
    save_runtime_config(cfg)

    # Archive processed commands
    done_file = COMMANDS_FILE.replace(".json", "_done.json")
    with open(done_file, "w") as f:
        json.dump({"processed_at": datetime.now().isoformat(), "commands": commands}, f, indent=2)
    os.remove(COMMANDS_FILE)


def get_runtime_config():
    """Called by each scalper to check current runtime overrides."""
    return load_runtime_config()


if __name__ == "__main__":
    process_commands()
    cfg = load_runtime_config()
    print(json.dumps(cfg, indent=2))
