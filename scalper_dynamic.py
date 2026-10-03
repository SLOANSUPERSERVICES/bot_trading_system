"""
scalper_dynamic.py — DYNAMIC TRADER Bot v1.0
Technical scoring (0–9 pts) → adaptive targets + tranche sizing
Coins: BTC, ETH, SOL, LINK
Daily cap: $20 | Cycle: 5 min | Cooldown: 2h after stop

Score  Target   Stop   Tranche   Mode
1-2    0.8%     0.5%   $3        Fixed
3-4    1.5%     0.8%   $5        Fixed
5-6    2.5%     1.2%   $8        Fixed
7-8    4.0%     1.8%   $12       Fixed
9      trailing  2.5%  $15       Trailing stop (0.8% trail)
"""

import os, sys, json, time, logging, math
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import robin_stocks.robinhood as rh
from colorama import init, Fore, Style

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
STATE_DIR  = BASE_DIR / "state"
LOG_DIR    = BASE_DIR / "logs"
SYS_DIR    = BASE_DIR / "system"
ENV_FILE   = BASE_DIR / ".env"

STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
SYS_DIR.mkdir(exist_ok=True)

STATE_FILE     = STATE_DIR  / "dynamic_state.json"
CONSENSUS_FILE = SYS_DIR    / "signal_consensus.json"
LOG_FILE       = LOG_DIR    / f"log_dynamic_{datetime.now():%Y%m%d}.txt"

# ── Auth ──────────────────────────────────────────────────────────────────────
load_dotenv(ENV_FILE)
init(autoreset=True)

BEARER_TOKEN = os.environ.get("RH_BEARER_TOKEN", "")
if BEARER_TOKEN:
    rh.authentication.set_login_state(True)
    rh.authentication.SESSION.headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
else:
    rh.login(os.environ["RH_USER"], os.environ["RH_PASS"])

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("DYNAMIC")

# ── Config ────────────────────────────────────────────────────────────────────
COINS = ["BTC", "ETH", "SOL", "LINK"]

DAILY_CAP       = 20.00   # max profit target per day
DAILY_LOSS_LIMIT = -3.00  # stop trading for the day if P&L hits this
CYCLE_SECS      = 300     # 5-minute cycle
COOLDOWN_SECS   = 7200    # 2h cooldown after a stop fires
MIN_SCORE       = 3       # minimum score to enter a trade
MAX_SPREAD_PCT  = 0.015   # 1.5% — skip if spread wider (volatility gate)

# RSI / BB config
RSI_PERIOD  = 14
BB_PERIOD   = 20
BB_MULT     = 2.0

# Score → (target_pct, stop_pct, tranche_usd, trailing)
SCORE_TABLE = {
    (1, 2): (0.008, 0.005, 3.00,  False),
    (3, 4): (0.015, 0.008, 5.00,  False),
    (5, 6): (0.025, 0.012, 8.00,  False),
    (7, 8): (0.040, 0.018, 12.00, False),
    (9, 9): (0.040, 0.025, 15.00, True),   # trailing; 4% is the activation
}
TRAIL_STEP = 0.008   # 0.8% trail distance

# ── Helpers ──────────────────────────────────────────────────────────────────
def lookup_score_params(score: int):
    for (lo, hi), params in SCORE_TABLE.items():
        if lo <= score <= hi:
            return params
    return SCORE_TABLE[(1, 2)]   # default to lowest tier


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_g  = sum(gains[-period:])  / period
    avg_l  = sum(losses[-period:]) / period or 1e-9
    rs     = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def bollinger(closes: list, period: int = 20, mult: float = 2.0):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    window = closes[-period:]
    mean   = sum(window) / period
    std    = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
    return mean + mult * std, mean, mean - mult * std


def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    window = trs[-period:]
    return sum(window) / len(window)


def get_historicals(sym: str, interval="5minute", span="day"):
    """Fetch OHLCV list from Robinhood, return (closes, highs, lows, volumes)."""
    try:
        data = rh.crypto.get_crypto_historicals(
            sym, interval=interval, span=span, bounds="24_7"
        )
        if not data:
            return [], [], [], []
        closes  = [safe_float(d.get("close_price"))  for d in data]
        highs   = [safe_float(d.get("high_price"))   for d in data]
        lows    = [safe_float(d.get("low_price"))    for d in data]
        volumes = [safe_float(d.get("volume"))       for d in data]
        return closes, highs, lows, volumes
    except Exception as e:
        log.warning(f"get_historicals({sym}): {e}")
        return [], [], [], []


