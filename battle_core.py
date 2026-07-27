#!/usr/bin/env python3
"""
🥊 MOON DEV'S AI TRADING BATTLES - CORE MODULE 🥊
moondev.com/ai — 6 AI models trade against each other on Hyperliquid

This is the SHARED core - ALL battle logic lives here. run_battle.py is
the one-screen arena that runs all six fighters in a single process.
Any change made here applies to all six fighters - consistency across
the whole season.

DATA FLOW (as few Hyperliquid calls as possible):
  - Candles: Hyperliquid public API, fetched ONCE per cycle and cached to
    data/candles_cache.json. One HL data call per hour TOTAL, not six.
  - People's positions + liquidations: Moon Dev API (moondev.com/docs),
    same shared cache. Raw data only, zero interpretation - the AIs draw
    their own conclusions.
  - Bid/ask + account state: Moon Dev API (api.moondev.com) - zero HL
    reads. The six subaccounts are UNIFIED accounts: equity = free spot
    USDC (total - hold) + perps accountValue, all from ONE /api/account call.
  - Orders + leverage: Hyperliquid SDK (signed, can't be avoided).
  - Decisions + raw responses: pushed outbound to the Moon Dev API server
    every cycle -> the live feed on moondev.com/ai.

THE RULES (per bot, per cycle):
  FLAT      + LONG    -> market buy (open long)
  FLAT      + SHORT   -> market sell (open short)
  FLAT      + NOTHING -> stay flat
  IN LONG   + NOTHING/LONG -> keep holding
  IN LONG   + CLOSE   -> close position, go flat
  IN LONG   + SHORT   -> close long, then open short (flip)
  IN SHORT  + NOTHING/SHORT -> keep holding
  IN SHORT  + CLOSE   -> close position, go flat
  IN SHORT  + LONG    -> close short, then open long (flip)

Built with love by Moon Dev 🌙
"""

import os
import re
import json
import time
import fcntl
import requests
import pandas as pd
import datetime as dt
from datetime import timedelta
import sys
from pathlib import Path
from termcolor import cprint, colored
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# ⚙️ MOON DEV'S BATTLE CONFIG - EDIT THIS SECTION (applies to ALL fighters)
# ============================================================================

PLACE_TRADES = True              # ⚔️ LIVE. False = dry run (decisions logged, no orders, no feed push)
DECISION_INTERVAL_MINUTES = 60   # ⚔️ LIVE hourly cadence. 2 = dry-run testing
CYCLE_OFFSET_MINUTES = 13        # Decisions fire at :13 past, NEVER on the round hour - the :00 candle
                                 # turn is where every algo/TWAP herd clusters. Season 1 anti-herd hack.
LEVERAGE = 1                     # 1 = no leverage. THE variable to crank later.
SYMBOL = "BTC"                   # Beta = BTC only. More symbols in future seasons.
TIMEFRAME = "1h"                 # Candle timeframe sent to the AIs
CANDLE_LOOKBACK_DAYS = 300       # Window requested from HL (the ~5000 bar cap is the real limit)
SNAPSHOT_MAX_BARS = 72           # Bars sent to the AIs (72 x 1h = 3 days). Cache holds ~5000 if we want more.
MIN_POSITION_VALUE_USD = 450_000  # People's positions below this size are filtered out of the snapshot
POSITION_SIZE_PERCENT = 95       # % of account value used as notional per trade
STARTING_BALANCE = 100.0         # Each fighter's starting stake ($100 for the first beta - Moon Dev)
ELIMINATION_DRAWDOWN_PCT = 50    # Lose 50% of starting stake = eliminated

MODEL_TIMEOUT = 180              # Seconds the model gets to answer (big prompt = more time)
AI_TEMPERATURE = 0.7
AI_MAX_TOKENS = 8192             # Room for reasoning before the final word (Kimi burns ~4k thinking - 4096 truncated it)

# 🥊 THE FIGHTERS - each model is mapped to its own Hyperliquid subaccount
# Model slugs verified live on openrouter.ai/api/v1/models by Moon Dev
BATTLE_BOTS = {
    "CLAUDE":   {"model": "anthropic/claude-opus-5",       "sub_acct_env": "ACCT1"},
    "OPENAI":   {"model": "openai/gpt-5.6-sol",            "sub_acct_env": "ACCT2"},
    "GEMINI":   {"model": "google/gemini-3.1-pro-preview", "sub_acct_env": "ACCT3"},
    "GROK":     {"model": "x-ai/grok-4.5",                 "sub_acct_env": "ACCT4"},
    "KIMI":     {"model": "moonshotai/kimi-k3",            "sub_acct_env": "ACCT5"},
    "DEEPSEEK": {"model": "deepseek/deepseek-v4-pro",      "sub_acct_env": "ACCT6"},
}

# ============================================================================
# 🧠 THE TWO PROMPTS (flat vs in-position)
# ============================================================================

