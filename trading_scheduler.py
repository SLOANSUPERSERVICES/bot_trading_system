"""
AGENTIC TRADING SCHEDULER
Account: Robinhood 8490
Purpose: Stop-loss monitoring, pre-market scanning, scheduled execution,
         24/7 crypto scalping (BTC + SOL, RSI-based, $1 tranches)
Run:     python trading_scheduler.py

SETUP (one-time):
  pip install robin_stocks requests schedule python-dotenv colorama pytz

Create a .env file in the same folder with:
  RH_ACCESS_TOKEN=your_robinhood_access_token
  ANTHROPIC_API_KEY=your_key
"""

import os
import time
import logging
import schedule
import requests
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from colorama import Fore, Style, init

try:
    import robin_stocks.robinhood as rh
    ROBIN_STOCKS_AVAILABLE = True
except ImportError:
    ROBIN_STOCKS_AVAILABLE = False
    print("robin_stocks not installed. Run: pip install robin_stocks")

init(autoreset=True)
load_dotenv()
ET = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("trading_log.txt"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("scheduler")

ACCOUNT_NUMBER = "544378490"

POSITIONS = {
    "PLTR": {"entry": 145.00, "stop_pct": 0.00, "target_pct": 0.10, "bucket": "swing"},
    "CBRS": {"entry": 236.32, "stop_pct": 0.05, "target_pct": 0.07, "bucket": "asymmetric"},
    "AMAT": {"entry": 558.26, "stop_pct": 0.01, "target_pct": 0.02, "bucket": "tactical"},
    "META": {"entry": 590.09, "stop_pct": 0.01, "target_pct": 0.02, "bucket": "macro_news"},
    "SQQQ": {"entry": 36.52,  "stop_pct": 0.05, "target_pct": 0.03, "bucket": "pairs_hedge"},
    "VICI": {"entry": 28.18,  "stop_pct": 0.05, "target_pct": 0.06, "bucket": "dividend"},
}

MAX_DAILY_LOSS_PCT = 0.03
MAX_POSITIONS      = 10
SCAN_INTERVAL_MINS = 5
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────
# CRYPTO SCALPER CONFIG  (24/7, RSI-based DCA)
# ─────────────────────────────────────────────
CRYPTO_COINS = {
    "BTC": {
        "budget":        5.00,   # total dollars to deploy
        "tranche":       1.00,   # dollars per buy
        "target_pct":    0.008,  # sell when up 0.8% from avg entry
        "rsi_buy":       45,     # buy tranche when RSI <= this
        "rsi_sell":      60,     # sell when RSI >= this (overrides target)
        "stop_pct":      0.02,   # hard stop: sell all if down 2% from avg
    },
    "SOL": {
        "budget":        5.00,
        "tranche":       1.00,
        "target_pct":    0.010,  # SOL is more volatile, slightly wider target
        "rsi_buy":       45,
        "rsi_sell":      62,
        "stop_pct":      0.025,
    },
}

# Runtime state per coin (resets on restart)
crypto_state = {
    coin: {
        "spent":       0.0,   # total dollars deployed so far
        "quantity":    0.0,   # total units held
        "avg_entry":   0.0,   # dollar-weighted average buy price
        "prices":      [],    # recent price history for RSI (last 15 ticks)
        "last_action": None,  # "buy" or "sell"
    }
    for coin in CRYPTO_COINS
}


# ─────────────────────────────────────────────
# RSI HELPER
# ─────────────────────────────────────────────
def calc_rsi(prices, period=14):
    """Simple RSI from a list of closing prices. Returns None if not enough data."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    # Use only the last `period` values
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


# ─────────────────────────────────────────────
# CRYPTO PRICE FETCH  (via Robinhood API)
# ─────────────────────────────────────────────
def get_crypto_price(symbol):
    """Fetch current mark price for a crypto symbol via Robinhood."""
    try:
        # robin_stocks crypto quote
        quote = rh.crypto.get_crypto_quote(symbol)
        if quote and quote.get("mark_price"):
            return float(quote["mark_price"])
        # fallback to ask_price
        if quote and quote.get("ask_price"):
            return float(quote["ask_price"])
    except Exception as e:
        log.error("Crypto price error " + symbol + ": " + str(e))
    return None


def buy_crypto(symbol, dollar_amount, reason):
    """Buy crypto fractionally by dollar amount."""
    try:
        log.info(Fore.GREEN + "CRYPTO BUY " + symbol + " $" + str(dollar_amount) + " - " + reason)
        order = rh.orders.order_buy_crypto_by_price(symbol, dollar_amount)
        log.info("Crypto buy placed: " + str(order))
        return order
    except Exception as e:
        log.error("Crypto buy failed " + symbol + ": " + str(e))
        return None


def sell_crypto(symbol, quantity, reason):
    """Sell crypto by quantity."""
    try:
        log.warning(Fore.RED + "CRYPTO SELL " + symbol + " " + str(round(quantity, 8)) + " - " + reason)
        order = rh.orders.order_sell_crypto_by_quantity(symbol, quantity)
        log.info("Crypto sell placed: " + str(order))
        return order
    except Exception as e:
        log.error("Crypto sell failed " + symbol + ": " + str(e))
        return None


# ─────────────────────────────────────────────
# CRYPTO SCALPER MONITOR  (runs every 5 min, 24/7)
# ─────────────────────────────────────────────
def monitor_crypto():
    """RSI-based DCA scalper. Buys on dips, sells on recovery. $1 tranches, $5 max."""
    print("\n" + Fore.MAGENTA + "--- CRYPTO MONITOR " + now_et().strftime("%H:%M:%S ET") + " ---")

    for symbol, cfg in CRYPTO_COINS.items():
        state = crypto_state[symbol]
        price = get_crypto_price(symbol)

        if not price:
            log.warning("No crypto price for " + symbol)
            continue

        # Build price history for RSI
        state["prices"].append(price)
        if len(state["prices"]) > 20:
            state["prices"].pop(0)

        rsi = calc_rsi(state["prices"])
        rsi_str = str(rsi) if rsi is not None else "warm-up"
        remaining = cfg["budget"] - state["spent"]

        # Current P&L
        if state["quantity"] > 0 and state["avg_entry"] > 0:
            pnl_pct = (price - state["avg_entry"]) / state["avg_entry"] * 100
            pnl_str = ("+" if pnl_pct >= 0 else "") + str(round(pnl_pct, 2)) + "%"
            color = Fore.GREEN if pnl_pct >= 0 else Fore.RED
        else:
            pnl_str = "flat"
            color = Fore.WHITE

        print("  " + color + symbol.ljust(4) +
              "  $" + str(round(price, 2)) +
              "  RSI=" + rsi_str +
              "  held=" + str(round(state["quantity"], 6)) +
              "  avg=$" + str(round(state["avg_entry"], 2)) +
              "  " + pnl_str +
              "  budget left=$" + str(round(remaining, 2)))

        # ── SELL logic ─────────────────────────────
        if state["quantity"] > 0 and state["avg_entry"] > 0:
            target_price = state["avg_entry"] * (1 + cfg["target_pct"])
            stop_price   = state["avg_entry"] * (1 - cfg["stop_pct"])

            hit_target = price >= target_price
            hit_rsi    = rsi is not None and rsi >= cfg["rsi_sell"]
            hit_stop   = price <= stop_price

            if hit_stop:
                order = sell_crypto(symbol, state["quantity"],
                    "STOP " + str(round(stop_price, 2)) + " | RSI=" + rsi_str)
                if order:
                    state["quantity"]  = 0.0
                    state["avg_entry"] = 0.0
                    state["spent"]     = cfg["budget"]  # stop deploying after stop-out
                    state["last_action"] = "sell"

            elif hit_target or hit_rsi:
                reason = ("TARGET $" + str(round(target_price, 2)) if hit_target
                          else "RSI overbought " + rsi_str)
                order = sell_crypto(symbol, state["quantity"], reason)
                if order:
                    # Reset for next round
                    state["quantity"]    = 0.0
                    state["avg_entry"]   = 0.0
                    state["spent"]       = 0.0   # budget resets so we can trade again
                    state["last_action"] = "sell"
                    log.info(Fore.GREEN + symbol + " cycle complete - budget reset for next trade")

        # ── BUY logic ──────────────────────────────
        elif remaining >= cfg["tranche"]:
            do_buy = False
            reason = ""

            if rsi is None:
                # Not enough data yet - buy first tranche to start tracking
                if state["spent"] == 0:
                    do_buy = True
                    reason = "Initial entry (RSI warming up)"
            elif rsi <= cfg["rsi_buy"]:
                do_buy = True
                reason = "RSI oversold " + str(rsi)
            elif state["spent"] == 0:
                # First tranche even if RSI neutral (already bought manually)
                do_buy = False  # already have tranche 1 from manual buy

            if do_buy:
                order = buy_crypto(symbol, cfg["tranche"], reason)
                if order:
                    # Update state
                    qty_bought = cfg["tranche"] / price
                    prev_cost  = state["avg_entry"] * state["quantity"]
                    state["quantity"]  += qty_bought
                    state["spent"]     += cfg["tranche"]
                    state["avg_entry"]  = (prev_cost + cfg["tranche"]) / state["quantity"]
                    state["last_action"] = "buy"


# ─────────────────────────────────────────────
# EXISTING EQUITY FUNCTIONS (unchanged)
# ─────────────────────────────────────────────
def login():
    """Authenticate using Robinhood access token (bypasses Face ID/passkey)."""
    if not ROBIN_STOCKS_AVAILABLE:
        log.error("robin_stocks not available")
        return False

    access_token = os.getenv("RH_ACCESS_TOKEN")
    if not access_token:
        log.error("RH_ACCESS_TOKEN not set in .env")
        log.error("Open .env file and add: RH_ACCESS_TOKEN=your_token")
        return False

    try:
        rh.authentication.set_login_state(True)
        rh.authentication.SESSION.headers["Authorization"] = "Bearer " + access_token
        profile = rh.profiles.load_account_profile(account_number=ACCOUNT_NUMBER)
        if profile:
            log.info(Fore.GREEN + "Authenticated with Robinhood access token (account 8490)")
            return True
        else:
            log.error("Token accepted but profile empty - token may be expired")
            return False
    except Exception as e:
        log.error("Token authentication failed: " + str(e))
        return False


def logout():
    log.info("Session ended (token remains valid)")


def now_et():
    return datetime.now(ET)


def is_market_open():
    n = now_et()
    if n.weekday() >= 5:
        return False
    market_open  = n.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = n.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= n <= market_close


def is_premarket():
    n = now_et()
    if n.weekday() >= 5:
        return False
    pre_open = n.replace(hour=8,  minute=0,  second=0, microsecond=0)
    mkt_open = n.replace(hour=9,  minute=30, second=0, microsecond=0)
    return pre_open <= n < mkt_open


def is_afterhours():
    n = now_et()
    if n.weekday() >= 5:
        return False
    mkt_close = n.replace(hour=16, minute=0,  second=0, microsecond=0)
    ah_close  = n.replace(hour=20, minute=0,  second=0, microsecond=0)
    return mkt_close < n <= ah_close


def minutes_to_open():
    n = now_et()
    target = n.replace(hour=9, minute=30, second=0, microsecond=0)
    if n > target:
        target += timedelta(days=1)
    return int((target - n).total_seconds() / 60)


def get_prices(symbols):
    if not ROBIN_STOCKS_AVAILABLE:
        return {}
    try:
        quotes = rh.get_quotes(symbols, info="last_trade_price")
        return {sym: float(price) if price else None for sym, price in zip(symbols, quotes)}
    except Exception as e:
        log.error("Price fetch error: " + str(e))
        return {}


def get_portfolio_value():
    try:
        profile = rh.profiles.load_portfolio_profile()
        return float(profile.get("equity", 0))
    except:
        return 0.0


def get_positions():
    try:
        positions = rh.get_open_stock_positions()
        return {
            p["symbol"]: {
                "quantity": float(p["quantity"]),
                "avg_price": float(p["average_buy_price"])
            }
            for p in positions if p
        }
    except Exception as e:
        log.error("Position fetch error: " + str(e))
        return {}


def sell_position(symbol, quantity, reason):
    try:
        log.warning(Fore.RED + "SELLING " + symbol + " (" + str(round(quantity, 6)) + " shares) - " + reason)
        order = rh.orders.order_sell_fractional_by_quantity(
            symbol, quantity, timeInForce="gfd", extendedHours=False)
        log.info("Sell order placed: " + str(order))
        return order
    except Exception as e:
        log.error("Failed to sell " + symbol + ": " + str(e))
        return None


def buy_position(symbol, dollar_amount, reason):
    try:
        log.info(Fore.GREEN + "BUYING " + symbol + " $" + str(dollar_amount) + " - " + reason)
        order = rh.orders.order_buy_fractional_by_price(
            symbol, dollar_amount, timeInForce="gfd", extendedHours=False)
        log.info("Buy order placed: " + str(order))
        return order
    except Exception as e:
        log.error("Failed to buy " + symbol + ": " + str(e))
        return None


daily_pnl = 0.0
daily_loss_triggered = False


def monitor_stops():
    global daily_pnl, daily_loss_triggered
    if not is_market_open():
        return
    if daily_loss_triggered:
        log.warning("Daily loss limit hit - no actions until tomorrow")
        return

    symbols = list(POSITIONS.keys())
    prices = get_prices(symbols)
    portfolio_value = get_portfolio_value()
    daily_loss_limit = portfolio_value * MAX_DAILY_LOSS_PCT

    print("\n" + Fore.CYAN + "--- STOP MONITOR " + now_et().strftime("%H:%M:%S ET") + " ---")

    for symbol, config in POSITIONS.items():
        price = prices.get(symbol)
        if not price:
            log.warning("No price for " + symbol + " - skipping")
            continue

        entry      = config["entry"]
        stop_price = entry * (1 - config["stop_pct"])
        tgt_price  = entry * (1 + config["target_pct"])
        pnl_pct    = (price - entry) / entry * 100
        bucket     = config["bucket"]

        color = Fore.GREEN if price > entry else Fore.RED
        print("  " + color + symbol.ljust(6) + "  entry $" + str(round(entry, 2)) +
              "  now $" + str(round(price, 2)) +
              "  (" + ("+" if pnl_pct >= 0 else "") + str(round(pnl_pct, 1)) + "%)" +
              "  stop $" + str(round(stop_price, 2)) +
              "  target $" + str(round(tgt_price, 2)) +
              "  [" + bucket + "]")

        if price <= stop_price:
            live = get_positions()
            pos = live.get(symbol)
            if pos and pos["quantity"] > 0:
                pnl = (price - entry) * pos["quantity"]
                daily_pnl += pnl
                sell_position(symbol, pos["quantity"],
                    "STOP HIT - $" + str(round(price, 2)) + " <= $" + str(round(stop_price, 2)))
                if abs(daily_pnl) >= daily_loss_limit and daily_pnl < 0:
                    log.error(Fore.RED + "DAILY LOSS LIMIT HIT - HALTING")
                    daily_loss_triggered = True
                    return

        elif price >= tgt_price:
            live = get_positions()
            pos = live.get(symbol)
            if pos and pos["quantity"] > 0:
                if bucket == "asymmetric":
                    half = pos["quantity"] / 2
                    daily_pnl += (price - entry) * half
                    sell_position(symbol, half, "TARGET 1 HIT (50% exit) - $" + str(round(price, 2)))
                    POSITIONS[symbol]["stop_pct"] = 0.0
                    POSITIONS[symbol]["entry"] = price
                    POSITIONS[symbol]["target_pct"] = config["target_pct"] * 1.5
                else:
                    daily_pnl += (price - entry) * pos["quantity"]
                    sell_position(symbol, pos["quantity"],
                        "TARGET HIT - $" + str(round(price, 2)) + " >= $" + str(round(tgt_price, 2)))

    pnl_color = Fore.GREEN if daily_pnl >= 0 else Fore.RED
    print("  Session P&L: " + pnl_color + ("+" if daily_pnl >= 0 else "") + str(round(daily_pnl, 2)))


def premarket_scan():
    if not is_premarket() and not is_market_open():
        return
    log.info("\nPRE-MARKET SCAN - " + now_et().strftime("%A %b %d, %Y %H:%M ET"))

    if ANTHROPIC_API_KEY:
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Today is " + now_et().strftime("%A, %B %d, %Y") + ". "
                            "Search for the top 5 pre-market stock news catalysts right now "
                            "that could drive 3-10% moves today. For each: ticker, catalyst "
                            "(1 sentence), direction (long/short), signal strength 1-10. "
                            "Format as numbered list. No penny stocks."
                        )
                    }]
                },
                timeout=30
            )
            data = response.json()
            result = " ".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
            log.info("\nAI SCAN:\n" + result + "\n")
        except Exception as e:
            log.error("AI scan failed: " + str(e))

    log.info("\nPOSITIONS:")
    prices = get_prices(list(POSITIONS.keys()))
    for sym, cfg in POSITIONS.items():
        price = prices.get(sym)
        if price:
            pnl = (price - cfg["entry"]) / cfg["entry"] * 100
            color = Fore.GREEN if pnl >= 0 else Fore.RED
            log.info("  " + color + sym + ": $" + str(cfg["entry"]) + " -> $" + str(round(price, 2)) +
                     " (" + ("+" if pnl >= 0 else "") + str(round(pnl, 1)) + "%)")
    log.info("\nMarket opens in " + str(minutes_to_open()) + " minutes\n")


def end_of_day_reset():
    global daily_pnl, daily_loss_triggered
    log.info("\nEND OF DAY - " + now_et().strftime("%A %b %d, %Y"))
    color = Fore.GREEN if daily_pnl >= 0 else Fore.RED
    log.info("  Session P&L: " + color + ("+" if daily_pnl >= 0 else "") + str(round(daily_pnl, 2)))
    with open("trading_journal.txt", "a") as f:
        f.write(now_et().strftime("%Y-%m-%d") + " | P&L: $" + str(round(daily_pnl, 2)) +
                " | Loss limit: " + str(daily_loss_triggered) + "\n")
    daily_pnl = 0.0
    daily_loss_triggered = False


def orb_check():
    if not is_market_open():
        return
    log.info("\nORB WINDOW OPEN - " + now_et().strftime("%H:%M ET"))
    log.info("  First 15-min candle closed. Check watchlist for breakouts.")


def scalp_window_open():
    if not is_market_open():
        return
    log.info("\nSCALP WINDOW OPEN - " + now_et().strftime("%H:%M ET"))


def scalp_window_close():
    if not is_market_open():
        return
    log.info("\nSCALP WINDOW CLOSED - no new entries after 11 AM")


def setup_schedule():
    # Equity stop monitor - market hours only
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("08:30").do(premarket_scan)
        getattr(schedule.every(), day).at("09:46").do(orb_check)
        getattr(schedule.every(), day).at("10:00").do(scalp_window_open)
        getattr(schedule.every(), day).at("11:00").do(scalp_window_close)
        getattr(schedule.every(), day).at("16:05").do(end_of_day_reset)

    schedule.every(SCAN_INTERVAL_MINS).minutes.do(monitor_stops)
    schedule.every().sunday.at("20:00").do(
        lambda: log.info("Check ex-dividend calendar for the week"))

    # Crypto scalper - runs 24/7, every 5 minutes
    schedule.every(5).minutes.do(monitor_crypto)

    log.info("Schedule configured:")
    log.info("  Equity stop monitor: every 5 min during market hours")
    log.info("  Crypto scalper (BTC+SOL): every 5 min, 24/7")


def main():
    print(Fore.CYAN + """
+----------------------------------------------------------+
|          AGENTIC TRADING SCHEDULER                       |
|          Account: 8490  |  $100 -> $1,000               |
|          Crypto: BTC + SOL scalper (24/7)                |
+----------------------------------------------------------+
""" + Style.RESET_ALL)

    if not login():
        log.error("Cannot start - check RH_ACCESS_TOKEN in .env")
        return

    setup_schedule()

    # AMAT: sell at open Monday (earnings miss Sep 25 AH)
    AMAT_SELL_AT_OPEN = True
    if AMAT_SELL_AT_OPEN:
        def sell_amat_at_open():
            if not is_market_open():
                return
            n = now_et()
            open_time = n.replace(hour=9, minute=30, second=0, microsecond=0)
            if abs((n - open_time).total_seconds()) < 120:
                log.warning("AMAT EARNINGS EXIT - selling at open")
                live = get_positions()
                pos = live.get("AMAT")
                if pos and pos["quantity"] > 0:
                    sell_position("AMAT", pos["quantity"], "Earnings miss - sell at open")
        schedule.every().monday.at("09:30").do(sell_amat_at_open)
        schedule.every().tuesday.at("09:30").do(sell_amat_at_open)
        log.warning("AMAT sell-at-open armed for Monday 9:30 ET")

    # Seed crypto state from manual buys placed earlier today
    # BTC: bought ~0.00001178 BTC @ $85,666
    crypto_state["BTC"]["quantity"]  = 0.00001178
    crypto_state["BTC"]["avg_entry"] = 85666.0
    crypto_state["BTC"]["spent"]     = 1.00

    # SOL: bought ~0.00817 SOL @ $123.61
    crypto_state["SOL"]["quantity"]  = 0.00817
    crypto_state["SOL"]["avg_entry"] = 123.61
    crypto_state["SOL"]["spent"]     = 1.00

    log.info("Crypto state seeded from earlier manual buys:")
    log.info("  BTC: 0.00001178 BTC @ $85,666 avg | $1 deployed")
    log.info("  SOL: 0.00817 SOL @ $123.61 avg | $1 deployed")

    if is_premarket():
        premarket_scan()

    # Run crypto monitor immediately on start
    monitor_crypto()

    log.info("Running. ET time: " + now_et().strftime("%H:%M:%S") + " | Press Ctrl+C to stop\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Scheduler stopped.")
        logout()


if __name__ == "__main__":
    main()
