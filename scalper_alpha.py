"""
scalper_alpha.py  — v1.0
Instance: ALPHA  |  Adaptive High-Probability Scalper
Goal: Turn $5 max risk into $0.50+ daily profit (10% target)

Strategy: Multi-signal confluence engine
- Scans BTC, ETH, SOL, AVAX, LINK dynamically
- Scores each coin on 5 signals; only trades the highest-scorer above threshold
- Holds up to $5 at a time (not per coin — total risk cap)
- Sells in 3 tranches: 33% at +0.8%, 33% at +1.5%, 34% at +2.5% or trail
- Buys dips on same coin if still in budget and signal confirms
- Stops when $0.50 daily profit locked OR stop-loss burns the $5

Signals scored (0-5 total):
  1. RSI oversold (<38 = +2pts, <45 = +1pt)
  2. Price below lower Bollinger Band (+2pts) or below mid (-1pt if above upper)
  3. Positive 12-bar momentum < 0.5% (just turning, not already surged) (+1pt)
  4. Volume surge > 1.3x average (+1pt)
  5. Cross-instance consensus: other bots signaling buy (+1pt)

Minimum score to trade: 3/5
"""

import json, os, time, logging, math
from datetime import datetime, timedelta
from dotenv import load_dotenv
import robin_stocks.robinhood as rh
import schedule
from colorama import Fore, init

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
init(autoreset=True)

# ── Auth ──────────────────────────────────────────────────────────────────────
BEARER_TOKEN = os.environ.get("RH_BEARER_TOKEN", "")
if BEARER_TOKEN:
    rh.authentication.set_login_state(True)
    rh.authentication.SESSION.headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
else:
    rh.login(os.environ["RH_USER"], os.environ["RH_PASS"])

# ── Config ────────────────────────────────────────────────────────────────────
INSTANCE        = "ALPHA"
MAX_RISK        = 5.00          # max $ held at any moment
DAILY_PROFIT_GOAL = 0.50        # stop trading for the day once locked
STATE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_alpha.json")
CONSENSUS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_consensus.json")
LOG_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_alpha.txt")

# Candidate coins with per-coin risk params
UNIVERSE = {
    "BTC":  {"max_spread_pct": 0.022, "stop_pct": 0.018, "tranche": MAX_RISK},
    "ETH":  {"max_spread_pct": 0.022, "stop_pct": 0.018, "tranche": MAX_RISK},
    "SOL":  {"max_spread_pct": 0.022, "stop_pct": 0.022, "tranche": MAX_RISK},
    "AVAX": {"max_spread_pct": 0.025, "stop_pct": 0.025, "tranche": MAX_RISK},
    "LINK": {"max_spread_pct": 0.025, "stop_pct": 0.025, "tranche": MAX_RISK},
}

# Tranche sell targets (applied to avg buy price)
SELL_TIERS = [
    {"pct": 0.008, "fraction": 0.33},   # tier 1: sell 33% at +0.8%
    {"pct": 0.015, "fraction": 0.50},   # tier 2: sell 50% of remaining at +1.5%
    {"pct": 0.025, "fraction": 1.00},   # tier 3: sell rest at +2.5% or trail
]
TRAIL_PCT       = 0.006   # 0.6% trailing stop on final tranche
MIN_SIGNAL_SCORE = 3      # out of 5
COOLDOWN_HOURS  = 2

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(INSTANCE)
log.setLevel(logging.INFO)
fh  = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(fh)
sh  = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(sh)

# ── State ─────────────────────────────────────────────────────────────────────
def blank_state():
    return {
        "instance":      INSTANCE,
        "ts":            "",
        "daily_pnl":     0.0,          # realized pnl today
        "daily_fees":    0.0,
        "goal_reached":  False,
        "active_coin":   None,         # symbol currently held
        "position": {
            "sym":        None,
            "qty":        0.0,
            "avg":        0.0,
            "cost":       0.0,         # $ in
            "tier":       0,           # 0=full, 1=sold tier1, 2=sold tier2, 3=done
            "trail_high": 0.0,
            "entry_ts":   None,
        },
        "coins": {},                   # per-coin history: wins, losses, cooldown_until
    }

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return blank_state()

def save_state(st):
    st["ts"] = datetime.now().astimezone().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

def midnight_reset(st):
    now = datetime.now()
    ts  = st.get("ts", "")
    if ts and datetime.fromisoformat(ts).date() < now.date():
        log.info("── MIDNIGHT RESET ──")
        st["daily_pnl"]    = 0.0
        st["daily_fees"]   = 0.0
        st["goal_reached"] = False
        # reset per-coin cooldowns if expired
    return st