FLAT_PROMPT = """You are a trader managing a {symbol} perpetuals account on Hyperliquid.

You currently have NO OPEN POSITION in {symbol}.

CRITICAL RULES:
1. Analyze the market data and decide: LONG, SHORT, or NOTHING
2. LONG = open a long position right now
3. SHORT = open a short position right now
4. NOTHING = stay flat this cycle, no trade
5. You may reason briefly, but the VERY LAST WORD of your response MUST be your decision: LONG, SHORT, or NOTHING"""

IN_POSITION_PROMPT = """You are a trader managing a {symbol} perpetuals account on Hyperliquid.

You currently HAVE AN OPEN {side} POSITION in {symbol} (details in the market data).

CRITICAL RULES:
1. Analyze the market data and decide: LONG, SHORT, CLOSE, or NOTHING
2. LONG = you want to be long ({long_meaning})
3. SHORT = you want to be short ({short_meaning})
4. CLOSE = close the position now and go flat
5. NOTHING = keep the current position exactly as is
6. You may reason briefly, but the VERY LAST WORD of your response MUST be your decision: LONG, SHORT, CLOSE, or NOTHING"""

# ============================================================================
# 📁 DATA / LOGGING - every decision stored forever, just like the site says
# (per-bot CSV files so six processes never fight over one file)
# ============================================================================

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CANDLE_CACHE = DATA_DIR / "candles_cache.json"
CANDLE_LOCK_PATH = "/tmp/moondev_ai_battles_candles.lock"

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
MOONDEV_BASE_URL = "https://api.moondev.com"
_session = requests.Session()
_meta_cache = None

# ============================================================================
# 🌙 MOON DEV API - bid/ask + account state (zero public HL reads)
# ============================================================================

def moondev_get(path, params=None, timeout=30):
    """GET from Moon Dev's API with auth + ONE retry on anything transient:
    rate limit (429), server blip (5xx), or a TIMEOUT/connection error -
    timeouts raise before any status code exists, so they need their own
    catch (learned live: a 30s read timeout cost all six a full cycle)."""
    params = dict(params or {})
    params["api_key"] = os.getenv("MOONDEV_API_KEY")
    headers = {"X-API-Key": os.getenv("MOONDEV_API_KEY")}
    for attempt in (1, 2):
        try:
            r = _session.get(f"{MOONDEV_BASE_URL}{path}", params=params, headers=headers, timeout=timeout)
            if r.status_code != 429 and r.status_code < 500:
                break
            reason = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                raise
            reason = type(e).__name__
        cprint(f"⚠️ Moon Dev API {reason} on {path} - waiting 5s and retrying once", "yellow")
        time.sleep(5)
    if r.status_code != 200:
        raise ValueError(f"Moon Dev API error {r.status_code} on {path}: {r.text[:200]}")
    return r.json()


def ask_bid(symbol):
    """Get ask and bid prices via Moon Dev's orderbook endpoint (verified tick-perfect vs HL)"""
    data = moondev_get(f"/api/orderbook/{symbol}", timeout=10)
    return float(data["best_ask"]), float(data["best_bid"])


def get_sz_decimals(symbol):
    """Get size decimals via Moon Dev's meta endpoint (cached once per session).

    Meta is static exchange info fetched ONCE per session, so if the Moon Dev
    meta endpoint hiccups we fall back to a single HL public meta call -
    one cached request can't rate-limit anything.
    """
    global _meta_cache
    if _meta_cache is None:
        try:
            _meta_cache = moondev_get("/api/meta", timeout=10).get("symbols", {})
            cprint(f"✓ Moon Dev: metadata loaded for {len(_meta_cache)} symbols", "green")
        except Exception as e:
            cprint(f"⚠️ Moon Dev meta endpoint failed ({e}) - one-time HL public meta fallback", "yellow")
            r = requests.post(HL_INFO_URL, headers={"Content-Type": "application/json"},
                              json={"type": "meta"}, timeout=10)
            _meta_cache = {s["name"]: {"sz_decimals": s["szDecimals"]} for s in r.json()["universe"]}
            cprint(f"✓ Moon Dev: metadata loaded for {len(_meta_cache)} symbols (HL fallback)", "green")
    if symbol not in _meta_cache:
        raise ValueError(f"Moon Dev: {symbol} not found in exchange meta!")
    return _meta_cache[symbol]["sz_decimals"]


def round_price(symbol, px):
    """Round price like Moon Dev's other HL bots (integer prices always valid)"""
    if symbol == "BTC":
        return round(px)
    return round(px, 1)


