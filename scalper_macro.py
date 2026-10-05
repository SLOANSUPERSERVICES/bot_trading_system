"""
SCALPER INSTANCE 3 — Mean Reversion (Fast Scalp)  [v3]
Coins:    BTC, ETH  (large cap, macro-driven)
Strategy: Buy when price touches lower Bollinger Band (15-period, 1.8 std)
          Sell when price returns to mid-band (mean reversion complete)
          Very tight stop: -1.5% — cut losers fast, small wins add up
Upgrades: ④ Volume confirmation (ratio >= 1.2 required; skips if unavailable)
          ⑤ Cross-instance consensus (logs CONSENSUS BUY when >= 2 instances agree)
Budget:   $5/coin/day | $10/day total | $1 tranches
Log:      log_macro.txt | state_macro.json
Consensus: signal_consensus.json
"""

import os, time, math, json, logging, schedule, pytz
from datetime import datetime
from dotenv import load_dotenv
from colorama import Fore, init

INSTANCE_NAME  = "MACRO"
LOG_FILE       = "log_macro.txt"
STATE_FILE     = "state_macro.json"
CONSENSUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_consensus.json")

try:
    import robin_stocks.robinhood as rh
    ROBIN_STOCKS_AVAILABLE = True
except ImportError:
    ROBIN_STOCKS_AVAILABLE = False

init(autoreset=True)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
ET = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(INSTANCE_NAME)

ACCOUNT_NUMBER = "544378490"
DAILY_CAP      = 30.00   # tighter cap — faster cycling strategy

COINS = {
    "BTC": {"budget": 10.00, "tranche": 3.00, "stop_pct": 0.015,
            "bb_period": 15, "bb_std": 1.8},
    "ETH": {"budget": 10.00, "tranche": 3.00, "stop_pct": 0.015,
            "bb_period": 15, "bb_std": 1.8},
}

state = {c: {"spent": 0.0, "qty": 0.0, "avg": 0.0, "prices": [],
             "day_spent": 0.0, "trades": 0, "wins": 0, "losses": 0,
             "last_action": None, "last_price": 0.0}
         for c in COINS}
daily_total = 0.0
session_pnl = 0.0


def save_state():
    out = {"instance": INSTANCE_NAME, "ts": now_et().isoformat(),
           "daily_total": daily_total, "session_pnl": session_pnl, "coins": {}}
    for sym, s in state.items():
        cfg = COINS[sym]; price = s["last_price"]
        value = price * s["qty"]
        pnl_pct = (price - s["avg"]) / s["avg"] * 100 if s["avg"] > 0 else 0
        out["coins"][sym] = {
            "price": price, "qty": s["qty"], "avg": s["avg"],
            "day_spent": s["day_spent"], "trades": s["trades"],
            "wins": s["wins"], "losses": s["losses"],
            "value": round(value, 4), "pnl_pct": round(pnl_pct, 2),
            "last_action": s["last_action"],
            "budget_left": round(cfg["budget"] - s["day_spent"], 2),
        }
    with open(STATE_FILE, "w") as f:
        json.dump(out, f, indent=2)


def now_et():
    return datetime.now(ET)


def calc_bb(prices, period=15, nstd=1.8):
    if len(prices) < period: return None, None, None
    w = prices[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((p-mid)**2 for p in w) / period)
    return mid - nstd*std, mid, mid + nstd*std


def get_price(sym):
    try:
        q = rh.crypto.get_crypto_quote(sym)
        if q and q.get("mark_price"): return float(q["mark_price"])
        if q and q.get("ask_price"):  return float(q["ask_price"])
    except Exception as e:
        log.error(f"Price {sym}: {e}")
    return None


