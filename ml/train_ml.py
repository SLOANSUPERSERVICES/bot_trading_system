"""
ml/train_ml.py — Offline ML training pipeline for crypto trading bot.
Trains Random Forest, XGBoost, and LSTM models on BTC-USD, ETH-USD, SOL-USD.
Saves models to ml/models/ for use by the live trading bot.
"""

import os
import sys
import warnings
import joblib
import numpy as np
import pandas as pd
import yfinance as yf

from pathlib import Path
from datetime import datetime, timedelta

from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import train_test_split

import xgboost as xgb

import tensorflow as tf
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout
from tensorflow.keras.callbacks import EarlyStopping

from colorama import init, Fore, Style

# Suppress TensorFlow and sklearn warnings for cleaner output
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
warnings.filterwarnings("ignore")
tf.get_logger().setLevel("ERROR")

init(autoreset=True)

# ─── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent          # ml/
BASE_DIR   = SCRIPT_DIR.parent.parent                  # project root (two levels up)
MODELS_DIR = SCRIPT_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

RF_PATH     = MODELS_DIR / "rf_model.pkl"
XGB_PATH    = MODELS_DIR / "xgb_model.pkl"
LSTM_PATH   = MODELS_DIR / "lstm_model.h5"
SCALER_PATH = MODELS_DIR / "scaler.pkl"

# ─── Config ───────────────────────────────────────────────────────────────────

COINS          = ["BTC-USD", "ETH-USD", "SOL-USD"]
LOOKBACK_YEARS = 2
LABEL_HORIZON  = 3        # days ahead
LABEL_THRESH   = 0.01     # 1% rise threshold
TRAIN_RATIO    = 0.80
SEQ_LEN        = 20       # LSTM sequence window

RF_PARAMS  = dict(n_estimators=200, max_depth=8, random_state=42, n_jobs=-1)
XGB_PARAMS = dict(n_estimators=200, max_depth=6, learning_rate=0.05,
                  random_state=42, eval_metric="logloss", verbosity=0)
LSTM_EPOCHS     = 30
LSTM_BATCH_SIZE = 32

# ─── Helpers ──────────────────────────────────────────────────────────────────

def header(text: str) -> None:
    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'─'*60}")
    print(f"  {text}")
    print(f"{'─'*60}{Style.RESET_ALL}")


def ok(text: str) -> None:
    print(f"{Fore.GREEN}  ✓  {text}{Style.RESET_ALL}")


def warn(text: str) -> None:
    print(f"{Fore.YELLOW}  ⚠  {text}{Style.RESET_ALL}")


def info(text: str) -> None:
    print(f"     {text}")


def fmt_size(path: Path) -> str:
    size = path.stat().st_size
    if size >= 1_048_576:
        return f"{size / 1_048_576:.2f} MB"
    return f"{size / 1_024:.1f} KB"


# ─── Data Fetching ────────────────────────────────────────────────────────────

def fetch_ohlcv(ticker: str, years: int = 2) -> pd.DataFrame:
    end   = datetime.today()
    start = end - timedelta(days=years * 365)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    return df


# ─── Feature Engineering ─────────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l  = loss.ewm(com=period - 1, min_periods=period).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    close  = df["close"]
    volume = df["volume"]

    # RSI(14)
    df["rsi"] = compute_rsi(close, 14)

    # Bollinger Band position
    bb_mid   = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_range = (bb_upper - bb_lower).replace(0, np.nan)
    df["bb_pos"] = (close - bb_lower) / bb_range

    # Volume ratio
    vol_ma = volume.rolling(20).mean()
    df["vol_ratio"] = volume / vol_ma.replace(0, np.nan)

    # Price momentum (5-day)
    df["momentum"] = close.pct_change(5)

    # ATR(14) normalized
    df["atr_norm"] = compute_atr(df, 14) / close

    # EMA ratio
    ema20 = close.ewm(span=20).mean()
    df["ema_ratio"] = close / ema20.replace(0, np.nan)

    return df


def build_label(df: pd.DataFrame,
                horizon: int = 3,
                threshold: float = 0.01) -> pd.DataFrame:
    future_return = df["close"].shift(-horizon) / df["close"] - 1
    df["label"] = (future_return > threshold).astype(int)
    return df


FEATURE_COLS = ["rsi", "bb_pos", "vol_ratio", "momentum", "atr_norm", "ema_ratio"]