# ── Indicators ────────────────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    d  = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    ag = sum(max(x,0) for x in d[:period]) / period
    al = sum(abs(min(x,0)) for x in d[:period]) / period
    for x in d[period:]:
        ag = (ag*(period-1) + max(x,0))  / period
        al = (al*(period-1) + abs(min(x,0))) / period
    return 100 - 100/(1 + ag/al) if al else 100.0

def calc_bb(prices, period=20, std_mult=2.0):
    if len(prices) < period:
        return None, None, None
    w   = prices[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((p-mid)**2 for p in w) / period)
    return mid - std_mult*std, mid, mid + std_mult*std

def calc_mom(prices, bars=12):
    if len(prices) < bars + 1:
        return 0.0
    return (prices[-1] - prices[-bars]) / prices[-bars] * 100

def volume_ratio(sym, lookback=10):
    try:
        data = rh.crypto.get_crypto_historicals(sym, interval="5minute", span="hour")
        if not data or len(data) < lookback + 1:
            return 1.0
        vols = [float(b.get("volume", 0)) for b in data]
        avg  = sum(vols[-lookback-1:-1]) / lookback
        return vols[-1] / avg if avg else 1.0
    except Exception:
        return 1.0

def consensus_count(sym):
    try:
        if not os.path.exists(CONSENSUS_FILE):
            return 0
        with open(CONSENSUS_FILE) as f:
            data = json.load(f)
        count  = 0
        cutoff = datetime.now() - timedelta(minutes=10)
        for inst, v in data.get(sym, {}).items():
            if inst == INSTANCE:
                continue
            if v.get("signal") == "buy" and datetime.fromisoformat(v["ts"]) > cutoff:
                count += 1
        return count
    except Exception:
        return 0

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
        log.warning(f"consensus write: {e}")

# ── Signal scoring ─────────────────────────────────────────────────────────────
def score_coin(sym, price, closes, spread, cfg):
    """Returns (score 0-6, breakdown dict)"""
    score = 0
    info  = {}

    r = calc_rsi(closes)
    info["rsi"] = round(r, 1)
    if r < 38:
        score += 2
    elif r < 45:
        score += 1

    lb, mb, ub = calc_bb(closes)
    info["bb"] = [round(x, 4) if x else None for x in [lb, mb, ub]]
    if lb and price <= lb:
        score += 2
    elif ub and price >= ub:
        score -= 1  # overbought penalty

    mom = calc_mom(closes)
    info["mom"] = round(mom, 3)
    if 0 < mom < 0.5:
        score += 1   # just starting to turn up

    vr = volume_ratio(sym)
    info["vol_ratio"] = round(vr, 2)
    if vr >= 1.3:
        score += 1

    cc = consensus_count(sym)
    info["consensus"] = cc
    if cc >= 1:
        score += 1

    spread_ok = spread <= cfg["max_spread_pct"]
    info["spread_pct"] = round(spread * 100, 3)
    info["spread_ok"]  = spread_ok

    return score, info