# ④ Volume confirmation
def get_volume_ratio(sym):
    """Returns current_candle_volume / avg_of_last_10_candles, or None if unavailable."""
    try:
        candles = rh.crypto.get_crypto_historicals(sym, interval="5minute", span="hour")
        if not candles or len(candles) < 2:
            return None
        volumes = []
        for c in candles:
            v = (c.get("volume") or c.get("volume_traded") or c.get("close_volume") or None)
            if v is not None:
                try:
                    volumes.append(float(v))
                except (TypeError, ValueError):
                    pass
        if len(volumes) < 2:
            return None
        current_vol = volumes[-1]
        prior_vols  = volumes[-11:-1]
        if not prior_vols:
            return None
        avg_vol = sum(prior_vols) / len(prior_vols)
        if avg_vol == 0:
            return None
        return round(current_vol / avg_vol, 2)
    except Exception as e:
        log.warning(f"Volume ratio {sym}: {e}")
        return None


# ⑤ Cross-instance consensus helpers
def write_signal(sym, signal_type):
    """Write this instance's signal for sym to the shared consensus file."""
    try:
        data = {}
        if os.path.exists(CONSENSUS_FILE):
            try:
                with open(CONSENSUS_FILE, "r") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}
        if INSTANCE_NAME not in data:
            data[INSTANCE_NAME] = {}
        data[INSTANCE_NAME][sym] = signal_type
        with open(CONSENSUS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"write_signal {sym} {signal_type}: {e}")


def read_consensus(sym):
    """Count how many instances have 'buy' signal for sym. Returns int 0-3."""
    try:
        if not os.path.exists(CONSENSUS_FILE):
            return 0
        with open(CONSENSUS_FILE, "r") as f:
            data = json.load(f)
        count = sum(
            1 for instance_signals in data.values()
            if isinstance(instance_signals, dict) and instance_signals.get(sym) == "buy"
        )
        return count
    except Exception as e:
        log.warning(f"read_consensus {sym}: {e}")
        return 0


def buy(sym, amt, reason):
    try:
        log.info(Fore.GREEN + f"BUY {sym} ${amt} — {reason}")
        o = rh.orders.order_buy_crypto_by_price(sym, amt)
        return o
    except Exception as e:
        log.error(f"Buy failed {sym}: {e}"); return None


def sell(sym, qty, reason):
    try:
        log.warning(Fore.RED + f"SELL {sym} {round(qty,8)} — {reason}")
        o = rh.orders.order_sell_crypto_by_quantity(sym, qty)
        return o
    except Exception as e:
        log.error(f"Sell failed {sym}: {e}"); return None