def prepare_dataset(coins: list[str]) -> tuple[pd.DataFrame, pd.Series]:
    frames = []
    for coin in coins:
        info(f"Fetching {coin}…")
        try:
            df = fetch_ohlcv(coin, LOOKBACK_YEARS)
            df = engineer_features(df)
            df = build_label(df, LABEL_HORIZON, LABEL_THRESH)
            df["coin"] = coin
            frames.append(df)
            ok(f"{coin}: {len(df):,} rows")
        except Exception as exc:
            warn(f"Skipping {coin} — {exc}")

    if not frames:
        raise RuntimeError("No data could be fetched. Check your internet connection.")

    combined = pd.concat(frames).dropna(subset=FEATURE_COLS + ["label"])
    # Sort chronologically within each coin, then concatenate
    combined = combined.sort_index()

    X = combined[FEATURE_COLS]
    y = combined["label"]
    return X, y


# ─── Chronological Train/Test Split ──────────────────────────────────────────

def chrono_split(X: pd.DataFrame,
                 y: pd.Series,
                 ratio: float = 0.80
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n     = len(X)
    split = int(n * ratio)
    X_tr, X_te = X.iloc[:split].values, X.iloc[split:].values
    y_tr, y_te = y.iloc[:split].values, y.iloc[split:].values
    return X_tr, X_te, y_tr, y_te


# ─── LSTM Sequence Builder ────────────────────────────────────────────────────

def build_sequences(X: np.ndarray,
                    y: np.ndarray,
                    seq_len: int = 20
                    ) -> tuple[np.ndarray, np.ndarray]:
    Xs, ys = [], []
    for i in range(seq_len, len(X)):
        Xs.append(X[i - seq_len:i])
        ys.append(y[i])
    return np.array(Xs), np.array(ys)


# ─── Model Builders ──────────────────────────────────────────────────────────

def build_lstm(input_shape: tuple) -> Sequential:
    model = Sequential([
        LSTM(64, return_sequences=True, input_shape=input_shape),
        Dropout(0.2),
        LSTM(64, return_sequences=False),
        Dropout(0.2),
        Dense(1, activation="sigmoid"),
    ])
    model.compile(optimizer="adam", loss="binary_crossentropy", metrics=["accuracy"])
    return model


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'═'*60}")
    print(f"  CRYPTO ML TRAINING PIPELINE")
    print(f"  Base dir : {BASE_DIR}")
    print(f"  Models   : {MODELS_DIR}")
    print(f"{'═'*60}{Style.RESET_ALL}")

    # ── 1. Data ──────────────────────────────────────────────────────────────

    header("1 / 5  — Fetching & engineering features")
    X, y = prepare_dataset(COINS)
    info(f"Total samples: {len(X):,}  |  Features: {len(FEATURE_COLS)}")
    info(f"Label balance: {y.mean():.1%} positive (price up >{LABEL_THRESH:.0%} in {LABEL_HORIZON}d)")

    # ── 2. Split & scale ────────────────────────────────────────────────────

    header("2 / 5  — Splitting & scaling")
    X_tr, X_te, y_tr, y_te = chrono_split(X, y, TRAIN_RATIO)
    info(f"Train: {len(X_tr):,}  |  Test: {len(X_te):,}")

    scaler = StandardScaler()
    X_tr_s = scaler.fit_transform(X_tr)
    X_te_s = scaler.transform(X_te)

    joblib.dump(scaler, SCALER_PATH)
    ok(f"Scaler saved → {SCALER_PATH.name}  ({fmt_size(SCALER_PATH)})")

    # ── 3. Random Forest ────────────────────────────────────────────────────

    header("3 / 5  — Random Forest")
    info(f"n_estimators={RF_PARAMS['n_estimators']}, max_depth={RF_PARAMS['max_depth']}")

    rf = RandomForestClassifier(**RF_PARAMS)
    rf.fit(X_tr_s, y_tr)
    rf_pred = rf.predict(X_te_s)
    rf_acc  = accuracy_score(y_te, rf_pred)

    acc_color = Fore.GREEN if rf_acc >= 0.55 else Fore.YELLOW
    print(f"  Accuracy: {acc_color}{rf_acc:.4f}{Style.RESET_ALL}")
    print()
    print(classification_report(y_te, rf_pred, target_names=["Down/Flat", "Up >1%"]))

    joblib.dump(rf, RF_PATH)
    ok(f"RF model saved → {RF_PATH.name}  ({fmt_size(RF_PATH)})")

    # ── 4. XGBoost ──────────────────────────────────────────────────────────

    header("4 / 5  — XGBoost")
    info(f"n_estimators={XGB_PARAMS['n_estimators']}, max_depth={XGB_PARAMS['max_depth']}, "
         f"lr={XGB_PARAMS['learning_rate']}")

    xgb_model = xgb.XGBClassifier(**XGB_PARAMS)
    xgb_model.fit(
        X_tr_s, y_tr,
        eval_set=[(X_te_s, y_te)],
        verbose=False,
    )
    xgb_pred = xgb_model.predict(X_te_s)
    xgb_acc  = accuracy_score(y_te, xgb_pred)

    acc_color = Fore.GREEN if xgb_acc >= 0.55 else Fore.YELLOW
    print(f"  Accuracy: {acc_color}{xgb_acc:.4f}{Style.RESET_ALL}")
    print()
    print(classification_report(y_te, xgb_pred, target_names=["Down/Flat", "Up >1%"]))

    joblib.dump(xgb_model, XGB_PATH)
    ok(f"XGB model saved → {XGB_PATH.name}  ({fmt_size(XGB_PATH)})")

    # ── 5. LSTM ─────────────────────────────────────────────────────────────

    header("5 / 5  — LSTM")
    info(f"seq_len={SEQ_LEN}, layers=2×64 LSTM + 0.2 dropout, epochs={LSTM_EPOCHS}")

    X_seq_tr, y_seq_tr = build_sequences(X_tr_s, y_tr, SEQ_LEN)
    X_seq_te, y_seq_te = build_sequences(X_te_s, y_te, SEQ_LEN)
    info(f"Sequences — train: {len(X_seq_tr):,}  |  val: {len(X_seq_te):,}")

    lstm = build_lstm(input_shape=(SEQ_LEN, len(FEATURE_COLS)))

    early_stop = EarlyStopping(monitor="val_loss", patience=5, restore_best_weights=True)

    history = lstm.fit(
        X_seq_tr, y_seq_tr,
        validation_data=(X_seq_te, y_seq_te),
        epochs=LSTM_EPOCHS,
        batch_size=LSTM_BATCH_SIZE,
        callbacks=[early_stop],
        verbose=1,
    )

    final_val_acc = history.history["val_accuracy"][-1]
    epochs_run    = len(history.history["val_accuracy"])

    acc_color = Fore.GREEN if final_val_acc >= 0.55 else Fore.YELLOW
    print(f"\n  Val accuracy (epoch {epochs_run}): "
          f"{acc_color}{final_val_acc:.4f}{Style.RESET_ALL}")

    if epochs_run < LSTM_EPOCHS:
        warn(f"Early stopping triggered at epoch {epochs_run}/{LSTM_EPOCHS}")

    lstm.save(str(LSTM_PATH))
    ok(f"LSTM model saved → {LSTM_PATH.name}  ({fmt_size(LSTM_PATH)})")

    # ── Summary ─────────────────────────────────────────────────────────────

    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'═'*60}")
    print(f"  TRAINING COMPLETE — ALL MODELS SAVED")
    print(f"{'═'*60}{Style.RESET_ALL}")

    saved_files = [
        ("Random Forest", RF_PATH),
        ("XGBoost",       XGB_PATH),
        ("LSTM",          LSTM_PATH),
        ("Scaler",        SCALER_PATH),
    ]

    for label, path in saved_files:
        size = fmt_size(path) if path.exists() else "missing!"
        ok(f"{label:<16}  {path}  [{size}]")

    print(f"\n{Fore.CYAN}  Accuracies on test set (last {len(X_te):,} rows):{Style.RESET_ALL}")
    for name, acc in [("Random Forest", rf_acc), ("XGBoost", xgb_acc), ("LSTM", final_val_acc)]:
        color = Fore.GREEN if acc >= 0.55 else Fore.YELLOW
        bar   = "█" * int(acc * 20)
        print(f"  {name:<16}  {color}{bar:<20}  {acc:.4f}{Style.RESET_ALL}")

    print()


if __name__ == "__main__":
    main()