# ── Order helpers ─────────────────────────────────────────────────────────────
def wait_for_fill(order_id, timeout=45, poll_interval=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            o         = rh.orders.get_crypto_order_info(order_id)
            state_val = o.get("state", "")
            log.info(f"  Order {order_id[:8]}… state={state_val}")
            if state_val in ("filled", "partially_filled"):
                fq = float(o.get("cumulative_quantity") or 0)
                fn = float(o.get("rounded_executed_notional") or 0)
                if fq > 0:
                    return fq, fn/fq if fq else None
            if state_val in ("cancelled", "failed", "rejected"):
                return None, None
        except Exception as e:
            log.warning(f"  poll error: {e}")
        time.sleep(poll_interval)
    log.warning(f"  Order {order_id[:8]}… timeout")
    return None, None

def place_buy(sym, amount):
    try:
        o = rh.orders.order_buy_crypto_by_price(sym, amount)
        log.info(Fore.CYAN + f"  BUY {sym} ${amount:.2f}  id={o.get('id','?')[:8]}…")
        return o
    except Exception as e:
        log.error(f"  BUY {sym} error: {e}")
        return None

def place_sell(sym, qty, reason=""):
    try:
        o = rh.orders.order_sell_crypto_by_quantity(sym, qty)
        log.info(Fore.YELLOW + f"  SELL {sym} {qty:.8f}  [{reason}]  id={o.get('id','?')[:8]}…")
        return o
    except Exception as e:
        log.error(f"  SELL {sym} error: {e}")
        return None

# ── Main loop ──────────────────────────────────────────────────────────────────
def run():
    st  = load_state()
    st  = midnight_reset(st)
    now = datetime.now()
    pos = st["position"]

    log.info("=" * 60)
    log.info(f"ALPHA cycle — daily_pnl=${st['daily_pnl']:.4f}  goal_reached={st['goal_reached']}")

    # ── Goal reached: skip trading ──
    if st["goal_reached"]:
        log.info(f"  ✅ Daily goal ${DAILY_PROFIT_GOAL:.2f} reached — resting")
        return

    # ── Get prices for all universe coins ──
    quotes = {}
    closes_map = {}
    for sym in UNIVERSE:
        try:
            q      = rh.crypto.get_crypto_quote(sym)
            bid    = float(q.get("bid_inclusive_of_sell_spread") or q.get("bid_price") or 0)
            ask    = float(q.get("ask_inclusive_of_buy_spread")  or q.get("ask_price") or 0)
            price  = float(q.get("mark_price") or (bid+ask)/2)
            spread = (ask - bid) / price if price else 1.0
            if price > 0:
                quotes[sym] = {"price": price, "spread": spread, "bid": bid, "ask": ask}
        except Exception as e:
            log.warning(f"  {sym} quote error: {e}")

        try:
            bars        = rh.crypto.get_crypto_historicals(sym, interval="5minute", span="day")
            closes_map[sym] = [float(b["close_price"]) for b in bars if b.get("close_price")]
        except Exception:
            closes_map[sym] = []

    # ─────────────── MANAGE EXISTING POSITION ───────────────
    active = pos.get("sym")
    if active and pos.get("qty", 0) > 0:
        q_data = quotes.get(active)
        if not q_data:
            log.warning(f"  Can't get {active} quote — holding")
        else:
            price   = q_data["price"]
            avg     = pos["avg"]
            qty     = pos["qty"]
            pnl_pct = (price - avg) / avg * 100 if avg else 0
            tier    = pos.get("tier", 0)
            log.info(f"  POSITION: {active} qty={qty:.8f} avg=${avg:.4f} price=${price:.4f} pnl={pnl_pct:.3f}% tier={tier}")

            # Stop loss
            cfg = UNIVERSE[active]
            if pnl_pct <= -cfg["stop_pct"] * 100:
                sell_qty_val     = qty
                pos["qty"]       = 0.0    # zero BEFORE sell
                pos["avg"]       = 0.0
                pos["cost"]      = 0.0
                o = place_sell(active, sell_qty_val, "stop-loss")
                if o:
                    realized = sell_qty_val * price - sell_qty_val * avg
                    st["daily_pnl"] += realized
                    cs = st["coins"].setdefault(active, {"wins": 0, "losses": 0})
                    cs["losses"] += 1
                    cs["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()
                    pos["sym"]    = None
                    pos["tier"]   = 0
                    st["active_coin"] = None
                    log.info(Fore.RED + f"  ✋ STOP-LOSS {active}  realized={realized:.4f}")

            # Tier sells
            elif tier == 0 and pnl_pct >= SELL_TIERS[0]["pct"] * 100:
                sell_amount  = round(qty * SELL_TIERS[0]["fraction"], 8)
                pos["qty"]  -= sell_amount   # deduct BEFORE sell
                o = place_sell(active, sell_amount, f"tier1 +{SELL_TIERS[0]['pct']*100:.1f}%")
                if o:
                    realized = sell_amount * (price - avg)
                    st["daily_pnl"] += realized
                    pos["tier"]      = 1
                    log.info(Fore.GREEN + f"  ✅ TIER 1 sell {sell_amount:.8f}  pnl+={realized:.4f}")
                    if st["daily_pnl"] >= DAILY_PROFIT_GOAL:
                        # Sell rest too and declare victory
                        if pos["qty"] > 0:
                            rem       = pos["qty"]
                            pos["qty"] = 0
                            place_sell(active, rem, "goal-reached-exit")
                            st["daily_pnl"] += rem * (price - avg)
                        st["goal_reached"] = True
                        pos["sym"] = None; pos["tier"] = 0; st["active_coin"] = None
                        log.info(Fore.GREEN + f"  🎯 DAILY GOAL REACHED! pnl=${st['daily_pnl']:.4f}")

            elif tier == 1 and pnl_pct >= SELL_TIERS[1]["pct"] * 100:
                sell_amount  = round(pos["qty"] * SELL_TIERS[1]["fraction"], 8)
                pos["qty"]  -= sell_amount
                o = place_sell(active, sell_amount, f"tier2 +{SELL_TIERS[1]['pct']*100:.1f}%")
                if o:
                    realized = sell_amount * (price - avg)
                    st["daily_pnl"] += realized
                    pos["tier"]      = 2
                    pos["trail_high"] = price
                    log.info(Fore.GREEN + f"  ✅ TIER 2 sell {sell_amount:.8f}  pnl+={realized:.4f}")

            elif tier == 2:
                # Trail the final tranche
                if price > pos.get("trail_high", 0):
                    pos["trail_high"] = price
                th = pos.get("trail_high", price)
                if price < th * (1 - TRAIL_PCT):
                    sell_amount  = pos["qty"]
                    pos["qty"]   = 0
                    pos["avg"]   = 0
                    o = place_sell(active, sell_amount, "trail-final")
                    if o:
                        realized = sell_amount * (price - avg)
                        st["daily_pnl"] += realized
                        cs = st["coins"].setdefault(active, {"wins": 0, "losses": 0})
                        cs["wins"] += 1
                        pos["sym"] = None; pos["tier"] = 0; st["active_coin"] = None
                        log.info(Fore.GREEN + f"  ✅ TRAIL EXIT {active}  total_pnl=${st['daily_pnl']:.4f}")
                        if st["daily_pnl"] >= DAILY_PROFIT_GOAL:
                            st["goal_reached"] = True
                            log.info(Fore.GREEN + f"  🎯 DAILY GOAL REACHED!")
                elif pnl_pct >= SELL_TIERS[2]["pct"] * 100:
                    sell_amount  = pos["qty"]
                    pos["qty"]   = 0
                    pos["avg"]   = 0
                    o = place_sell(active, sell_amount, f"tier3 +{SELL_TIERS[2]['pct']*100:.1f}%")
                    if o:
                        realized = sell_amount * (price - avg)
                        st["daily_pnl"] += realized
                        cs = st["coins"].setdefault(active, {"wins": 0, "losses": 0})
                        cs["wins"] += 1
                        pos["sym"] = None; pos["tier"] = 0; st["active_coin"] = None
                        log.info(Fore.GREEN + f"  ✅ TIER 3 EXIT  pnl=${st['daily_pnl']:.4f}")
                        if st["daily_pnl"] >= DAILY_PROFIT_GOAL:
                            st["goal_reached"] = True

        save_state(st)
        return   # manage position first; re-evaluate next cycle

    # ─────────────── SCAN FOR ENTRY ───────────────
    # No active position — find best opportunity
    log.info("  No position — scanning universe...")

    best_sym, best_score, best_info = None, MIN_SIGNAL_SCORE - 1, {}

    for sym, cfg in UNIVERSE.items():
        # Cooldown check
        cs = st["coins"].get(sym, {})
        cd = cs.get("cooldown_until")
        if cd and datetime.fromisoformat(cd) > now:
            log.info(f"  {sym} in cooldown — skip")
            continue

        q_data  = quotes.get(sym)
        closes  = closes_map.get(sym, [])
        if not q_data or len(closes) < 22:
            continue

        price  = q_data["price"]
        spread = q_data["spread"]
        score, info = score_coin(sym, price, closes, spread, cfg)

        log.info(f"  {sym} score={score}/6  RSI={info.get('rsi')}  "
                 f"BB={info.get('bb')}  mom={info.get('mom')}%  "
                 f"vol={info.get('vol_ratio')}x  spread={info.get('spread_pct')}%  "
                 f"consensus={info.get('consensus')}")

        update_consensus(sym, "buy" if score >= MIN_SIGNAL_SCORE else "hold")

        if score > best_score and info.get("spread_ok", False):
            best_score, best_sym, best_info = score, sym, info

    if best_sym:
        price  = quotes[best_sym]["price"]
        amount = MAX_RISK
        log.info(Fore.CYAN + f"  BEST ENTRY: {best_sym} score={best_score}/6 @ ${price:.4f}")

        o = place_buy(best_sym, amount)
        if o and o.get("id"):
            fq, fp = wait_for_fill(o["id"])
            if fq and fq > 0:
                pos["sym"]       = best_sym
                pos["qty"]       = fq
                pos["avg"]       = fp
                pos["cost"]      = amount
                pos["tier"]      = 0
                pos["trail_high"] = fp
                pos["entry_ts"]  = now.isoformat()
                st["active_coin"] = best_sym
                log.info(Fore.GREEN + f"  ✅ ENTERED {best_sym}: {fq:.8f} @ ${fp:.4f}")
            else:
                log.warning(f"  {best_sym} buy did NOT fill")
        else:
            log.warning(f"  {best_sym} buy order had no id")
    else:
        log.info("  No qualifying entry found this cycle")

    save_state(st)
    log.info(f"  daily_pnl=${st['daily_pnl']:.4f}")

# ── Scheduler ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("ALPHA bot v1.0 starting — goal: $0.50/day on $5 max risk")
    run()
    schedule.every(5).minutes.do(run)
    while True:
        schedule.run_pending()
        time.sleep(10)