def monitor():
    global daily_total, session_pnl
    print(f"\n{Fore.YELLOW}─── {INSTANCE_NAME}  {now_et().strftime('%H:%M:%S ET')}  daily=${round(daily_total,2)}/${DAILY_CAP} ───")

    for sym, cfg in COINS.items():
        s = state[sym]
        price = get_price(sym)
        if not price: continue
        s["last_price"] = price
        s["prices"].append(price)
        if len(s["prices"]) > 20: s["prices"].pop(0)

        lb, mb, ub = calc_bb(s["prices"], cfg["bb_period"], cfg["bb_std"])
        vol = get_volume_ratio(sym)

        bb_s = f"L${round(lb,2)} M${round(mb,2)} U${round(ub,2)}" if lb else "warm"
        vol_s = f"{vol:.2f}x" if vol is not None else "—"
        rem  = cfg["budget"] - s["day_spent"]

        pnl_pct = (price - s["avg"]) / s["avg"] * 100 if s["avg"] > 0 else 0
        col = Fore.GREEN if pnl_pct >= 0 else Fore.RED
        print(f"  {col}{sym}  ${price:,.2f}  BB=[{bb_s}]  vol={vol_s}"
              f"  qty={round(s['qty'],6)}  {'%+.2f%%' % pnl_pct if s['qty'] else 'flat'}  left=${round(rem,2)}")

        # SELL: mean reversion — price returned to mid band
        if s["qty"] > 0 and s["avg"] > 0:
            write_signal(sym, "sell")
            stop = s["avg"] * (1 - cfg["stop_pct"])
            if price <= stop:
                o = sell(sym, s["qty"], f"STOP ${round(stop,2)}")
                if o:
                    pnl=(price-s["avg"])*s["qty"]; session_pnl+=pnl
                    s["losses"]+=1; s["trades"]+=1
                    s["qty"]=0; s["avg"]=0; s["day_spent"]=cfg["budget"]
                    s["last_action"]="stop"
            elif mb and price >= mb:
                o = sell(sym, s["qty"], f"MID BAND ${round(mb,2)} (mean reversion)")
                if o:
                    pnl=(price-s["avg"])*s["qty"]; session_pnl+=pnl
                    (s.__setitem__("wins",s["wins"]+1) if pnl>0 else s.__setitem__("losses",s["losses"]+1))
                    s["trades"]+=1; s["qty"]=0; s["avg"]=0; s["day_spent"]=0
                    s["last_action"]="sell"

        # BUY: price at or below lower band
        elif rem >= cfg["tranche"] and daily_total < DAILY_CAP:
            if lb is None:
                if s["day_spent"] == 0:
                    write_signal(sym, "buy")
                    o = buy(sym, cfg["tranche"], "Seed entry")
                    if o:
                        s["qty"]+=cfg["tranche"]/price; s["avg"]=price
                        s["day_spent"]+=cfg["tranche"]; daily_total+=cfg["tranche"]
                        s["last_action"]="buy"
                else:
                    write_signal(sym, "flat")
            elif price <= lb:
                # ④ Volume confirmation
                if vol is not None and vol < 1.2:
                    log.info(f"  SKIP BUY {sym} — vol ratio {vol:.2f} < 1.2 (low volume)")
                    write_signal(sym, "flat")
                    continue

                # ⑤ Cross-instance consensus
                write_signal(sym, "buy")
                consensus = read_consensus(sym)
                if consensus >= 2:
                    reason = f"LOWER BB ${round(lb,2)} 🤝CONSENSUS({consensus}/3)"
                    log.info(Fore.CYAN + f"  🤝 CONSENSUS BUY {sym} ({consensus}/3)")
                else:
                    reason = f"LOWER BB ${round(lb,2)}"

                o = buy(sym, cfg["tranche"], reason)
                if o:
                    pc=s["avg"]*s["qty"]; s["qty"]+=cfg["tranche"]/price
                    s["day_spent"]+=cfg["tranche"]; daily_total+=cfg["tranche"]
                    s["avg"]=(pc+cfg["tranche"])/s["qty"]; s["trades"]+=1
                    s["last_action"]="buy"
            else:
                write_signal(sym, "flat")
        else:
            write_signal(sym, "flat")

    save_state()


def reset_daily():
    global daily_total
    log.info(f"DAILY RESET — spent ${round(daily_total,2)}")
    daily_total = 0.0
    for s in state.values():
        s["day_spent"]=0.0; s["trades"]=0; s["spent"]=0.0


def login():
    if not ROBIN_STOCKS_AVAILABLE: return False
    token = os.getenv("RH_BEARER_TOKEN")
    if not token: return False
    try:
        rh.authentication.set_login_state(True)
        rh.authentication.SESSION.headers["Authorization"] = f"Bearer {token}"
        p = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER)
        if p: log.info(Fore.GREEN + "Authenticated (8490)"); return True
    except Exception as e:
        log.error(f"Auth: {e}")
    return False


def main():
    print(Fore.YELLOW + f"""
+----------------------------------------------------------+
|  INSTANCE 3: {INSTANCE_NAME:<10}  Mean Reversion (Fast) v3 |
|  Coins: BTC · ETH   Budget: $5/coin  $10/day            |
|  Sell target: mid-band  |  Stop: -1.5%                  |
|  ④ Volume confirmation (ratio>=1.2)                      |
|  ⑤ Cross-instance consensus (signal_consensus.json)      |
+----------------------------------------------------------+
""")
    if not login(): return
    schedule.every(5).minutes.do(monitor)
    schedule.every().day.at("00:01").do(reset_daily)
    monitor()
    log.info(f"Running — {now_et().strftime('%H:%M ET')} | Ctrl+C to stop")
    try:
        while True: schedule.run_pending(); time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped.")

if __name__ == "__main__":
    main()



