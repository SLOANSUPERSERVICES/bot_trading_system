"""
SENTIMENT TRADER — NLP sentiment-based crypto scalper for BTC/ETH
Uses CryptoPanic headlines + Fear & Greed Index to score sentiment (0–10),
then trades on Robinhood accordingly.
"""

from pathlib import Path
from datetime import datetime, date
import os
import json
import time
import logging

import requests
import requests.exceptions
from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
import robin_stocks.robinhood as rh

# ── Auth ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
BEARER_TOKEN = os.environ.get("RH_BEARER_TOKEN", "")
if BEARER_TOKEN:
    rh.authentication.set_login_state(True)
    rh.authentication.SESSION.headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
else:
    rh.login(os.environ["RH_USER"], os.environ["RH_PASS"])

# ── Config ────────────────────────────────────────────────────────────────────
BOT_NAME        = "SENTIMENT TRADER"
COINS           = ["BTC", "ETH"]
DAILY_CAP       = 10.00
DAILY_LOSS_LIM  = -2.00
CYCLE_SECONDS   = 600
TRANCHE         = 4.00
TARGET_PROFIT   = 0.012   # 1.2 %
STOP_LOSS       = 0.008   # 0.8 %

CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/v1/posts/"
    "?auth_token=anonymous&currencies=BTC,ETH&public=true"
)
FEAR_GREED_URL  = "https://api.alternative.me/fng/?limit=1"

# Paths (relative to repo root, same as other bots)
STATE_FILE     = BASE_DIR / "state"  / "sentiment_state.json"
CONSENSUS_FILE = BASE_DIR / "system" / "signal_consensus.json"
LOG_DIR        = BASE_DIR / "logs"

colorama_init(autoreset=True)

# ── Logging ───────────────────────────────────────────────────────────────────
def get_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"log_sentiment_{date.today().strftime('%Y%m%d')}.txt"
    logger = logging.getLogger(BOT_NAME)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    return logger

log = get_logger()

# ── State helpers ─────────────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "date":        str(date.today()),
        "daily_spend": 0.0,
        "daily_loss":  0.0,
        "positions":   {},  # coin -> {"qty": float, "entry_price": float}
    }

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") != str(date.today()):
                log.info("New day — resetting daily counters.")
                s = _default_state()
            return s
        except (json.JSONDecodeError, KeyError):
            pass
    return _default_state()

def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2))

# ── Consensus helpers ─────────────────────────────────────────────────────────
def load_consensus() -> dict:
    CONSENSUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if CONSENSUS_FILE.exists():
        try:
            return json.loads(CONSENSUS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}

def write_consensus(coin: str, signal: str, score: float) -> None:
    c = load_consensus()
    c.setdefault("SENTIMENT", {})[coin] = {
        "signal": signal,
        "score":  round(score, 4),
        "ts":     datetime.utcnow().isoformat(),
    }
    CONSENSUS_FILE.write_text(json.dumps(c, indent=2))

def ml_says_avoid(coin: str) -> bool:
    c = load_consensus()
    ml = c.get("ML TRADER", {}).get(coin, {})
    return ml.get("signal") == "AVOID"

# ── Sentiment scoring ─────────────────────────────────────────────────────────
def fetch_cryptopanic() -> list[dict]:
    """Return list of post dicts from CryptoPanic (may be empty on failure)."""
    try:
        r = requests.get(CRYPTOPANIC_URL, timeout=10)
        r.raise_for_status()
        return r.json().get("results", [])
    except requests.exceptions.RequestException as exc:
        log.warning(f"CryptoPanic unavailable: {exc} — skipping headline scoring.")
        return []