def consensus_count(sym: str) -> int:
    """Count how many other bots are signalling BUY for this coin."""
    try:
        if not CONSENSUS_FILE.exists():
            return 0
        data = json.loads(CONSENSUS_FILE.read_text())
        coin_data = data.get(sym, {})
        return sum(
            1
            for bot, info in coin_data.items()
            if bot != "DYNAMIC"
            and isinstance(info, dict)
            and info.get("signal") == "buy"
        )
    except Exception:
        return 0


def sentiment_veto() -> bool:
    """Return True if SENTIMENT bot is blocking buys (score < 4)."""
    try:
        if not CONSENSUS_FILE.exists():
            return False
        data = json.loads(CONSENSUS_FILE.read_text())
        for sym_data in data.values():
            sent = sym_data.get("SENTIMENT", {})
            if isinstance(sent, dict) and sent.get("signal") == "hold":
                score = sent.get("score", 10)
                if score is not None and score < 4:
                    return True
        return False
    except Exception:
        return False


def ml_avoid(sym: str) -> bool:
    """Return True if ML bot is signalling AVOID for this coin."""
    try:
        if not CONSENSUS_FILE.exists():
            return False
        data = json.loads(CONSENSUS_FILE.read_text())
        ml_info = data.get(sym, {}).get("ML", {})
        return isinstance(ml_info, dict) and ml_info.get("signal") == "avoid"
    except Exception:
        return False


def write_consensus(sym: str, signal: str, score: int):
    """Write this bot's signal for a coin into signal_consensus.json."""
    try:
        data = {}
        if CONSENSUS_FILE.exists():
            data = json.loads(CONSENSUS_FILE.read_text())
        if sym not in data:
            data[sym] = {}
        data[sym]["DYNAMIC"] = {
            "signal": signal,
            "score":  score,
            "ts":     datetime.now().isoformat(),
        }
        CONSENSUS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning(f"write_consensus({sym}): {e}")


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_trade(sym: str, price: float, closes: list, highs: list,
                lows: list, volumes: list) -> int:
    score = 0

    if len(closes) < BB_PERIOD + 2:
        return 0

    current_rsi = rsi(closes, RSI_PERIOD)
    _, bb_mid, bb_lower = bollinger(closes, BB_PERIOD, BB_MULT)

    # ── RSI component (0–2 pts) ───────────────────────────────────────────
    if current_rsi < 30:
        score += 2
    elif current_rsi < 42:
        score += 1

    # ── Bollinger position (0–2 pts) ──────────────────────────────────────
    pct_below = (bb_lower - price) / bb_lower * 100
    if pct_below > 1.0:
        score += 2
    elif pct_below >= 0:
        score += 1

    # ── Volume surge (0–2 pts) ────────────────────────────────────────────
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:-1]) / 19
        if avg_vol > 0 and volumes[-1] > avg_vol * 1.5:
            score += 2
        elif avg_vol > 0 and volumes[-1] > avg_vol * 1.2:
            score += 1

    # ── Momentum inflection (0–1 pt) ─────────────────────────────────────
    # Price recovering: last 3 bars each higher than the previous
    if len(closes) >= 4 and closes[-1] > closes[-2] and closes[-2] > closes[-3]:
        score += 1

    # ── ATR expansion (0–1 pt): volatility calming ────────────────────────
    if len(closes) >= 28:
        atr_now  = atr(highs[-14:], lows[-14:], closes[-14:], 14)
        atr_prev = atr(highs[-28:-14], lows[-28:-14], closes[-28:-14], 14)
        if atr_prev > 0 and atr_now < atr_prev * 0.9:   # ATR shrinking
            score += 1

    # ── Cross-bot consensus bonus (0–1 pt) ────────────────────────────────
    if consensus_count(sym) >= 2:
        score += 1

    return min(score, 9)


