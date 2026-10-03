# 🤖 Bot Trading System

> Automated crypto trading on Robinhood — 7 specialized bots running in parallel, each with its own strategy, risk limits, and a shared ML feedback loop that continuously improves performance.

---

## 🌐 Live Dashboard

**[→ View BotShare Dashboard](https://sloansuperservices.github.io/bot_trading_system)**

The BotShare dashboard shows real-time bot status, P&L, signal consensus, and trade history across all 7 bots. Built as a free GitHub Pages site — no server required.

---

## 🗺️ System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    TRADING SCHEDULER                        │
│              (trading_scheduler.py — runs all bots)         │
└────────────┬──────────────────────────────────┬────────────┘
             │                                  │
   ┌─────────▼──────────┐            ┌──────────▼─────────┐
   │   SIGNAL CONSENSUS  │            │   WEEKLY REVIEW     │
   │ system/signal_      │            │ system/weekly_      │
   │ consensus.json      │            │ review.py (Sun 2AM) │
   └─────────┬──────────┘            └──────────┬─────────┘
             │ shared across all bots             │ retrains ML
             │                                   │
   ┌─────────▼──────────────────────────────────▼──────────┐
   │                    7 TRADING BOTS                      │
   │  Alpha · DeFi · Macro · Momentum · Dynamic · Sentiment · ML  │
   └────────────────────────────────────────────────────────┘
```

---

## 🤖 The 7 Bots

| # | Bot | File | Coins | Daily Cap | Cycle | Strategy |
|---|-----|------|-------|-----------|-------|----------|
| 1 | **ALPHA TRADER** | `scalper_alpha.py` | BTC, ETH | $10 | 5 min | RSI + Bollinger Bands momentum scalper |
| 2 | **DEFI TRADER** | `scalper_defi.py` | SOL, LINK, MATIC | $15 | 5 min | DeFi-sector momentum + volume spikes |
| 3 | **MACRO TRADER** | `scalper_macro.py` | BTC, ETH | $20 | 15 min | Macro trend-following with ATR stops |
| 4 | **MOMENTUM TRADER** | `scalper_momentum.py` | BTC, ETH, SOL | $15 | 5 min | Price momentum + EMA crossovers |
| 5 | **DYNAMIC TRADER** | `scalper_dynamic.py` | BTC, ETH, SOL, LINK | $20 | 5 min | Adaptive scoring → dynamic targets & tranches |
| 6 | **SENTIMENT TRADER** | `scalper_sentiment.py` | BTC, ETH | $10 | 10 min | CryptoPanic headlines + Fear & Greed Index |
| 7 | **ML TRADER** | `scalper_ml.py` | BTC, ETH, SOL | $15 | 15 min | Random Forest + XGBoost + LSTM ensemble |

**Total max daily exposure: ~$105** across all 7 bots (each has independent caps + loss limits).

---

## 🧠 ML Pipeline

The ML Trader (Bot 7) uses an offline-trained ensemble of 3 models predicting whether price will rise >1% in the next 3 days.

```
ml/
├── train_ml.py        ← run this to retrain (takes ~5 min)
└── models/
    ├── rf_model.pkl   ← Random Forest (200 trees, max_depth=8)
    ├── xgb_model.pkl  ← XGBoost (200 trees, lr=0.05)
    ├── lstm_model.h5  ← LSTM (2×64 units, dropout=0.2, seq=20)
    └── scaler.pkl     ← StandardScaler for features
```

**Features:** RSI(14), Bollinger Band position, Volume ratio, 5-day momentum, ATR(14), EMA ratio

**Signal rule:** All 3 models must agree with >65% confidence to trigger a BUY.

To retrain:
```powershell
pip install xgboost tensorflow yfinance scikit-learn
python ml\train_ml.py
```

---

## 🔄 Weekly Self-Improvement Loop

`system/weekly_review.py` runs every Sunday at 2AM and:
- Reads 7 days of logs across all bots
- Calculates win rate, P&L, and drawdown per bot
- **Auto-adjusts** caps up (WR > 70%) or down (WR < 45%)
- **Flags for human review** if any coin loses 3+ consecutive weeks
- Retrains the ML models with fresh data
- Saves report to `logs/weekly_report_YYYYMMDD.txt`

---

## 📁 Project Structure

```
trading/
├── scalper_alpha.py          # Bot 1
├── scalper_defi.py           # Bot 2
├── scalper_macro.py          # Bot 3
├── scalper_momentum.py       # Bot 4
├── scalper_dynamic.py        # Bot 5
├── scalper_sentiment.py      # Bot 6
├── scalper_ml.py             # Bot 7
├── trading_scheduler.py      # Runs all bots together
├── dashboard.py              # Local dashboard (terminal)
├── ml/
│   ├── train_ml.py           # Offline ML training pipeline
│   └── models/               # Trained model files (gitignored)
├── system/
│   ├── weekly_review.py      # Sunday improvement loop
│   └── signal_consensus.json # Shared signal state
├── state/                    # Per-bot state JSONs (gitignored)
├── logs/                     # Daily log files (gitignored)
├── docs/
│   └── index.html            # BotShare dashboard (GitHub Pages)
└── .env                      # API keys — NEVER committed
```

---

## 🚀 Quick Start

**1. Install dependencies**
```powershell
pip install robin_stocks yfinance xgboost tensorflow scikit-learn colorama python-dotenv requests
```

**2. Set up your `.env` file** (never commit this)
```
RH_USER=your@email.com
RH_PASS=yourpassword
# or use bearer token:
RH_BEARER_TOKEN=your_token_here
ANTHROPIC_API_KEY=your_key_here
```

**3. Train the ML models** (first time only)
```powershell
python ml\train_ml.py
```

**4. Run all bots**
```powershell
python trading_scheduler.py
```

---

## 🔒 Security

- `.env` is in `.gitignore` and will **never** be committed
- `state/` JSON files (local bot state) are excluded
- `ml/models/` binary files are excluded (retrain locally)
- `logs/` are excluded

---

## 📊 BotShare — Live Dashboard

The `docs/index.html` page is hosted for free on GitHub Pages:

**https://sloansuperservices.github.io/bot_trading_system**

This is the public-facing dashboard for the trading system. Future roadmap: connect it to live bot data via API so performance stats update in real time.

---

*Built with Python · Robinhood via robin_stocks · ML via scikit-learn, XGBoost, TensorFlow*
