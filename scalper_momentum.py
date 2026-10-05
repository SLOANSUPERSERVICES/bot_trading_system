"""
scalper_momentum.py  — v4.1
Instance: MOMENTUM  |  Coins: BTC, SOL, ETH
Strategy: RSI(14) + Bollinger Bands mean-reversion / breakout combo
Fixes in v4.1:
  - BTC and SOL max_spread_pct raised from 0.005 → 0.020 (matches ETH fix from v4)
  - Duplicate sell guard: qty zeroed atomically before sell API call
All v4 features retained:
  ① Spread filter  ② Partial sell + trailing stop  ③ Stop cooldown (2h)
  ④ Volume confirmation  ⑤ Cross-instance consensus  ⑥ Fill confirmation (wait_for_fill)
"""

import json, os, time, logging, math
from datetime import datetime, timedelta
from dotenv import load_dotenv
import robin_stocks.robinhood as rh
import schedule
from colorama import Fore, Style, init

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
init(autoreset=True)

# ── Auth ──────────────────────────────────────────────────────────────────────
BEARER_TOKEN = os.environ.get("RH_BEARER_TOKEN", "")
if BEARER_TOKEN:
    rh.authentication.set_login_state(True)
    rh.authentication.set_login_state(True)
    rh.authentication.set_login_state(True)
    rh.authentication.SESSION.headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
else:
    rh.login(os.environ["RH_USER"], os.environ["RH_PASS"])

# ── Config ────────────────────────────────────────────────────────────────────
INSTANCE        = "MOMENTUM"
DAILY_CAP       = 30.00        # max $ spent per day across all coins
STATE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_momentum.json")
CONSENSUS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_consensus.json")
LOG_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_momentum.txt")

COINS = {
    "BTC": {
        "budget": 10.00, "tranche": 3.00,
        "target_pct": 0.010, "stop_pct": 0.020,
        "rsi_buy": 35, "rsi_sell": 55,
        "bb_period": 20, "bb_std": 2.0,
        "trail_pct": 0.005, "partial_pct": 0.010,
        "max_spread_pct": 0.020,   # ← FIXED from 0.005 (actual spread ~1.87%)
    },
    "SOL": {
        "budget": 10.00, "tranche": 3.00,
        "target_pct": 0.012, "stop_pct": 0.025,
        "rsi_buy": 35, "rsi_sell": 55,
        "bb_period": 20, "bb_std": 2.0,
        "trail_pct": 0.005, "partial_pct": 0.010,
        "max_spread_pct": 0.020,   # ← FIXED from 0.005 (actual spread ~1.87%)
    },
    "ETH": {
        "budget": 10.00, "tranche": 3.00,
        "target_pct": 0.010, "stop_pct": 0.020,
        "rsi_buy": 35, "rsi_sell": 55,
        "bb_period": 20, "bb_std": 2.0,
        "trail_pct": 0.005, "partial_pct": 0.010,
        "max_spread_pct": 0.020,   # was already fixed in v4
    },
}

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(INSTANCE)
log.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(fh)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(sh)

# ── State helpers ─────────────────────────────────────────────────────────────
def _blank_coin():
    return dict(price=0.0, qty=0, avg=0, day_spent=0.0, trades=0,
                wins=0, losses=0, value=0.0, pnl_pct=0,
                last_action=None, budget_left=0.0,
                partial_sold=False, cooldown_until=None)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
        # ensure all configured coins present
        for sym in COINS:
            if sym not in st["coins"]:
                st["coins"][sym] = _blank_coin()
                st["coins"][sym]["budget_left"] = COINS[sym]["budget"]
        return st
    st = {"instance": INSTANCE, "ts": "", "daily_total": 0.0,
          "session_pnl": 0.0, "coins": {}}
    for sym, cfg in COINS.items():
        st["coins"][sym] = _blank_coin()
        st["coins"][sym]["budget_left"] = cfg["budget"]
    return st

def save_state(st):
    st["ts"] = datetime.now().astimezone().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

