"""
Bot 7: ML TRADER — Live ML-driven crypto scalper
Coins: BTC, ETH, SOL | Daily cap: $15.00 | Daily loss limit: -$3.00
Cycle: 900s (15 min) | Target: 2.0% | Stop: 1.0%
"""

from pathlib import Path
from dotenv import load_dotenv
import os, robin_stocks.robinhood as rh

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
BEARER_TOKEN = os.environ.get("RH_BEARER_TOKEN", "")
if BEARER_TOKEN:
    rh.authentication.set_login_state(True)
    rh.authentication.SESSION.headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
else:
    rh.login(os.environ["RH_USER"], os.environ["RH_PASS"])

import time
import json
import pickle
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, date
from colorama import init, Fore, Style

init(autoreset=True)
warnings.filterwarnings("ignore")

# ── Configuration ────────────────────────────────────────────────────────────
BOT_NAME       = "ML TRADER"
COINS          = ["BTC", "ETH", "SOL"]
YF_TICKERS     = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
DAILY_CAP      = 15.00
DAILY_LOSS_LIM = -3.00
CYCLE_SECS     = 900
TRANCHE        = 6.00
REDUCED_TRANCHE= 3.00       # when ML signals AVOID to other bots
TARGET_PCT     = 0.02       # 2.0%
STOP_PCT       = 0.01       # 1.0%
TRAIL_STEP     = 0.008      # trailing stop step for high-confidence trades
CONF_THRESHOLD = 0.65       # model agreement threshold
AVOID_THRESHOLD= 0.35       # flip to AVOID if any prob < this
TRAIL_CONF_MIN = 0.80       # avg confidence required for trailing stop
LSTM_SEQ_LEN   = 20         # LSTM input sequence length
MIN_CANDLES    = 25         # minimum candles required to trade

# ── Paths ─────────────────────────────────────────────────────────────────────
ML_DIR         = BASE_DIR / "ml" / "models"
STATE_FILE     = BASE_DIR / "state" / "ml_state.json"
CONSENSUS_FILE = BASE_DIR / "system" / "signal_consensus.json"
LOG_DIR        = BASE_DIR / "logs"
LOG_FILE       = LOG_DIR / f"log_ml_{date.today().strftime('%Y%m%d')}.txt"

# ── Model paths ───────────────────────────────────────────────────────────────
RF_PATH     = ML_DIR / "rf_model.pkl"
XGB_PATH    = ML_DIR / "xgb_model.pkl"
LSTM_PATH   = ML_DIR / "lstm_model.h5"
SCALER_PATH = ML_DIR / "scaler.pkl"

# ── Ensure directories exist ──────────────────────────────────────────────────
for d in [STATE_FILE.parent, CONSENSUS_FILE.parent, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            # strip ANSI codes for file
            import re
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
            f.write(clean + "\n")
    except Exception:
        pass


# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    default = {
        "daily_spent": 0.0,
        "daily_pnl":   0.0,
        "date":        str(date.today()),
        "positions":   {}
    }
    try:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") != str(date.today()):
                log(f"{Fore.YELLOW}New day — resetting daily counters.")
                default["positions"] = s.get("positions", {})
                return default
            return s
    except Exception as e:
        log(f"{Fore.RED}State load error: {e}")
    return default


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"{Fore.RED}State save error: {e}")


# ── Consensus helpers ─────────────────────────────────────────────────────────
def read_consensus() -> dict:
    try:
        if CONSENSUS_FILE.exists():
            return json.loads(CONSENSUS_FILE.read_text())
    except Exception:
        pass
    return {}


def write_consensus(coin: str, signal: str, confidence: float):
    consensus = read_consensus()
    if BOT_NAME not in consensus:
        consensus[BOT_NAME] = {}
    consensus[BOT_NAME][coin] = {
        "signal":     signal,
        "confidence": round(confidence, 4),
        "ts":         datetime.now().isoformat()
    }
    try:
        CONSENSUS_FILE.write_text(json.dumps(consensus, indent=2))
    except Exception as e:
        log(f"{Fore.RED}Consensus write error: {e}")


