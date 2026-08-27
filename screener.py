"""
Crypto momentum screener (CoinGecko version): range breakout + retest.
Uses CoinGecko for both the top-N-by-market-cap list and hourly price history,
since Bybit/Binance block direct API access from GitHub Actions' servers.

Note: CoinGecko's free tier gives hourly PRICE POINTS, not full OHLC candles,
so breakout/retest detection here is point-based rather than wick-based.
Less precise than exchange candle data, but functional and not IP-blocked.
"""

import requests
import pandas as pd
import time

# ---- Config ----
TOP_N_BY_MARKETCAP = 200
HISTORY_DAYS = 14             # how many days of hourly price history to pull per coin
RANGE_LOOKBACK = 40           # points used to define the "consolidation range"
BREAKOUT_LOOKBACK = 10        # how many recent points to check for a breakout event
MIN_RANGE_TIGHTNESS = 0.15    # (high-low)/low must be under this to count as "consolidating"
RETEST_MAX_POINTS = 6         # how many points after breakout we allow for the retest
MIN_RISK_REWARD = 2.0
REQUEST_PAUSE_SEC = 1.5       # CoinGecko free tier rate-limits fairly aggressively

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"}


def get_top_coins(n=TOP_N_BY_MARKETCAP):
    """Pull top N coins by market cap. Returns list of dicts with id + symbol."""
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
    return [{"id": c["id"], "symbol": c["symbol"].upper()} for c in data[:n]]


def get_hourly_prices(coin_id, days=HISTORY_DAYS):
    """Fetch hourly price history for a coin. Returns DataFrame with timestamp, price."""
    resp = requests.get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days},
        headers=HEADERS,
        timeout=15,
    )
    if resp.status_code == 429:
        raise Exception("rate limited")
    resp.raise_for_status()
    data = resp.json()
    prices = data.get("prices", [])
    if not prices:
        return None
    df = pd.DataFrame(prices, columns=["timestamp", "price"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def find_range(df, lookback=RANGE_LOOKBACK, end_idx=None):
    """Find the high/low of a consolidation range ending at end_idx (exclusive)."""
    if end_idx is None:
        end_idx = len(df)
    start_idx = max(0, end_idx - lookback)
    window = df.iloc[start_idx:end_idx]
    if len(window) < lookback // 2:
        return None
    range_high = window["price"].max()
    range_low = window["price"].min()
    if range_low <= 0:
        return None
    tightness = (range_high - range_low) / range_low
    return {"high": range_high, "low": range_low, "tightness": tightness}


def detect_breakout_and_retest(df):
    """
    Point-based version of the breakout+retest pattern:
      1. A tight range (computed using points BEFORE the breakout point)
      2. A breakout point: price above range high
      3. A retest point within RETEST_MAX_POINTS: dips near range high, comes back above it
    """
    n = len(df)
    if n < RANGE_LOOKBACK + BREAKOUT_LOOKBACK + RETEST_MAX_POINTS:
        return None

    search_start = n - BREAKOUT_LOOKBACK - RETEST_MAX_POINTS
    for i in range(search_start, n - 1):
        rng = find_range(df, lookback=RANGE_LOOKBACK, end_idx=i)
        if rng is None:
            continue
        if rng["tightness"] > MIN_RANGE_TIGHTNESS:
            continue

        breakout_price = df.iloc[i]["price"]
        if breakout_price <= rng["high"]:
            continue

        for j in range(i + 1, min(i + 1 + RETEST_MAX_POINTS, n)):
            price = df.iloc[j]["price"]
            near_zone = price <= rng["high"] * 1.02
            back_above = price > rng["high"]
            if near_zone and back_above:
                return {
                    "range_high": rng["high"],
                    "range_low": rng["low"],
                    "entry": price,
                    "stop": rng["low"],
                }
            if price < rng["low"]:
                break
    return None


def analyze_coin(coin_id, symbol):
    df = get_hourly_prices(coin_id)
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
    df = get_hourly_prices("bitcoin")
    if df is None:
        return None
    rng = find_range(df, lookback=RANGE_LOOKBACK, end_idx=len(df) - 1)
    if rng is None:
        return None
    last_price = df.iloc[-1]["price"]
    near_or_above = last_price >= rng["high"] * 0.98
    return {
        "range_high": rng["high"],
        "last_price": last_price,
        "near_or_above_breakout": near_or_above,
    }


def main():
    print("Fetching top coins by market cap...")
    coins = get_top_coins()
    print(f"Got {len(coins)} coins.")

    print("Checking BTC context...")
    btc_ctx = get_btc_context()
    if btc_ctx:
        print(
            f"BTC context: last price {btc_ctx['last_price']:.2f}, "
            f"range high {btc_ctx['range_high']:.2f}, "
            f"near/above breakout: {btc_ctx['near_or_above_breakout']}"
        )
    time.sleep(REQUEST_PAUSE_SEC)

    hits = []
    for coin in coins:
        try:
            result = analyze_coin(coin["id"], coin["symbol"])
            if result:
                hits.append(result)
        except Exception as e:
            print(f"  skipped {coin['symbol']}: {e}")
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