def midnight_reset(st):
    now = datetime.now()
    ts  = st.get("ts", "")
    if ts and datetime.fromisoformat(ts).date() < now.date():
        log.info("── MIDNIGHT RESET ──")
        st["daily_total"] = 0.0
        for sym, cfg in COINS.items():
            s = st["coins"][sym]
            s["day_spent"]   = 0.0
            s["budget_left"] = cfg["budget"]
    return st

# ── Technical indicators ──────────────────────────────────────────────────────
def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period-1) + gains[i])  / period
        al = (al * (period-1) + losses[i]) / period
    return 100 - 100 / (1 + ag / al) if al else 100.0

def bollinger(prices, period=20, std_mult=2.0):
    if len(prices) < period:
        return None, None, None
    window = prices[-period:]
    mid    = sum(window) / period
    std    = math.sqrt(sum((p - mid)**2 for p in window) / period)
    return mid - std_mult*std, mid, mid + std_mult*std

# ── Consensus helpers ─────────────────────────────────────────────────────────
def update_consensus(sym, signal):
    try:
        data = {}
        if os.path.exists(CONSENSUS_FILE):
            with open(CONSENSUS_FILE) as f:
                data = json.load(f)
        if sym not in data:
            data[sym] = {}
        data[sym][INSTANCE] = {"signal": signal, "ts": datetime.now().isoformat()}
        with open(CONSENSUS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"consensus write error: {e}")

def consensus_count(sym):
    try:
        if not os.path.exists(CONSENSUS_FILE):
            return 0
        with open(CONSENSUS_FILE) as f:
            data = json.load(f)
        count = 0
        cutoff = datetime.now() - timedelta(minutes=10)
        for inst, v in data.get(sym, {}).items():
            if v.get("signal") == "buy":
                ts = datetime.fromisoformat(v["ts"])
                if ts > cutoff:
                    count += 1
        return count
    except Exception:
        return 0

