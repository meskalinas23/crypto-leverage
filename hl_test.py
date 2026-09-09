"""
Quick connectivity test: can we reach Hyperliquid's and DexScreener's public
APIs from GitHub Actions? No auth needed for either - this just does a
simple read as a sanity check before building anything real on top of them.
"""

import requests

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/search"


def test_connection():
    resp = requests.post(
        HYPERLIQUID_API,
        json={"type": "meta"},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    universe = data.get("universe", [])
    print(f"SUCCESS: reached Hyperliquid API. Found {len(universe)} perp markets.")
    print("First few:", [u["name"] for u in universe[:10]])


def test_leaderboard():
    """The real leaderboard lives on a separate stats endpoint, not /info."""
    try:
        resp = requests.get(
            "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
            headers={"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("leaderboardRows", data if isinstance(data, list) else [])
        print(f"SUCCESS: leaderboard endpoint responded. Rows found: {len(rows) if isinstance(rows, list) else 'unknown shape'}")
        print("Sample:", str(rows[:1] if isinstance(rows, list) else data)[:500])
    except Exception as e:
        print(f"Leaderboard endpoint failed: {e}")


def test_dexscreener():
    try:
        resp = requests.get(
            DEXSCREENER_API,
            params={"q": "SOL"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        print(f"SUCCESS: reached DexScreener API. Found {len(pairs)} pairs for 'SOL'.")
        if pairs:
            print("First pair:", pairs[0].get("baseToken", {}).get("symbol"), "/", pairs[0].get("quoteToken", {}).get("symbol"))
    except Exception as e:
        print(f"DexScreener test failed: {e}")


if __name__ == "__main__":
    print("--- Testing Hyperliquid ---")
    test_connection()
    test_leaderboard()
    print("\n--- Testing DexScreener ---")
    test_dexscreener()