# ── State management ─────────────────────────────────────────────────────────
def load_state() -> dict:
    default = {
        "positions":    {},      # sym → {qty, entry, target, stop, trailing, peak, score}
        "daily_pnl":    0.0,
        "last_reset":   datetime.now().date().isoformat(),
        "last_stop_ts": {},      # sym → ISO timestamp of last stop
        "closed_today": [],      # list of closed trade records
    }
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            # Reset daily P&L if it's a new day
            if saved.get("last_reset") != datetime.now().date().isoformat():
                saved["daily_pnl"]    = 0.0
                saved["last_reset"]   = datetime.now().date().isoformat()
                saved["closed_today"] = []
            return {**default, **saved}
        except Exception:
            pass
    return default


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Trade execution ───────────────────────────────────────────────────────────
def get_price(sym: str) -> tuple[float, float, float]:
    """Return (bid, ask, mid) for a crypto symbol."""
    try:
        q = rh.crypto.get_crypto_quote(sym)
        bid = safe_float(q.get("bid_inclusive_of_buy_spread"))
        ask = safe_float(q.get("ask_inclusive_of_sell_spread"))
        mid = (bid + ask) / 2 if bid and ask else 0.0
        return bid, ask, mid
    except Exception as e:
        log.warning(f"get_price({sym}): {e}")
        return 0.0, 0.0, 0.0


def buy(sym: str, usd: float) -> dict | None:
    try:
        result = rh.orders.order_buy_crypto_by_price(sym, round(usd, 2))
        return result
    except Exception as e:
        log.error(f"buy({sym}, ${usd:.2f}): {e}")
        return None


def sell(sym: str, qty: float) -> dict | None:
    try:
        result = rh.orders.order_sell_crypto_by_quantity(sym, round(qty, 6))
        return result
    except Exception as e:
        log.error(f"sell({sym}, {qty}): {e}")
        return None