def _hl_public_state(vault_address):
    """Account state straight from the HL public API, shaped exactly like
    /api/account - the fallback when the Moon Dev API misreads an account."""
    p = _session.post(HL_INFO_URL, headers={"Content-Type": "application/json"},
                      json={"type": "clearinghouseState", "user": vault_address}, timeout=10).json()
    s = _session.post(HL_INFO_URL, headers={"Content-Type": "application/json"},
                      json={"type": "spotClearinghouseState", "user": vault_address}, timeout=10).json()
    return {"marginSummary": p["marginSummary"], "assetPositions": p.get("assetPositions", []),
            "spotBalances": s.get("balances", [])}


def get_user_state(vault_address):
    """Unified account state via Moon Dev's API - ONE call returns perps
    marginSummary + assetPositions + spotBalances. Zero HL account reads
    when the API is healthy.

    SAFETY: the account read is the ONE input that can falsely bench a
    fighter or missize a trade, so it self-heals to the source of truth:
    any Moon Dev API failure (502s seen live) OR an exactly-$0.00 read
    (stale-instance bug, also seen live: 200 OK, all zeros) -> re-read
    straight from HL public. If HL also says $0, the $0 is real."""
    try:
        data = moondev_get(f"/api/account/{vault_address}", timeout=10)
        if not isinstance(data, dict) or "marginSummary" not in data or "spotBalances" not in data:
            raise ValueError(f"missing marginSummary/spotBalances: {str(data)[:150]}")
        if _unified_equity(data) == 0:
            raise ValueError("read exactly $0.00 (stale-instance bug)")
        return data
    except Exception as e:
        cprint(f"⚠️ Moon Dev API account read failed for {vault_address[:10]}... - using HL public directly ({str(e)[:120]})", "yellow")
        return _hl_public_state(vault_address)


def _parse_position(user_state, symbol):
    """Pull our symbol's position out of a user_state. Returns None if flat."""
    for p in user_state["assetPositions"]:
        pos = p["position"]
        if pos["coin"] == symbol and float(pos["szi"]) != 0:
            szi = float(pos["szi"])
            return {
                "size": szi,
                "is_long": szi > 0,
                "entry_px": float(pos["entryPx"]),
                "pnl_perc": float(pos["returnOnEquity"]) * 100,
                "unrealized_pnl": float(pos["unrealizedPnl"]),
            }
    return None


def _unified_equity(user_state):
    """Equity = FREE spot USDC (total - hold) + perps accountValue.
    The six subaccounts are UNIFIED accounts (HL forces this on new
    subaccounts): idle margin sits on the SPOT side, and when a position is
    open the perps margin shows up as spot 'hold' AND as perps accountValue -
    verified live by Moon Dev with a real position, so free-spot + perps is
    the formula that never double counts."""
    perps_value = float(user_state["marginSummary"]["accountValue"])
    free_spot = sum(float(b["total"]) - float(b["hold"])
                    for b in user_state["spotBalances"] if b["coin"] == "USDC")
    return perps_value + free_spot


def get_fighter_state(vault_address, symbol):
    """ONE Moon Dev API call per fighter per cycle gets BOTH equity and position"""
    user_state = get_user_state(vault_address)
    return _unified_equity(user_state), _parse_position(user_state, symbol)


def get_account_value(vault_address):
    """Total unified-account equity for a subaccount"""
    return _unified_equity(get_user_state(vault_address))


# ============================================================================
# 📊 SHARED CANDLE CACHE - one Hyperliquid fetch per cycle across ALL SIX bots
# First bot into the new cycle fetches and writes the cache (under a file
# lock), the other five read the file. Fairness bonus: everyone sees the
# exact same dataset for the cycle.
# ============================================================================

