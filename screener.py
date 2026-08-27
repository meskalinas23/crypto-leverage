"""
Crypto momentum screener: range breakout + retest, using Binance USDT-M Futures data.
Scans top N coins by market cap, flags coins where:
  1. Price recently broke above a consolidation range (1h close above range high)
  2. Price retested that level and closed back above it (the entry trigger)
Also checks BTC's own range position as a loose market-context filter.
"""

import requests
import pandas as pd
import time

# ---- Config ----
TOP_N_BY_MARKETCAP = 200
RANGE_LOOKBACK = 40          # candles used to define the "consolidation range"
BREAKOUT_LOOKBACK = 10       # how many recent candles to check for a breakout event
MIN_RANGE_TIGHTNESS = 0.15   # range (high-low)/low must be under this to count as "consolidating"
RETEST_MAX_CANDLES = 6       # how many candles after breakout we allow for the retest
MIN_RISK_REWARD = 2.0
REQUEST_PAUSE_SEC = 0.2

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"}


def get_top_marketcap_symbols(n=TOP_N_BY_MARKETCAP):
    """Pull top N coins by market cap from CoinGecko, return as Binance-style USDT symbols."""
    symbols = []
    resp = requests.get(
        f"{COINGECKO_BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        },
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for coin in data[:n]:
        symbols.append(coin["symbol"].upper() + "USDT")
    return symbols


def get_binance_tradeable_symbols():
    """Get the set of USDT perpetual symbols actually tradeable on Binance Futures."""
    resp = requests.get(
        f"{BINANCE_FUTURES_BASE}/fapi/v1/exchangeInfo",
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    tradeable = set()
    for item in data["symbols"]:
        if item["status"] == "TRADING" and item["symbol"].endswith("USDT") and item["contractType"] == "PERPETUAL":
            tradeable.add(item["symbol"])
    return tradeable


def get_klines(symbol, interval="1h", limit=200):
    """Fetch candles from Binance Futures. Returns DataFrame oldest->newest."""
    resp = requests.get(
        f"{BINANCE_FUTURES_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ],
    )
    df = df.astype(
        {
            "timestamp": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
        }
    )
    df = df.sort_values("timestamp").reset_index(drop=True)  # oldest -> newest
    return df


def find_range(df, lookback=RANGE_LOOKBACK, end_idx=None):
    """Find the high/low of a consolidation range ending at end_idx (exclusive)."""
    if end_idx is None:
        end_idx = len(df)
    start_idx = max(0, end_idx - lookback)
    window = df.iloc[start_idx:end_idx]
    if len(window) < lookback // 2:
        return None
    range_high = window["high"].max()
    range_low = window["low"].min()
    if range_low <= 0:
        return None
    tightness = (range_high - range_low) / range_low
    return {
        "high": range_high,
        "low": range_low,
        "tightness": tightness,
        "start_idx": start_idx,
        "end_idx": end_idx,
    }


def detect_breakout_and_retest(df):
    """
    Look through recent candles for:
      1. A range (computed using candles BEFORE the breakout candle)
      2. A breakout candle: closes above range high
      3. A retest candle within RETEST_MAX_CANDLES: dips toward range high, closes back above it
    Returns a dict describing the setup if found, else None.
    """
    n = len(df)
    if n < RANGE_LOOKBACK + BREAKOUT_LOOKBACK + RETEST_MAX_CANDLES:
        return None

    search_start = n - BREAKOUT_LOOKBACK - RETEST_MAX_CANDLES
    for i in range(search_start, n - 1):
        rng = find_range(df, lookback=RANGE_LOOKBACK, end_idx=i)
        if rng is None:
            continue
        if rng["tightness"] > MIN_RANGE_TIGHTNESS:
            continue

        breakout_candle = df.iloc[i]
        if breakout_candle["close"] <= rng["high"]:
            continue

        for j in range(i + 1, min(i + 1 + RETEST_MAX_CANDLES, n)):
            candle = df.iloc[j]
            dipped_into_zone = candle["low"] <= rng["high"] * 1.01
            closed_above = candle["close"] > rng["high"]
            if dipped_into_zone and closed_above:
                return {
                    "range_high": rng["high"],
                    "range_low": rng["low"],
                    "breakout_idx": i,
                    "retest_idx": j,
                    "entry": candle["close"],
                    "stop": rng["low"],
                }
            if candle["close"] < rng["low"]:
                break
    return None


def analyze_symbol(symbol):
    df = get_klines(symbol)
    if df is None or len(df) < RANGE_LOOKBACK + BREAKOUT_LOOKBACK:
        return None

    setup = detect_breakout_and_retest(df)
    if setup is None:
        return None

    entry = setup["entry"]
    stop = setup["stop"]
    risk = entry - stop
    if risk <= 0:
        return None

    range_height = setup["range_high"] - setup["range_low"]
    target = setup["range_high"] + range_height * 2
    reward = target - entry
    risk_reward = reward / risk
    if risk_reward < MIN_RISK_REWARD:
        return None

    return {
        "symbol": symbol,
        "entry": round(entry, 6),
        "stop": round(stop, 6),
        "target": round(target, 6),
        "risk_reward": round(risk_reward, 2),
        "range_high": round(setup["range_high"], 6),
        "range_low": round(setup["range_low"], 6),
    }


def get_btc_context():
    """Loose market filter: is BTC near/above its own recent range high?"""
    df = get_klines("BTCUSDT")
    if df is None:
        return None
    rng = find_range(df, lookback=RANGE_LOOKBACK, end_idx=len(df) - 1)
    if rng is None:
        return None
    last_close = df.iloc[-1]["close"]
    near_or_above = last_close >= rng["high"] * 0.98
    return {
        "range_high": rng["high"],
        "last_close": last_close,
        "near_or_above_breakout": near_or_above,
    }


def main():
    print("Fetching top coins by market cap...")
    mcap_symbols = get_top_marketcap_symbols()
    print(f"Got {len(mcap_symbols)} candidates from market cap list.")

    print("Fetching Binance Futures tradeable symbols...")
    tradeable = get_binance_tradeable_symbols()

    symbols = [s for s in mcap_symbols if s in tradeable]
    print(f"{len(symbols)} of those are tradeable as USDT perps on Binance Futures.")

    btc_ctx = get_btc_context()
    if btc_ctx:
        print(
            f"BTC context: last close {btc_ctx['last_close']:.2f}, "
            f"range high {btc_ctx['range_high']:.2f}, "
            f"near/above breakout: {btc_ctx['near_or_above_breakout']}"
        )

    hits = []
    for symbol in symbols:
        try:
            result = analyze_symbol(symbol)
            if result:
                hits.append(result)
        except Exception as e:
            print(f"  skipped {symbol}: {e}")
        time.sleep(REQUEST_PAUSE_SEC)

    print(f"\nFound {len(hits)} setups.\n")
    if hits:
        print("| Symbol | Entry | Stop | Target | R:R | Range |")
        print("|---|---|---|---|---|---|")
        for h in hits:
            print(
                f"| {h['symbol']} | {h['entry']} | {h['stop']} | {h['target']} | "
                f"{h['risk_reward']}:1 | {h['range_low']}-{h['range_high']} |"
            )


if __name__ == "__main__":
    main()