# ── Main cycle ────────────────────────────────────────────────────────────────
def run_cycle(state: dict) -> dict:
    now      = datetime.now()
    pnl      = state["daily_pnl"]
    positions = state["positions"]

    # ── Check daily limits ────────────────────────────────────────────────
    if pnl >= DAILY_CAP:
        log.info(f"{Fore.GREEN}Daily cap reached (${pnl:.2f}). Resting.{Style.RESET_ALL}")
        return state
    if pnl <= DAILY_LOSS_LIMIT:
        log.info(f"{Fore.RED}Daily loss limit hit (${pnl:.2f}). Paused for the day.{Style.RESET_ALL}")
        return state

    # ── Sentiment veto ────────────────────────────────────────────────────
    sent_veto = sentiment_veto()
    if sent_veto:
        log.info(f"{Fore.YELLOW}SENTIMENT bot blocking buys (fear/score < 4).{Style.RESET_ALL}")

    for sym in COINS:

        # ── Fetch price + check spread ────────────────────────────────────
        bid, ask, mid = get_price(sym)
        if not mid:
            continue
        spread_pct = (ask - bid) / mid if mid else 1.0
        if spread_pct > MAX_SPREAD_PCT:
            log.info(f"{sym}: spread {spread_pct:.2%} too wide — skipping")
            continue

        price = ask   # buy at ask

        # ── Fetch historicals ─────────────────────────────────────────────
        closes, highs, lows, volumes = get_historicals(sym)
        if len(closes) < BB_PERIOD + 2:
            log.info(f"{sym}: insufficient history")
            continue

        # ── Manage open position ──────────────────────────────────────────
        if sym in positions:
            pos  = positions[sym]
            qty  = pos["qty"]
            entry = pos["entry"]
            target = pos["target"]
            stop   = pos["stop"]
            trailing = pos.get("trailing", False)
            peak  = pos.get("peak", entry)
            score = pos.get("score", 3)

            current = bid   # sell at bid

            # Update peak for trailing stop
            if trailing and current > peak:
                pos["peak"] = current
                trail_stop  = current * (1 - TRAIL_STEP)
                pos["stop"] = max(stop, trail_stop)
                stop = pos["stop"]
                log.info(f"{sym}: trailing — peak ${current:.4f}, stop now ${stop:.4f}")

            pnl_pct  = (current - entry) / entry
            pnl_usd  = (current - entry) * qty

            # ── Stop hit ──────────────────────────────────────────────────
            if current <= stop:
                log.info(
                    f"{Fore.RED}{sym}: STOP HIT @ ${current:.4f} "
                    f"(entry ${entry:.4f}, P&L {pnl_pct:.2%} / ${pnl_usd:.2f}){Style.RESET_ALL}"
                )
                pos["qty"] = 0
                result = sell(sym, qty)
                if result:
                    state["daily_pnl"] += pnl_usd
                    state["last_stop_ts"][sym] = now.isoformat()
                    state["closed_today"].append({
                        "sym": sym, "entry": entry, "exit": current,
                        "pnl_usd": pnl_usd, "score": score, "reason": "stop",
                        "ts": now.isoformat(),
                    })
                    del positions[sym]
                    write_consensus(sym, "hold", 0)
                continue

            # ── Target hit ────────────────────────────────────────────────
            if not trailing and current >= target:
                log.info(
                    f"{Fore.GREEN}{sym}: TARGET HIT @ ${current:.4f} "
                    f"(entry ${entry:.4f}, P&L {pnl_pct:.2%} / ${pnl_usd:.2f}){Style.RESET_ALL}"
                )
                pos["qty"] = 0
                result = sell(sym, qty)
                if result:
                    state["daily_pnl"] += pnl_usd
                    state["closed_today"].append({
                        "sym": sym, "entry": entry, "exit": current,
                        "pnl_usd": pnl_usd, "score": score, "reason": "target",
                        "ts": now.isoformat(),
                    })
                    del positions[sym]
                    write_consensus(sym, "hold", 0)
                continue

            log.info(
                f"{sym}: holding — entry ${entry:.4f} | now ${current:.4f} | "
                f"P&L {pnl_pct:.2%} | target ${target:.4f} | stop ${stop:.4f}"
            )
            continue

        # ── No open position — evaluate entry ─────────────────────────────

        # Cooldown check
        last_stop = state["last_stop_ts"].get(sym)
        if last_stop:
            elapsed = (now - datetime.fromisoformat(last_stop)).total_seconds()
            if elapsed < COOLDOWN_SECS:
                remaining = int((COOLDOWN_SECS - elapsed) / 60)
                log.info(f"{sym}: cooldown — {remaining}m remaining")
                continue

        # ML veto check
        if ml_avoid(sym):
            log.info(f"{sym}: ML TRADER signalling AVOID — skipping")
            write_consensus(sym, "hold", 0)
            continue

        # Score the trade
        score = score_trade(sym, price, closes, highs, lows, volumes)
        write_consensus(sym, "buy" if score >= MIN_SCORE else "hold", score)

        if score < MIN_SCORE:
            log.info(f"{sym}: score {score} < {MIN_SCORE} — no trade")
            continue

        if sent_veto and score < 6:
            log.info(f"{sym}: SENTIMENT veto + score {score} < 6 — skipping")
            continue

        # Daily cap check for tranche
        target_pct, stop_pct, tranche_usd, trailing = lookup_score_params(score)
        if pnl + tranche_usd * target_pct > DAILY_CAP * 1.1:
            log.info(f"{sym}: daily cap close — limiting entry")
            tranche_usd = min(tranche_usd, max(3.0, DAILY_CAP - pnl) * 0.5)

        if tranche_usd < 1.0:
            log.info(f"{sym}: tranche too small — daily cap nearly done")
            continue

        # ── ENTER ─────────────────────────────────────────────────────────
        result = buy(sym, tranche_usd)
        if not result:
            continue

        # Estimate qty from fill
        qty_filled = safe_float(result.get("filled_asset_quantity")) or tranche_usd / price

        target_price = price * (1 + target_pct)
        stop_price   = price * (1 - stop_pct)

        positions[sym] = {
            "qty":      qty_filled,
            "entry":    price,
            "target":   target_price,
            "stop":     stop_price,
            "trailing": trailing,
            "peak":     price,
            "score":    score,
            "opened":   now.isoformat(),
        }

        tier = "TRAILING" if trailing else f"target ${target_price:.4f}"
        log.info(
            f"{Fore.CYAN}{sym}: BUY ${tranche_usd:.2f} @ ${price:.4f} "
            f"| score {score} | {tier} | stop ${stop_price:.4f}{Style.RESET_ALL}"
        )

    state["positions"] = positions
    return state


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log.info(f"{Fore.MAGENTA}═══ DYNAMIC TRADER v1.0 starting ══════════════════{Style.RESET_ALL}")
    log.info(f"Coins: {COINS} | Daily cap: ${DAILY_CAP} | Min score: {MIN_SCORE}")

    state = load_state()

    while True:
        try:
            log.info(f"── Cycle {datetime.now():%H:%M:%S} | P&L today: ${state['daily_pnl']:.2f} ──")
            state = run_cycle(state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Shutting down DYNAMIC TRADER.")
            save_state(state)
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
        time.sleep(CYCLE_SECS)


if __name__ == "__main__":
    main()