def fetch_fear_greed() -> int | None:
    """Return Fear & Greed value (0–100) or None on failure."""
    try:
        r = requests.get(FEAR_GREED_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        return int(data["data"][0]["value"])
    except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as exc:
        log.warning(f"Fear & Greed API unavailable: {exc}")
        return None

def score_sentiment() -> float:
    """Return sentiment score 0–10."""
    score = 5.0  # neutral baseline

    # ── CryptoPanic headlines ─────────────────────────────────────────────
    posts = fetch_cryptopanic()
    bullish_delta = 0.0
    bearish_delta = 0.0
    for post in posts:
        kind = (post.get("kind") or "").lower()
        votes = post.get("votes", {}) or {}
        # CryptoPanic uses 'positive'/'negative' vote counts; also check kind
        positive = votes.get("positive", 0) or 0
        negative = votes.get("negative", 0) or 0
        if kind == "news":
            # Simple heuristic: rely on vote counts
            if positive > negative:
                bullish_delta = min(bullish_delta + 0.3, 3.0)
            elif negative > positive:
                bearish_delta = max(bearish_delta - 0.3, -3.0)

    score += bullish_delta + bearish_delta

    # ── Fear & Greed ──────────────────────────────────────────────────────
    fg = fetch_fear_greed()
    if fg is not None:
        if fg <= 25:
            score -= 2
        elif fg <= 45:
            score -= 1
        elif fg <= 55:
            pass  # neutral
        elif fg <= 75:
            score += 1
        else:
            score += 2

    score = max(0.0, min(10.0, score))
    return score

# ── Robinhood helpers ─────────────────────────────────────────────────────────
def get_crypto_price(coin: str) -> float | None:
    try:
        symbol = f"{coin}-USD"
        quotes = rh.crypto.get_crypto_quote(symbol)
        if quotes:
            return float(quotes.get("mark_price") or quotes.get("ask_price") or 0)
    except Exception as exc:
        log.warning(f"Price fetch failed for {coin}: {exc}")
    return None

def place_buy(coin: str, usd_amount: float) -> dict | None:
    symbol = f"{coin}-USD"
    try:
        order = rh.orders.order_buy_crypto_by_price(symbol, usd_amount)
        return order
    except Exception as exc:
        log.error(f"Buy order failed for {coin}: {exc}")
        return None

def place_sell(coin: str, qty: float) -> dict | None:
    symbol = f"{coin}-USD"
    try:
        order = rh.orders.order_sell_crypto_by_quantity(symbol, qty)
        return order
    except Exception as exc:
        log.error(f"Sell order failed for {coin}: {exc}")
        return None

# ── Main cycle ────────────────────────────────────────────────────────────────
def run_cycle(state: dict, score: float) -> dict:
    for coin in COINS:
        # ── Check exits first ─────────────────────────────────────────────
        pos = state["positions"].get(coin)
        if pos:
            price = get_crypto_price(coin)
            if price is None:
                log.warning(f"  [{coin}] Cannot fetch price — skipping exit check.")
                continue

            entry = pos["entry_price"]
            qty   = pos["qty"]
            pct   = (price - entry) / entry

            if pct >= TARGET_PROFIT:
                log.info(
                    Fore.GREEN
                    + f"  [{coin}] TARGET HIT {pct*100:.2f}% — selling {qty:.6f}"
                )
                order = place_sell(coin, qty)
                if order:
                    gain = (price - entry) * qty
                    state["daily_loss"] += gain  # positive gain reduces net loss
                    del state["positions"][coin]
                    write_consensus(coin, "HOLD", score)

            elif pct <= -STOP_LOSS:
                loss = (price - entry) * qty
                log.info(
                    Fore.RED
                    + f"  [{coin}] STOP LOSS {pct*100:.2f}% — selling {qty:.6f}"
                )
                order = place_sell(coin, qty)
                if order:
                    state["daily_loss"] += loss   # negative
                    del state["positions"][coin]
                    write_consensus(coin, "HOLD", score)

            else:
                log.info(f"  [{coin}] HOLDING  entry={entry:.4f}  now={price:.4f}  {pct*100:+.2f}%")
            continue  # don't evaluate a new entry while in position

        # ── Entry logic ───────────────────────────────────────────────────
        if score >= 6:
            signal = "BUY"
        elif score < 4:
            signal = "AVOID"
        else:
            signal = "HOLD"

        write_consensus(coin, signal, score)

        if signal != "BUY":
            log.info(f"  [{coin}] Signal={signal}  score={score:.2f} — no trade.")
            continue

        if state["daily_spend"] + TRANCHE > DAILY_CAP:
            log.info(f"  [{coin}] Daily cap reached — skipping buy.")
            continue

        if state["daily_loss"] <= DAILY_LOSS_LIM:
            log.info(
                Fore.RED
                + f"  [{coin}] Daily loss limit hit ({state['daily_loss']:.2f}) — no more buys."
            )
            continue

        if ml_says_avoid(coin):
            log.info(f"  [{coin}] ML TRADER says AVOID — skipping buy.")
            continue

        price = get_crypto_price(coin)
        if price is None:
            log.warning(f"  [{coin}] No price — cannot place buy.")
            continue

        log.info(
            Fore.GREEN
            + f"  [{coin}] BUY  score={score:.2f}  price={price:.4f}  ${TRANCHE:.2f}"
        )
        order = place_buy(coin, TRANCHE)
        if order:
            # Estimate qty from tranche / price (actual fill qty may differ)
            qty = TRANCHE / price
            state["positions"][coin] = {"qty": qty, "entry_price": price}
            state["daily_spend"] += TRANCHE

    return state

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    print(
        Fore.CYAN + Style.BRIGHT
        + f"\n{'='*55}\n  {BOT_NAME}  |  BTC & ETH  |  cycle={CYCLE_SECONDS}s\n{'='*55}"
    )

    while True:
        state = load_state()

        log.info(Fore.CYAN + f"\n[{datetime.now().strftime('%H:%M:%S')}] === {BOT_NAME} cycle ===")
        log.info(
            f"  Daily spend: ${state['daily_spend']:.2f}/{DAILY_CAP:.2f}"
            f"   Daily P&L: ${state['daily_loss']:.2f}"
        )

        score = score_sentiment()
        log.info(
            Fore.YELLOW
            + f"  Sentiment score: {score:.2f}/10"
            + ("  (BULLISH)" if score >= 6 else "  (BEARISH)" if score < 4 else "  (NEUTRAL)")
        )

        # Guard: if daily loss limit breached, log and sleep
        if state["daily_loss"] <= DAILY_LOSS_LIM:
            log.info(
                Fore.RED
                + f"  Daily loss limit reached ({state['daily_loss']:.2f}) — no trading this cycle."
            )
        else:
            state = run_cycle(state, score)

        save_state(state)
        log.info(f"  Sleeping {CYCLE_SECONDS}s …\n")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