def _cycle_bucket():
    """Which decision-cycle window are we in right now?"""
    return int(time.time() // (DECISION_INTERVAL_MINUTES * 60))


def _read_market_cache(bucket):
    """Read the shared cache if it's from the current cycle, else None"""
    if not CANDLE_CACHE.exists():
        return None
    with open(CANDLE_CACHE) as f:
        cache = json.load(f)
    if (cache.get("bucket") == bucket and cache.get("interval") == TIMEFRAME
            and cache.get("candles") and "positions" in cache and "liquidations" in cache):
        return cache
    return None


def _fetch_hl_candles():
    """Fetch the max candle dataset from Hyperliquid (~5000 bar cap)"""
    end_time = dt.datetime.utcnow()
    start_time = end_time - timedelta(days=CANDLE_LOOKBACK_DAYS)
    r = requests.post(HL_INFO_URL, headers={"Content-Type": "application/json"},
                      json={"type": "candleSnapshot",
                            "req": {"coin": SYMBOL, "interval": TIMEFRAME,
                                    "startTime": int(start_time.timestamp() * 1000),
                                    "endTime": int(end_time.timestamp() * 1000),
                                    "limit": 5000}},
                      timeout=20)
    candles = r.json()
    if not isinstance(candles, list) or not candles:
        raise ValueError(f"Moon Dev: no candle data from Hyperliquid! {str(candles)[:200]}")
    candles = sorted(candles, key=lambda c: c["t"])
    return candles


def _fetch_positions():
    """Fetch everybody's positions from the Moon Dev API (moondev.com/docs -
    People's Positions). Returns the block for our SYMBOL: top 50 longs +
    top 50 shorts with entry/liq prices, leverage, pnl + aggregate totals."""
    data = moondev_get("/api/positions/all.json", timeout=30)
    block = data.get("symbols", {}).get(SYMBOL)
    if not block:
        raise ValueError(f"Moon Dev: no {SYMBOL} positions in People's Positions response!")
    block["updated_at"] = data.get("updated_at")
    return block


def _fetch_liquidations():
    """Fetch past liquidation totals from the Moon Dev API - long/short split
    across binance, bybit, okx, hyperliquid for 6 trailing windows (5m..4h)."""
    data = moondev_get("/api/all_liquidations/totals.json", timeout=15)
    windows = data.get("windows")
    if not windows:
        raise ValueError(f"Moon Dev: no windows in liquidations response! {str(data)[:200]}")
    return {"windows": windows, "generated_at": data.get("generated_at")}


def get_shared_market_data():
    """Get this cycle's shared dataset (candles + people's positions) - from
    the cache if another fighter already fetched it, otherwise fetch and write.
    All six fighters see the IDENTICAL dataset every cycle."""
    bucket = _cycle_bucket()

    cache = _read_market_cache(bucket)
    if cache:
        cprint(f"📂 Moon Dev: shared market cache HIT ({len(cache['candles'])} bars + positions + liqs) - no fetch needed", "green")
        return cache["candles"], cache["positions"], cache["liquidations"]

    # Cache stale for this cycle - grab the fetch lock (blocks if another bot is fetching)
    lock_file = open(CANDLE_LOCK_PATH, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    try:
        # Re-check: another bot may have fetched while we waited on the lock
        cache = _read_market_cache(bucket)
        if cache:
            cprint(f"📂 Moon Dev: another fighter fetched while we waited - no fetch needed", "green")
            return cache["candles"], cache["positions"], cache["liquidations"]

        cprint(f"🌐 Moon Dev: fetching this cycle's candles (HL) + positions + liquidations (Moon Dev API)...", "cyan")
        candles = _fetch_hl_candles()
        positions = _fetch_positions()
        liquidations = _fetch_liquidations()

        # Atomic write so readers never see a half-written file
        tmp_path = CANDLE_CACHE.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump({"bucket": bucket, "interval": TIMEFRAME, "symbol": SYMBOL,
                       "fetched_at": dt.datetime.utcnow().isoformat(),
                       "candles": candles, "positions": positions, "liquidations": liquidations}, f)
        tmp_path.rename(CANDLE_CACHE)
        cprint(f"✅ Moon Dev: cached {len(candles)} bars + {len(positions.get('longs', []))} longs / "
               f"{len(positions.get('shorts', []))} shorts + 6 liq windows for all six fighters", "green")
        return candles, positions, liquidations
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _candles_to_df(candles):
    """Convert HL candle dicts to a DataFrame"""
    return pd.DataFrame([{
        "timestamp": dt.datetime.utcfromtimestamp(c["t"] / 1000),
        "open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]),
        "close": float(c["c"]), "volume": float(c["v"]),
    } for c in candles])


# ============================================================================
# 📐 INDICATORS - hand-rolled on purpose (Moon Dev)
# ============================================================================
# These were pandas_ta calls. Two problems for a decade-long benchmark:
#   1. pandas_ta is GONE from PyPI for python < 3.12 - a fresh server install
#      simply fails, and the arena never starts.
#   2. Worse and silent: pandas_ta routes to TA-Lib when TA-Lib happens to be
#      installed (the mac) and to its own pandas math when it isn't (a fresh
#      server). Those two disagree - RSI14 drifted 0.0063 on a live 72-bar
#      window. The AIs would have started seeing DIFFERENT numbers the day the
#      battle moved boxes, mid-season, with nothing in the logs to show it.
# So: no dependency, no branch, no drift. These match TA-Lib exactly (verified
# to 1e-13 against the live candle cache) - the season stays continuous.

def _sma(close, length):
    """Simple moving average - matches TA-Lib SMA"""
    return close.rolling(length).mean()


def _wilder(series, length):
    """Wilder's smoothing with TA-Lib's seed: the first average is a SIMPLE
    mean of the first `length` values, and every one after it is smoothed.
    (A plain ewm without that seed is the pandas_ta fallback - the 0.0063 gap.)"""
    if len(series) <= length:
        return pd.Series([float("nan")] * len(series), index=series.index)
    seeded = series.copy()
    seeded.iloc[:length] = float("nan")
    seeded.iloc[length] = series.iloc[1:length + 1].mean()
    return seeded.ewm(alpha=1 / length, adjust=False).mean()


def _rsi(close, length=14):
    """Wilder's RSI - matches TA-Lib RSI"""
    delta = close.diff()
    avg_gain = _wilder(delta.clip(lower=0), length)
    avg_loss = _wilder((-delta).clip(lower=0), length)
    total = avg_gain + avg_loss
    # A dead-flat window is 0/0. TA-Lib calls that 0.0; plain division gives NaN,
    # which would land in the AIs' prompt as "nan" and blank the dataset column.
    # BTC hourly should never print 14 identical closes - "should never" isn't
    # a reason to leave it undefined. Warm-up bars stay NaN (total is NaN there).
    return (100 * avg_gain / total).where(total != 0, 0.0)


def _positions_block(positions):
    """Format everybody's positions as raw CSV rows - data only, ZERO
    interpretation. The AIs draw their own conclusions.
    Only positions >= MIN_POSITION_VALUE_USD make the cut."""
    def rows(side_list):
        kept = [p for p in side_list if abs(float(p.get("value", 0))) >= MIN_POSITION_VALUE_USD]
        lines = ["value_usd,liq_price,distance_to_liq_pct"]
        for p in kept:
            lines.append(f"{round(float(p['value']))},{round(float(p['liq_price']))},{p['distance_pct']}")
        return len(kept), "\n".join(lines)

    n_longs, long_rows = rows(positions.get("longs", []))
    n_shorts, short_rows = rows(positions.get("shorts", []))

    return f"""PEOPLE'S POSITIONS: {SYMBOL}-PERP on Hyperliquid (positions >= ${MIN_POSITION_VALUE_USD:,.0f}, sorted closest to liquidation first, updated {positions.get('updated_at', 'n/a')})
Market totals: {positions.get('total_longs')} longs (${positions.get('total_long_value'):,.0f}) | {positions.get('total_shorts')} shorts (${positions.get('total_short_value'):,.0f})

LONGS ({n_longs} shown):
{long_rows}

SHORTS ({n_shorts} shown):
{short_rows}"""


def _liquidations_block(liq):
    """Format past liquidation totals as raw CSV - data only, ZERO
    interpretation. Explicit about the mechanics so the AIs read it right."""
    lines = ["window,long_liquidated_usd,long_count,short_liquidated_usd,short_count"]
    for name in ["5m", "15m", "1h", "2h", "3h", "4h"]:
        w = liq["windows"].get(name, {})
        lines.append(f"{name},{round(float(w.get('long_volume_usd', 0)))},{w.get('long_count', 0)},"
                     f"{round(float(w.get('short_volume_usd', 0)))},{w.get('short_count', 0)}")
    return f"""PAST LIQUIDATIONS: all crypto markets combined, across binance + bybit + okx + hyperliquid (generated {liq.get('generated_at', 'n/a')})
How to read this: each row is a TRAILING window ending NOW - the 1h row = everything liquidated in the last 60 minutes. Windows overlap (the 4h window contains the 1h window). long_liquidated_usd = long positions that got liquidated; short_liquidated_usd = short positions that got liquidated.
{chr(10).join(lines)}"""


def build_market_snapshot():
    """Build the market data block every fighter receives this cycle.

    Candles + people's positions + past liquidations are identical for all
    six (shared cache). Bid/ask is pulled live per bot as the most recent price.
    """
    candles, positions, liquidations = get_shared_market_data()
    df = _candles_to_df(candles).tail(SNAPSHOT_MAX_BARS)

    # Header stats so the AIs get the current technical picture up front
    sma20 = _sma(df["close"], 20).iloc[-1]
    sma40 = _sma(df["close"], 40).iloc[-1]
    rsi14 = _rsi(df["close"], 14).iloc[-1]

    ask, bid = ask_bid(SYMBOL)
    last_price = (ask + bid) / 2
    change_24h = (df["close"].iloc[-1] / df["close"].iloc[-25] - 1) * 100 if len(df) > 25 else 0.0

    # Compact CSV rows - no open column (each bar opens at the previous bar's
    # close in a 24/7 market, so it's a duplicated number - Moon Dev)
    rows = df[["timestamp", "high", "low", "close", "volume"]].copy()
    rows["timestamp"] = rows["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
    rows["volume"] = rows["volume"].round(2)
    csv_block = rows.to_csv(index=False)

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    snapshot = f"""MARKET DATA
CURRENT TIME: {now_utc} UTC. Everything below is timestamped so you know exactly where you are in time.
LIVE PRICE RIGHT NOW: bid ${bid:,.1f} / ask ${ask:,.1f} (this is the actual tradeable price at this moment)
24h change: {change_24h:+.2f}% | RSI(14): {rsi14:.1f} | SMA20: ${sma20:,.2f} | SMA40: ${sma40:,.2f}

Candle dataset: Hyperliquid {SYMBOL}-PERP {TIMEFRAME} candles, {len(df)} bars, CSV.
Rows are in chronological order: OLDEST at the top ({df['timestamp'].iloc[0]} UTC) -> NEWEST at the bottom ({df['timestamp'].iloc[-1]} UTC). The LAST row is the current/most recent candle.
Note: no open column - each bar opens at the previous bar's close.
{csv_block}
{_positions_block(positions)}

{_liquidations_block(liquidations)}"""
    cprint(f"📊 Snapshot ready | {len(df)} bars + people's positions | {SYMBOL} bid ${bid:,.1f} / ask ${ask:,.1f} ({change_24h:+.2f}% 24h)", "cyan")
    # Market stats ride along for the research dataset - the exact market
    # state every fighter saw at decision time (Moon Dev's decade dataset).
    # Everything the AIs get, one row: indicators, market position totals,
    # all six liquidation windows, and the AT-RISK LADDER - total position
    # value that liquidates within each %-move from the current price
    # (aggregated totals of the same >= $450k positions the AIs see raw).
    stats = {"bid": bid, "ask": ask, "rsi14": round(rsi14, 2), "sma20": round(sma20, 2),
             "sma40": round(sma40, 2), "change_24h_pct": round(change_24h, 2),
             "total_longs_count": positions.get("total_longs"),
             "total_longs_usd": round(float(positions.get("total_long_value", 0))),
             "total_shorts_count": positions.get("total_shorts"),
             "total_shorts_usd": round(float(positions.get("total_short_value", 0)))}
    for w in ["5m", "15m", "1h", "2h", "3h", "4h"]:
        win = liquidations["windows"].get(w, {})
        stats[f"liq_long_usd_{w}"] = round(float(win.get("long_volume_usd", 0)))
        stats[f"liq_short_usd_{w}"] = round(float(win.get("short_volume_usd", 0)))
    for side in ("longs", "shorts"):
        kept = [p for p in positions.get(side, []) if abs(float(p.get("value", 0))) >= MIN_POSITION_VALUE_USD]
        for t in (0.5, 1, 2, 3, 5):
            total = sum(abs(float(p["value"])) for p in kept if float(p["distance_pct"]) <= t)
            stats[f"{side}_at_risk_{str(t).replace('.', '_')}pct_usd"] = round(total)
    return snapshot, last_price, stats


# ============================================================================
# 🤖 MODEL QUERYING - one model per process, via OpenRouter
# ============================================================================

def ask_model(model_id, system_prompt, user_content):
    """Query one model via OpenRouter"""
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                 "Content-Type": "application/json"},
        json={"model": model_id,
              "messages": [{"role": "system", "content": system_prompt},
                           {"role": "user", "content": user_content}],
              "temperature": AI_TEMPERATURE,
              "max_tokens": AI_MAX_TOKENS},
        timeout=MODEL_TIMEOUT)
    r.raise_for_status()
    # Reasoning models can burn the whole token budget thinking and return
    # content=null - never hand None downstream, an empty answer = NOTHING
    return r.json()["choices"][0]["message"]["content"] or ""


def parse_decision(text, in_position):
    """Pull the decision out of a model response - last valid keyword wins"""
    cleaned = text.upper().replace("DO NOTHING", "NOTHING")
    valid = {"LONG", "SHORT", "CLOSE", "NOTHING"} if in_position else {"LONG", "SHORT", "NOTHING"}
    words = [w for w in re.findall(r"\b(LONG|SHORT|CLOSE|NOTHING)\b", cleaned) if w in valid]
    return words[-1] if words else None


def get_bot_decision(name, model_id, position, snapshot):
    """Build the right prompt (flat vs in-position) and ask the model.
    NO account value in the prompt - the market doesn't care about your
    balance, and knowing it only invites loss-aversion / revenge-trading
    bias. Every fighter sees identical inputs except its own position."""
    if position is None:
        system_prompt = FLAT_PROMPT.format(symbol=SYMBOL)
        position_text = "Current position: FLAT (no open position)"
    else:
        side = "LONG" if position["is_long"] else "SHORT"
        long_meaning = "keep holding your long" if position["is_long"] else "flip: close the short and open a long"
        short_meaning = "flip: close the long and open a short" if position["is_long"] else "keep holding your short"
        system_prompt = IN_POSITION_PROMPT.format(symbol=SYMBOL, side=side,
                                                  long_meaning=long_meaning, short_meaning=short_meaning)
        position_text = (f"Current position: {side} {abs(position['size'])} {SYMBOL} | "
                         f"Entry: ${position['entry_px']:,.2f} | "
                         f"Unrealized PnL: ${position['unrealized_pnl']:+,.2f} ({position['pnl_perc']:+.2f}%)")

    user_content = f"{snapshot}\n{position_text}\n\nWhat is your decision?"

    try:
        raw = ask_model(model_id, system_prompt, user_content)
    except Exception as e:
        # A failed model call must not kill the process mid-season
        cprint(f"❌ {name} model call failed: {e}", "red")
        return "NOTHING", f"MODEL ERROR: {e}"

    decision = parse_decision(raw, in_position=position is not None)
    if decision is None:
        cprint(f"⚠️ {name} gave no valid decision, treating as NOTHING. Raw: {raw[:200]}", "yellow")
        decision = "NOTHING"
    return decision, raw


# ============================================================================
# ⚔️ ORDER EXECUTION - the ONLY signed calls, straight to Hyperliquid
# ============================================================================

def market_open(exchange, symbol, is_buy, usd_size):
    """Open a position with an aggressive IOC order (Moon Dev's market order style)"""
    ask, bid = ask_bid(symbol)
    px = round_price(symbol, ask * 1.001 if is_buy else bid * 0.999)
    sz = round(usd_size / px, get_sz_decimals(symbol))

    if sz * px < 10:
        raise ValueError(f"Moon Dev: order value ${sz * px:.2f} below Hyperliquid's $10 minimum!")

    side = "LONG" if is_buy else "SHORT"
    cprint(f"   🛒 Opening {side}: {sz} {symbol} @ ${px} (${sz * px:,.2f} notional)", "green" if is_buy else "red")
    result = exchange.order(symbol, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=False)
    print(f"   📬 Order result: {json.dumps(result)[:300]}")
    return result


def close_position(exchange, symbol, position):
    """Close the open position with a reduce-only IOC order (Moon Dev's kill
    switch). Takes this cycle's position - no extra account re-fetch."""
    ask, bid = ask_bid(symbol)
    px = round_price(symbol, bid * 0.999 if position["is_long"] else ask * 1.001)
    sz = abs(position["size"])

    cprint(f"   🔪 Closing {'LONG' if position['is_long'] else 'SHORT'}: {sz} {symbol} @ ${px} "
           f"(PnL: {position['pnl_perc']:+.2f}%)", "yellow")
    result = exchange.order(symbol, not position["is_long"], sz, px,
                            {"limit": {"tif": "Ioc"}}, reduce_only=True)
    print(f"   📬 Order result: {json.dumps(result)[:300]}")
    return result


def execute_decision(exchange, vault, decision, position, account_value):
    """Execute this fighter's decision on its own subaccount. Returns action taken.
    Uses this cycle's account_value for sizing - no re-fetch. Only a FLIP
    re-fetches equity, because the close just realized the PnL and changed
    what 95% of the account means."""

    def notional(value):
        # Notional = equity * size% * leverage (at 1x this is just size% of equity)
        return value * POSITION_SIZE_PERCENT / 100 * LEVERAGE

    if position is None:
        if decision == "LONG":
            market_open(exchange, SYMBOL, True, notional(account_value))
            return "OPENED_LONG"
        if decision == "SHORT":
            market_open(exchange, SYMBOL, False, notional(account_value))
            return "OPENED_SHORT"
        return "STAYED_FLAT"

    # In a position
    if decision == "NOTHING":
        return "HELD_LONG" if position["is_long"] else "HELD_SHORT"
    if decision == "CLOSE":
        close_position(exchange, SYMBOL, position)
        return "CLOSED"
    if decision == "LONG":
        if position["is_long"]:
            return "HELD_LONG"
        close_position(exchange, SYMBOL, position)
        market_open(exchange, SYMBOL, True, notional(get_account_value(vault)))
        return "FLIPPED_TO_LONG"
    if decision == "SHORT":
        if not position["is_long"]:
            return "HELD_SHORT"
        close_position(exchange, SYMBOL, position)
        market_open(exchange, SYMBOL, False, notional(get_account_value(vault)))
        return "FLIPPED_TO_SHORT"
    return "NO_ACTION"


# ============================================================================
# 📝 RECORD KEEPING + LEADERBOARD
# ============================================================================

def append_csv(path, row):
    """Append one row to a CSV, writing the header on first use"""
    df = pd.DataFrame([row])
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def push_decisions(rows):
    """Push this cycle's decisions + FULL raw model responses to the Moon Dev
    API server (outbound only - the battle server never opens an inbound port).
    Feeds the decisions feed on moondev.com/ai. Optional: leave BATTLE_PUSH_URL
    blank in .env to skip (dry runs). A failed push never hurts the battle -
    the CSVs in data/ are the source of truth."""
    url = os.getenv("BATTLE_PUSH_URL")
    if not url:
        return
    try:
        r = _session.post(url,
                          json={"symbol": SYMBOL,
                                "interval_minutes": DECISION_INTERVAL_MINUTES,
                                "decisions": rows},
                          headers={"X-Battle-Key": os.getenv("BATTLE_PUSH_KEY", "")},
                          timeout=15)
        if r.status_code == 200:
            cprint(f"📤 Moon Dev: pushed {len(rows)} decision(s) to the API server", "green")
        else:
            cprint(f"⚠️ Moon Dev: decision push failed {r.status_code}: {r.text[:200]}", "yellow")
    except Exception as e:
        cprint(f"⚠️ Moon Dev: decision push error (battle continues): {e}", "yellow")


def print_leaderboard(values):
    """The whole point: who's winning? Takes this cycle's already-fetched
    values={name: account_value} - zero extra API calls."""
    standings = []
    for name, cfg in BATTLE_BOTS.items():
        if name not in values:
            continue
        value = values[name]
        roi = (value / STARTING_BALANCE - 1) * 100
        standings.append({"name": name, "model": cfg["model"], "value": value, "roi": roi})

    standings.sort(key=lambda s: s["value"], reverse=True)
    cprint("\n" + "=" * 62, "magenta")
    cprint("🏆 MOON DEV'S AI TRADING BATTLES - LEADERBOARD 🏆", "magenta", attrs=["bold"])
    cprint("=" * 62, "magenta")
    for i, s in enumerate(standings, 1):
        color = "green" if s["roi"] >= 0 else "red"
        eliminated = " 💀 ELIMINATED" if s["value"] <= STARTING_BALANCE * (1 - ELIMINATION_DRAWDOWN_PCT / 100) else ""
        cprint(f" #{i} {s['name']:<9} ${s['value']:>9,.2f}  {s['roi']:+7.2f}%  ({s['model']}){eliminated}", color)
    cprint("=" * 62 + "\n", "magenta")


# ============================================================================
# ⏰ CYCLE TIMER + FIGHT COUNTDOWN (optimize for fun - Moon Dev)
# ============================================================================

COUNTDOWN_HYPE = [
    "🥊 six AIs. one market. no mercy.",
    "🧠 CLAUDE is doing breathing exercises...",
    "🤖 OPENAI is reading the tape...",
    "🔮 GEMINI is counting liquidations...",
    "⚡ GROK is talking trash to the orderbook...",
    "🌙 KIMI is thinking... still thinking...",
    "🐋 DEEPSEEK is watching the whales...",
    "📼 every decision. public. forever.",
    "💀 lose 50% and it's the Hall of Blowups",
    "🔔 the bell rings at :13 - no algo herds allowed",
    "📊 same data. same rules. may the best model win.",
    "🌙 Moon Dev's AI Trading Battles - moondev.com/ai",
]
_COUNTDOWN_COLORS = ["cyan", "magenta", "yellow", "green"]


def next_cycle_ts(now=None):
    """Unix timestamp of the NEXT scheduled decision (live = :13 past every
    hour, never the round hour). Deterministic across restarts - a reboot at
    10:47 still fires next at 11:13. Shared by the countdown AND the
    heartbeat, so the watcher and the arena always agree - Moon Dev"""
    interval_s = DECISION_INTERVAL_MINUTES * 60
    offset_s = (CYCLE_OFFSET_MINUTES * 60) % interval_s
    now = time.time() if now is None else now
    return (int((now - offset_s) // interval_s) + 1) * interval_s + offset_s


def sleep_until_next_cycle():
    """Sleep to the next scheduled decision time (live = :13 past every hour,
    never the round hour) with a LIVE ticking countdown - MM:SS, a filling
    fight bar, and rotating hype lines. Deterministic across restarts - a
    reboot at 10:47 still fires next at 11:13."""
    interval_s = DECISION_INTERVAL_MINUTES * 60
    next_wake = next_cycle_ts()
    wake = dt.datetime.fromtimestamp(next_wake)
    cprint(f"\n🔔 NEXT BATTLE: {wake.strftime('%H:%M:%S')} local", "magenta", attrs=["bold"])

    tick = 0
    while True:
        remaining = next_wake - time.time()
        if remaining <= 0:
            break
        mins, secs = divmod(int(remaining), 60)
        frac = 1 - (remaining / interval_s)
        bar = "█" * int(18 * frac) + "░" * (18 - int(18 * frac))
        hype = COUNTDOWN_HYPE[(tick // 6) % len(COUNTDOWN_HYPE)]
        color = _COUNTDOWN_COLORS[(tick // 6) % len(_COUNTDOWN_COLORS)]
        pulse = "⚔️ " if tick % 2 == 0 else "🥊 "
        line = f"  {pulse} {mins:02d}:{secs:02d} |{bar}| {hype}"
        sys.stdout.write("\r" + colored(f"{line:<75}", color))
        sys.stdout.flush()
        time.sleep(min(1, max(remaining, 0.05)))
        tick += 1
    sys.stdout.write("\r" + " " * 78 + "\r")
    cprint("🔔🔔🔔 DING DING DING - BATTLE TIME! 🔔🔔🔔", "red", attrs=["bold"])