# ── Order helpers ─────────────────────────────────────────────────────────────
def wait_for_fill(order_id, timeout=45, poll_interval=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            o         = rh.orders.get_crypto_order_info(order_id)
            state_val = o.get("state", "")
            log.info(f"  Order {order_id[:8]}… state={state_val}")
            if state_val in ("filled", "partially_filled"):
                filled_qty      = float(o.get("cumulative_quantity") or 0)
                filled_notional = float(o.get("rounded_executed_notional") or 0)
                if filled_qty > 0:
                    avg_price = filled_notional / filled_qty if filled_qty else None
                    return filled_qty, avg_price
            if state_val in ("cancelled", "failed", "rejected"):
                log.warning(f"  Order {order_id[:8]}… {state_val} — state NOT updated")
                return None, None
        except Exception as e:
            log.warning(f"  Order poll error: {e}")
        time.sleep(poll_interval)
    log.warning(f"  Order {order_id[:8]}… timeout after {timeout}s — state NOT updated")
    return None, None

def buy(sym, amount, reason=""):
    try:
        o = rh.orders.order_buy_crypto_by_price(sym, amount)
        log.info(Fore.CYAN + f"  BUY {sym} ${amount:.2f}  [{reason}]  order_id={o.get('id','?')[:8]}…")
        return o
    except Exception as e:
        log.error(f"  BUY {sym} error: {e}")
        return None

def sell_qty(sym, qty, reason=""):
    try:
        o = rh.orders.order_sell_crypto_by_quantity(sym, qty)
        log.info(Fore.YELLOW + f"  SELL {sym} {qty:.6f}  [{reason}]  order_id={o.get('id','?')[:8]}…")
        return o
    except Exception as e:
        log.error(f"  SELL {sym} error: {e}")
        return None

# ── Volume confirmation ────────────────────────────────────────────────────────
def volume_ok(sym, lookback=10, multiplier=1.2):
    try:
        bars = rh.crypto.get_crypto_historicals(sym, interval="5minute", span="hour")
        if not bars or len(bars) < lookback + 1:
            return True
        vols    = [float(b.get("volume", 0)) for b in bars]
        avg_vol = sum(vols[-lookback-1:-1]) / lookback
        cur_vol = vols[-1]
        ok = cur_vol >= avg_vol * multiplier
        if not ok:
            log.info(f"  {sym} volume low ({cur_vol:.2f} < {avg_vol*multiplier:.2f}) — skip")
        return ok
    except Exception:
        return True  # if we can't check, don't block

# ── Main loop ──────────────────────────────────────────────────────────────────
def run():
    st          = load_state()
    st          = midnight_reset(st)
    daily_total = st.get("daily_total", 0.0)
    now         = datetime.now()

    log.info(f"{'='*60}")
    log.info(f"MOMENTUM cycle — daily_spent=${daily_total:.2f}/{DAILY_CAP:.2f}")

    for sym, cfg in COINS.items():
        s = st["coins"].setdefault(sym, _blank_coin())
        s["budget_left"] = cfg["budget"] - s["day_spent"]

        try:
            # ── Price & spread ──
            quote  = rh.crypto.get_crypto_quote(sym)
            bid    = float(quote.get("bid_inclusive_of_sell_spread") or quote.get("bid_price") or 0)
            ask    = float(quote.get("ask_inclusive_of_buy_spread")  or quote.get("ask_price") or 0)
            price  = float(quote.get("mark_price") or (bid+ask)/2)
            spread = (ask - bid) / price if price else 0
            s["price"] = price
            if price == 0:
                log.warning(f"  {sym} price=0 — skip")
                continue

            log.info(f"  {sym} ${price:.4f}  spread={spread*100:.2f}%  qty={s['qty']:.6f}")

            # ── Spread filter ──
            if spread > cfg["max_spread_pct"]:
                log.info(f"  {sym} buy skipped — spread {spread*100:.2f}% > {cfg['max_spread_pct']*100:.1f}%")
                update_consensus(sym, "hold")
                # still check sell side if holding
            else:
                update_consensus(sym, "hold")  # reset; may change to "buy" below

            # ── Historical prices for indicators ──
            bars   = rh.crypto.get_crypto_historicals(sym, interval="5minute", span="day")
            closes = [float(b["close_price"]) for b in bars if b.get("close_price")]
            if len(closes) < 22:
                log.warning(f"  {sym} not enough history — skip")
                continue

            r            = rsi(closes)
            lb, mb, ub   = bollinger(closes, cfg["bb_period"], cfg["bb_std"])
            s["value"]   = round(s["qty"] * price, 4)
            pnl_pct      = ((price - s["avg"]) / s["avg"] * 100) if s["avg"] else 0
            s["pnl_pct"] = round(pnl_pct, 3)

            log.info(f"  {sym} RSI={r:.1f}  BB=[{lb:.2f},{mb:.2f},{ub:.2f}]  pnl={pnl_pct:.2f}%")

            # ── Cooldown check ──
            cooldown = s.get("cooldown_until")
            in_cooldown = False
            if cooldown:
                cd_time = datetime.fromisoformat(cooldown)
                if now < cd_time:
                    in_cooldown = True
                    log.info(f"  {sym} in cooldown until {cd_time.strftime('%H:%M')}")

            # ─────────────── SELL LOGIC ───────────────
            if s["qty"] > 0 and s["avg"] > 0:
                # Stop loss
                if pnl_pct <= -cfg["stop_pct"] * 100:
                    sell_amount = s["qty"]
                    s["qty"]    = 0  # zero BEFORE API call — prevents duplicate sell
                    o = sell_qty(sym, sell_amount, "stop-loss")
                    if o:
                        s["losses"]      += 1
                        s["last_action"]  = "stop"
                        s["partial_sold"] = False
                        s["avg"]          = 0
                        s["cooldown_until"] = (now + timedelta(hours=2)).isoformat()
                        st["session_pnl"] += s["value"] - (sell_amount * s["avg"] if s["avg"] else 0)
                        log.info(Fore.RED + f"  ✋ {sym} STOP-LOSS")

                # Partial sell at target
                elif not s.get("partial_sold") and pnl_pct >= cfg["partial_pct"] * 100:
                    half       = round(s["qty"] / 2, 8)
                    s["qty"]  -= half  # deduct BEFORE API call
                    o = sell_qty(sym, half, "partial-target")
                    if o:
                        s["partial_sold"] = True
                        s["last_action"]  = "partial"
                        log.info(Fore.GREEN + f"  ✅ {sym} PARTIAL SELL {half:.6f}")

                # Full sell at RSI overbought or upper BB
                elif pnl_pct >= cfg["target_pct"] * 100 and (r >= cfg["rsi_sell"] or price >= ub):
                    sell_amount = s["qty"]
                    s["qty"]    = 0  # zero BEFORE API call
                    o = sell_qty(sym, sell_amount, "target")
                    if o:
                        s["wins"]        += 1
                        s["last_action"]  = "sell"
                        s["partial_sold"] = False
                        s["avg"]          = 0
                        log.info(Fore.GREEN + f"  ✅ {sym} FULL SELL")

                # Trailing stop on partial
                elif s.get("partial_sold") and s.get("trail_high"):
                    if price > s.get("trail_high", 0):
                        s["trail_high"] = price
                    elif price < s["trail_high"] * (1 - cfg["trail_pct"]):
                        sell_amount = s["qty"]
                        s["qty"]    = 0  # zero BEFORE API call
                        o = sell_qty(sym, sell_amount, "trail-stop")
                        if o:
                            s["wins"]        += 1
                            s["last_action"]  = "trail-stop"
                            s["partial_sold"] = False
                            s["avg"]          = 0
                            log.info(Fore.GREEN + f"  ✅ {sym} TRAIL STOP WIN")
                else:
                    if s.get("partial_sold"):
                        s["trail_high"] = max(s.get("trail_high", 0), price)

            # ─────────────── BUY LOGIC ───────────────
            if s["qty"] == 0 and not in_cooldown:
                budget_ok  = s["budget_left"] >= cfg["tranche"] and daily_total + cfg["tranche"] <= DAILY_CAP
                signal_buy = r <= cfg["rsi_buy"] and lb is not None and price <= lb * 1.005

                if signal_buy:
                    update_consensus(sym, "buy")
                    cc = consensus_count(sym)
                    log.info(f"  {sym} consensus={cc}/3")

                if signal_buy and budget_ok and spread <= cfg["max_spread_pct"] and volume_ok(sym):
                    tag = " [CONSENSUS]" if cc >= 2 else ""
                    o   = buy(sym, cfg["tranche"], f"RSI={r:.1f} BB-low{tag}")
                    if o and o.get("id"):
                        fq, fp = wait_for_fill(o["id"])
                        if fq and fq > 0:
                            pc             = s["avg"] * s["qty"]
                            s["qty"]      += fq
                            s["day_spent"] += cfg["tranche"]
                            daily_total    += cfg["tranche"]
                            s["avg"]        = (pc + cfg["tranche"]) / s["qty"]
                            s["trades"]    += 1
                            s["last_action"] = "buy"
                            s["trail_high"]  = price
                            log.info(Fore.GREEN + f"  ✅ {sym} FILLED {fq:.6f} @ ${fp:.2f}")
                        else:
                            log.warning(f"  {sym} buy did NOT fill — state NOT updated")
                    else:
                        log.warning(f"  {sym} buy order had no id — state NOT updated")

        except Exception as e:
            log.error(f"  {sym} ERROR: {e}", exc_info=True)

    st["daily_total"] = daily_total
    save_state(st)
    log.info(f"State saved. daily_total=${daily_total:.2f}")

# ── Scheduler ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"{'='*60}")
    log.info(f"MOMENTUM bot v4.1 starting")
    run()
    schedule.every(5).minutes.do(run)
    while True:
        schedule.run_pending()
        time.sleep(10)