def sentiment_blocked(consensus: dict) -> bool:
    """Return True if SENTIMENT score < 4 in consensus (skip all buys)."""
    try:
        sentiment = consensus.get("SENTIMENT", {})
        score = sentiment.get("score", 10)
        return float(score) < 4
    except Exception:
        return False


# ── Model loading ─────────────────────────────────────────────────────────────
def load_models():
    """Load RF, XGB, LSTM, and scaler. Returns (rf, xgb, lstm, scaler) or None if any missing."""
    missing = []
    for path, name in [(RF_PATH, "rf_model.pkl"), (XGB_PATH, "xgb_model.pkl"),
                       (LSTM_PATH, "lstm_model.h5"), (SCALER_PATH, "scaler.pkl")]:
        if not path.exists():
            missing.append(name)

    if missing:
        log(f"{Fore.YELLOW}⚠  Models not found: {', '.join(missing)}")
        log(f"{Fore.YELLOW}   Run train_ml.py first. Skipping trading this cycle.")
        return None

    try:
        with open(RF_PATH, "rb") as f:
            rf = pickle.load(f)
        with open(XGB_PATH, "rb") as f:
            xgb = pickle.load(f)
        with open(SCALER_PATH, "rb") as f:
            scaler = pickle.load(f)

        # LSTM: try keras/tensorflow
        try:
            from tensorflow.keras.models import load_model as keras_load
            lstm = keras_load(str(LSTM_PATH))
        except ImportError:
            try:
                from keras.models import load_model as keras_load
                lstm = keras_load(str(LSTM_PATH))
            except ImportError:
                log(f"{Fore.RED}TensorFlow/Keras not installed — cannot load LSTM.")
                return None

        log(f"{Fore.CYAN}Models loaded: RF, XGB, LSTM, Scaler ✓")
        return rf, xgb, lstm, scaler

    except Exception as e:
        log(f"{Fore.RED}Model load error: {e}")
        return None


# ── Feature engineering ───────────────────────────────────────────────────────
def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, min_periods=period).mean()
    return float(atr.iloc[-1])


def build_features(coin: str) -> np.ndarray | None:
    """
    Fetch yfinance data and compute 6-feature vector matching train_ml.py:
      [RSI14, BB_position, volume_ratio, momentum_5, ATR_norm, EMA_ratio]
    Returns shape (n_rows, 6) DataFrame or None on error.
    """
    ticker = YF_TICKERS[coin]
    try:
        df = yf.download(ticker, period="5d", interval="1h",
                         progress=False, auto_adjust=True)
    except Exception as e:
        log(f"{Fore.RED}yfinance error for {coin}: {e}")
        return None

    if df is None or len(df) < MIN_CANDLES:
        log(f"{Fore.YELLOW}{coin}: only {len(df) if df is not None else 0} candles returned "
            f"(need {MIN_CANDLES}) — skipping.")
        return None

    close  = df["Close"].squeeze()
    high   = df["High"].squeeze()
    low    = df["Low"].squeeze()
    volume = df["Volume"].squeeze()

    # 1. RSI(14)
    rsi_series = pd.Series(dtype=float)
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, min_periods=14).mean()
    avg_loss = loss.ewm(com=13, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_col = 100 - (100 / (1 + rs))

    # 2. Bollinger Band position (period=20, std=2)
    bb_mid   = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_pos   = (close - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)

    # 3. Volume ratio
    vol_ratio = volume / volume.rolling(20).mean()

    # 4. Price momentum (5-period pct_change)
    momentum = close.pct_change(5)

    # 5. ATR(14) normalized
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    atr      = tr.ewm(com=13, min_periods=14).mean()
    atr_norm = atr / close

    # 6. EMA ratio (span=20)
    ema20     = close.ewm(span=20).mean()
    ema_ratio = close / ema20

    features = pd.DataFrame({
        "rsi":       rsi_col,
        "bb_pos":    bb_pos,
        "vol_ratio": vol_ratio,
        "momentum":  momentum,
        "atr_norm":  atr_norm,
        "ema_ratio": ema_ratio
    })

    features = features.dropna()
    if len(features) < LSTM_SEQ_LEN + 1:
        log(f"{Fore.YELLOW}{coin}: insufficient clean rows after dropna ({len(features)}) — skipping.")
        return None

    return features


# ── ML prediction ─────────────────────────────────────────────────────────────
def predict(coin: str, models) -> dict:
    """
    Run all three models. Returns dict with keys:
      signal, confidence, rf_prob, xgb_prob, lstm_prob, agreed
    """
    rf, xgb, lstm, scaler = models
    features_df = build_features(coin)
    if features_df is None:
        return {"signal": "SKIP", "confidence": 0.0}

    X = features_df.values  # shape (n, 6)
    X_scaled = scaler.transform(X)

    # ── RF ──────────────────────────────────────────────────────────────
    try:
        rf_prob = float(rf.predict_proba(X_scaled[-1:])[:, 1][0])
    except Exception as e:
        log(f"{Fore.RED}RF predict error ({coin}): {e}")
        return {"signal": "SKIP", "confidence": 0.0}

    # ── XGB ─────────────────────────────────────────────────────────────
    try:
        xgb_prob = float(xgb.predict_proba(X_scaled[-1:])[:, 1][0])
    except Exception as e:
        log(f"{Fore.RED}XGB predict error ({coin}): {e}")
        return {"signal": "SKIP", "confidence": 0.0}

    # ── LSTM ─────────────────────────────────────────────────────────────
    try:
        seq = X_scaled[-LSTM_SEQ_LEN:]            # (20, 6)
        if len(seq) < LSTM_SEQ_LEN:
            log(f"{Fore.YELLOW}{coin}: LSTM sequence too short ({len(seq)}) — skipping.")
            return {"signal": "SKIP", "confidence": 0.0}
        seq_input = seq.reshape(1, LSTM_SEQ_LEN, 6)
        lstm_raw  = lstm.predict(seq_input, verbose=0)
        # Handle (1,1) or (1,) output shape
        lstm_prob = float(np.array(lstm_raw).flatten()[0])
    except Exception as e:
        log(f"{Fore.RED}LSTM predict error ({coin}): {e}")
        return {"signal": "SKIP", "confidence": 0.0}

    avg_conf = (rf_prob + xgb_prob + lstm_prob) / 3.0

    # ── Model detail logging ─────────────────────────────────────────────
    rf_color   = Fore.GREEN  if rf_prob   > CONF_THRESHOLD else (Fore.RED if rf_prob   < AVOID_THRESHOLD else Fore.YELLOW)
    xgb_color  = Fore.GREEN  if xgb_prob  > CONF_THRESHOLD else (Fore.RED if xgb_prob  < AVOID_THRESHOLD else Fore.YELLOW)
    lstm_color = Fore.GREEN  if lstm_prob > CONF_THRESHOLD else (Fore.RED if lstm_prob < AVOID_THRESHOLD else Fore.YELLOW)

    log(f"  {Fore.MAGENTA}[{coin}] Model probs → "
        f"RF: {rf_color}{rf_prob:.3f}{Style.RESET_ALL}  "
        f"XGB: {xgb_color}{xgb_prob:.3f}{Style.RESET_ALL}  "
        f"LSTM: {lstm_color}{lstm_prob:.3f}{Style.RESET_ALL}  "
        f"avg: {Fore.YELLOW}{avg_conf:.3f}{Style.RESET_ALL}")

    # ── AVOID logic ───────────────────────────────────────────────────────
    if rf_prob < AVOID_THRESHOLD or xgb_prob < AVOID_THRESHOLD or lstm_prob < AVOID_THRESHOLD:
        avoiders = []
        if rf_prob   < AVOID_THRESHOLD: avoiders.append(f"RF({rf_prob:.3f})")
        if xgb_prob  < AVOID_THRESHOLD: avoiders.append(f"XGB({xgb_prob:.3f})")
        if lstm_prob < AVOID_THRESHOLD: avoiders.append(f"LSTM({lstm_prob:.3f})")
        log(f"  {Fore.RED}[{coin}] AVOID signal from: {', '.join(avoiders)}")
        return {
            "signal":     "AVOID",
            "confidence": avg_conf,
            "rf_prob":    rf_prob,
            "xgb_prob":   xgb_prob,
            "lstm_prob":  lstm_prob,
            "agreed":     False
        }

    # ── BUY logic — all three must agree ──────────────────────────────────
    if rf_prob > CONF_THRESHOLD and xgb_prob > CONF_THRESHOLD and lstm_prob > CONF_THRESHOLD:
        log(f"  {Fore.GREEN}[{coin}] ALL THREE models agree → BUY  confidence={avg_conf:.3f}")
        return {
            "signal":     "BUY",
            "confidence": avg_conf,
            "rf_prob":    rf_prob,
            "xgb_prob":   xgb_prob,
            "lstm_prob":  lstm_prob,
            "agreed":     True
        }

    # ── HOLD ────────────────────────────────────────────────────────────
    disagreed = []
    if rf_prob   <= CONF_THRESHOLD: disagreed.append(f"RF({rf_prob:.3f})")
    if xgb_prob  <= CONF_THRESHOLD: disagreed.append(f"XGB({xgb_prob:.3f})")
    if lstm_prob <= CONF_THRESHOLD: disagreed.append(f"LSTM({lstm_prob:.3f})")
    log(f"  {Fore.YELLOW}[{coin}] HOLD — not all above {CONF_THRESHOLD}: {', '.join(disagreed)}")
    return {
        "signal":     "HOLD",
        "confidence": avg_conf,
        "rf_prob":    rf_prob,
        "xgb_prob":   xgb_prob,
        "lstm_prob":  lstm_prob,
        "agreed":     False
    }


# ── Robinhood helpers ─────────────────────────────────────────────────────────
def get_crypto_price(coin: str) -> float | None:
    try:
        quote = rh.crypto.get_crypto_quote(coin)
        if quote:
            return float(quote.get("mark_price") or quote.get("ask_price", 0))
    except Exception as e:
        log(f"{Fore.RED}Price fetch error ({coin}): {e}")
    return None


def get_crypto_positions() -> dict:
    """Return {coin: {"quantity": float, "avg_cost": float}}"""
    positions = {}
    try:
        raw = rh.crypto.get_crypto_positions()
        if not raw:
            return positions
        for p in raw:
            currency = p.get("currency", {}).get("code", "")
            qty      = float(p.get("quantity", 0))
            cost     = float(p.get("cost_bases", [{}])[0].get("direct_cost_basis", 0))
            if qty > 0.000001 and currency in COINS:
                avg = cost / qty if qty else 0
                positions[currency] = {"quantity": qty, "avg_cost": avg}
    except Exception as e:
        log(f"{Fore.RED}Position fetch error: {e}")
    return positions


def place_buy(coin: str, dollar_amount: float) -> bool:
    try:
        result = rh.orders.order_buy_crypto_by_price(
            symbol=coin,
            amountInDollars=round(dollar_amount, 2),
            timeInForce="gtc"
        )
        if result and result.get("id"):
            log(f"{Fore.GREEN}BUY order placed: ${dollar_amount:.2f} of {coin}  "
                f"order_id={result['id']}")
            return True
        log(f"{Fore.RED}BUY failed for {coin}: {result}")
    except Exception as e:
        log(f"{Fore.RED}BUY exception ({coin}): {e}")
    return False


def place_sell(coin: str, quantity: float) -> bool:
    try:
        result = rh.orders.order_sell_crypto_by_quantity(
            symbol=coin,
            quantity=quantity,
            timeInForce="gtc"
        )
        if result and result.get("id"):
            log(f"{Fore.GREEN}SELL order placed: {quantity:.8f} {coin}  "
                f"order_id={result['id']}")
            return True
        log(f"{Fore.RED}SELL failed for {coin}: {result}")
    except Exception as e:
        log(f"{Fore.RED}SELL exception ({coin}): {e}")
    return False


# ── Trade exit logic ───────────────────────────────────────────────────────────
def manage_exits(state: dict, rh_positions: dict) -> float:
    """
    Check open positions against targets, stops, and trailing stops.
    Returns net realized P&L from exits this cycle.
    """
    pnl_this_cycle = 0.0
    for coin in list(state["positions"].keys()):
        pos = state["positions"][coin]
        if coin not in rh_positions:
            log(f"{Fore.YELLOW}[{coin}] Position in state but not on RH — clearing state entry.")
            del state["positions"][coin]
            continue

        price = get_crypto_price(coin)
        if price is None:
            continue

        entry       = pos["entry_price"]
        qty         = pos["quantity"]
        cost        = entry * qty
        current_val = price * qty
        pct_chg     = (price - entry) / entry
        high_price  = pos.get("high_price", price)
        trailing    = pos.get("trailing", False)

        # update high watermark
        if price > high_price:
            pos["high_price"] = price
            high_price = price

        # ── Target hit ────────────────────────────────────────────────────
        if pct_chg >= TARGET_PCT:
            log(f"{Fore.GREEN}TARGET HIT [{coin}]  entry={entry:.4f}  "
                f"price={price:.4f}  pct={pct_chg*100:.2f}%")
            if place_sell(coin, qty):
                realized = current_val - cost
                pnl_this_cycle += realized
                state["daily_pnl"] += realized
                del state["positions"][coin]
            continue

        # ── Trailing stop (high-confidence trades only) ───────────────────
        if trailing:
            trail_trigger = high_price * (1 - TRAIL_STEP)
            if price <= trail_trigger:
                log(f"{Fore.RED}TRAIL STOP [{coin}]  high={high_price:.4f}  "
                    f"price={price:.4f}  trail={trail_trigger:.4f}")
                if place_sell(coin, qty):
                    realized = current_val - cost
                    pnl_this_cycle += realized
                    state["daily_pnl"] += realized
                    del state["positions"][coin]
                continue

        # ── Hard stop ────────────────────────────────────────────────────
        if pct_chg <= -STOP_PCT:
            log(f"{Fore.RED}STOP LOSS [{coin}]  entry={entry:.4f}  "
                f"price={price:.4f}  pct={pct_chg*100:.2f}%")
            if place_sell(coin, qty):
                realized = current_val - cost
                pnl_this_cycle += realized
                state["daily_pnl"] += realized
                del state["positions"][coin]
            continue

        log(f"  [{coin}] Holding  entry={entry:.4f}  price={price:.4f}  "
            f"pct={pct_chg*100:+.2f}%  "
            f"{'(trailing)' if trailing else ''}")

    return pnl_this_cycle


# ── Main trading loop ─────────────────────────────────────────────────────────
def main():
    log(f"\n{Fore.CYAN}{'='*60}")
    log(f"{Fore.CYAN}  {BOT_NAME} — starting up")
    log(f"{Fore.CYAN}  Coins: {', '.join(COINS)}  |  Cap: ${DAILY_CAP}  |  "
        f"Loss limit: ${DAILY_LOSS_LIM}  |  Cycle: {CYCLE_SECS}s")
    log(f"{Fore.CYAN}{'='*60}\n")

    while True:
        cycle_start = time.time()
        log(f"\n{Fore.CYAN}── Cycle {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──")

        # ── Load state ────────────────────────────────────────────────────
        state     = load_state()
        consensus = read_consensus()

        # ── Load models ───────────────────────────────────────────────────
        models = load_models()
        if models is None:
            log(f"{Fore.YELLOW}Waiting {CYCLE_SECS}s before retrying…")
            time.sleep(CYCLE_SECS)
            continue

        # ── Daily limits check ────────────────────────────────────────────
        if state["daily_pnl"] <= DAILY_LOSS_LIM:
            log(f"{Fore.RED}Daily loss limit reached "
                f"(${state['daily_pnl']:.2f} ≤ ${DAILY_LOSS_LIM}) — no new buys.")
        if state["daily_spent"] >= DAILY_CAP:
            log(f"{Fore.YELLOW}Daily cap reached (${state['daily_spent']:.2f}) — no new buys.")

        buys_allowed = (
            state["daily_pnl"] > DAILY_LOSS_LIM and
            state["daily_spent"] < DAILY_CAP
        )

        # ── Check sentiment gate ───────────────────────────────────────────
        if sentiment_blocked(consensus):
            log(f"{Fore.YELLOW}SENTIMENT score < 4 — skipping all buys this cycle.")
            buys_allowed = False

        # ── Check if ML previously signalled AVOID (use reduced tranche) ──
        ml_consensus = consensus.get(BOT_NAME, {})
        any_avoid = any(
            v.get("signal") == "AVOID"
            for v in ml_consensus.values()
        )
        tranche = REDUCED_TRANCHE if any_avoid else TRANCHE

        # ── Manage open positions / exits ──────────────────────────────────
        rh_positions = get_crypto_positions()
        pnl = manage_exits(state, rh_positions)
        if pnl != 0:
            log(f"  Cycle P&L from exits: ${pnl:+.2f}  "
                f"Daily P&L: ${state['daily_pnl']:+.2f}")

        # ── Run ML predictions for each coin ──────────────────────────────
        for coin in COINS:
            result = predict(coin, models)
            signal = result.get("signal", "SKIP")
            conf   = result.get("confidence", 0.0)

            if signal == "SKIP":
                continue

            # Write to consensus file
            if signal in ("BUY", "AVOID", "HOLD"):
                write_consensus(coin, signal, conf)

            # ── AVOID: already logged in predict(), nothing to trade ───────
            if signal == "AVOID":
                continue

            # ── HOLD: no action ────────────────────────────────────────────
            if signal == "HOLD":
                continue

            # ── BUY ────────────────────────────────────────────────────────
            if signal == "BUY" and buys_allowed:
                if coin in state["positions"]:
                    log(f"  {Fore.YELLOW}[{coin}] Already in position — skipping new buy.")
                    continue

                remaining_cap = DAILY_CAP - state["daily_spent"]
                spend = min(tranche, remaining_cap)
                if spend < 1.00:
                    log(f"  {Fore.YELLOW}[{coin}] Remaining cap too small (${spend:.2f}) — skipping.")
                    continue

                price = get_crypto_price(coin)
                if price is None:
                    continue

                use_trailing = conf > TRAIL_CONF_MIN
                log(f"  {Fore.GREEN}[{coin}] BUY  ${spend:.2f}  "
                    f"price={price:.4f}  conf={conf:.3f}  "
                    f"trailing={'YES' if use_trailing else 'NO'}")

                if place_buy(coin, spend):
                    qty = spend / price
                    state["positions"][coin] = {
                        "entry_price": price,
                        "quantity":    qty,
                        "spend":       spend,
                        "high_price":  price,
                        "trailing":    use_trailing,
                        "ts":          datetime.now().isoformat()
                    }
                    state["daily_spent"] += spend
                    log(f"  {Fore.CYAN}Daily spent: ${state['daily_spent']:.2f} / ${DAILY_CAP:.2f}")

        # ── Save state ────────────────────────────────────────────────────
        save_state(state)

        # ── Summary ───────────────────────────────────────────────────────
        log(f"\n  Daily P&L:   ${state['daily_pnl']:+.2f}")
        log(f"  Daily spent: ${state['daily_spent']:.2f} / ${DAILY_CAP:.2f}")
        log(f"  Open positions: {list(state['positions'].keys()) or 'None'}")

        # ── Sleep until next cycle ────────────────────────────────────────
        elapsed = time.time() - cycle_start
        sleep_for = max(0, CYCLE_SECS - elapsed)
        log(f"\n{Fore.CYAN}Cycle complete in {elapsed:.1f}s. Sleeping {sleep_for:.0f}s…\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log(f"\n{Fore.YELLOW}{BOT_NAME} stopped by user.")
