#!/usr/bin/env python3
"""
🛫 MOON DEV'S PREFLIGHT CHECK 🛫
Run this BEFORE going live - proves every wire is connected with zero fill risk:
  1. .env        - every key present (values never printed)
  2. Moon Dev API - account value for all six subaccounts (did the money land?)
  3. Hyperliquid  - candle fetch works (the one public HL read per cycle)
  4. Orders       - place a bid 30% BELOW market on EACH subaccount, then
                    cancel it. Proves the private key signs for every vault
                    without any chance of a fill.
  5. OpenRouter   - tiny ping to all six battle models (are the slugs alive?)

Run: python preflight.py

Built with love by Moon Dev 🌙
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor

import eth_account
from termcolor import cprint
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from battle_core import (BATTLE_BOTS, SYMBOL, DECISION_INTERVAL_MINUTES, ask_bid,
                         get_account_value, get_sz_decimals, round_price,
                         _fetch_hl_candles, ask_model)

load_dotenv()

FAR_BELOW_PCT = 30    # bid 30% under the market - never fills
TEST_ORDER_USD = 15   # small, but above Hyperliquid's $10 minimum at the bid price

failures = []


def result(ok, label, detail):
    if ok:
        cprint(f"   ✅ {label}: {detail}", "green")
    else:
        cprint(f"   ❌ {label}: {detail}", "red")
        failures.append(f"{label}: {detail}")


def main():
    cprint("\n🛫 MOON DEV'S PREFLIGHT CHECK - AI TRADING BATTLES 🛫\n", "magenta", attrs=["bold"])

    # 1️⃣ env vars
    cprint("1️⃣ .env keys", "cyan", attrs=["bold"])
    needed = ["HYPERLIQUID_PRIVATE_KEY", "MOONDEV_API_KEY", "OPENROUTER_API_KEY"] + \
             [cfg["sub_acct_env"] for cfg in BATTLE_BOTS.values()]
    for var in needed:
        result(bool(os.getenv(var)), var, "set" if os.getenv(var) else "MISSING from .env")
    if failures:
        cprint("\n🛑 Fix the missing keys first - skipping the live checks.", "red", attrs=["bold"])
        return

    # 2️⃣ funding check via Moon Dev API
    cprint("\n2️⃣ Subaccount funding (unified equity: free spot USDC + perps)", "cyan", attrs=["bold"])
    for name, cfg in BATTLE_BOTS.items():
        vault = os.getenv(cfg["sub_acct_env"])
        try:
            value = get_account_value(vault)
            result(value >= TEST_ORDER_USD, f"{name} ({cfg['sub_acct_env']})",
                   f"${value:,.2f}" + ("" if value >= TEST_ORDER_USD else " - not enough to even place the test bid!"))
        except Exception as e:
            result(False, f"{name} ({cfg['sub_acct_env']})", f"account read failed: {e}")

    # 3️⃣ candle fetch
    cprint("\n3️⃣ Hyperliquid candles", "cyan", attrs=["bold"])
    try:
        candles = _fetch_hl_candles()
        result(True, "candleSnapshot", f"{len(candles)} bars of {SYMBOL} fetched")
    except Exception as e:
        result(False, "candleSnapshot", str(e))

    # 4️⃣ far-away bid + cancel on every subaccount (proves signing per vault)
    cprint(f"\n4️⃣ Order signing: bid {FAR_BELOW_PCT}% below market on each subaccount, then cancel", "cyan", attrs=["bold"])
    account = eth_account.Account.from_key(os.getenv("HYPERLIQUID_PRIVATE_KEY"))
    _, bid = ask_bid(SYMBOL)
    px = round_price(SYMBOL, bid * (1 - FAR_BELOW_PCT / 100))
    sz = round(TEST_ORDER_USD / px, get_sz_decimals(SYMBOL))
    cprint(f"   (market bid ${bid:,.1f} -> test bid ${px:,} for {sz} {SYMBOL} ≈ ${sz * px:,.2f})", "white")
    for name, cfg in BATTLE_BOTS.items():
        vault = os.getenv(cfg["sub_acct_env"])
        try:
            exchange = Exchange(account, constants.MAINNET_API_URL, vault_address=vault)
            order = exchange.order(SYMBOL, True, sz, px, {"limit": {"tif": "Gtc"}}, reduce_only=False)
            status = order["response"]["data"]["statuses"][0]
            if "resting" not in status:
                result(False, name, f"order rejected: {status}")
                continue
            oid = status["resting"]["oid"]
            cancel = exchange.cancel(SYMBOL, oid)
            cancelled = cancel.get("status") == "ok"
            result(cancelled, name, f"bid placed (oid {oid}) and cancelled"
                   if cancelled else f"bid placed but CANCEL FAILED: {cancel} - go cancel oid {oid} manually!")
        except Exception as e:
            result(False, name, f"order failed: {e}")

    # 5️⃣ openrouter ping, all six models in parallel
    cprint("\n5️⃣ OpenRouter: pinging all six battle models", "cyan", attrs=["bold"])
    def ping(item):
        name, cfg = item
        try:
            reply = ask_model(cfg["model"], "You are a connectivity test.", "Reply with exactly one word: PONG")
            return name, cfg["model"], True, reply.strip()[-40:]
        except Exception as e:
            return name, cfg["model"], False, str(e)[:120]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, model, ok, detail in pool.map(ping, BATTLE_BOTS.items()):
            result(ok, f"{name} ({model})", detail)

    # 6️⃣ decision push endpoint - probe uses an INVALID bot name on purpose:
    # 400 back = our key authenticated AND validation fired, zero rows stored
    cprint("\n6️⃣ Decision push -> Moon Dev API server", "cyan", attrs=["bold"])
    url = os.getenv("BATTLE_PUSH_URL")
    if not url:
        cprint("   ⚪ BATTLE_PUSH_URL blank - push disabled, skipping (fine for dry runs)", "yellow")
    else:
        probe = {"symbol": SYMBOL, "interval_minutes": DECISION_INTERVAL_MINUTES,
                 "decisions": [{"timestamp": "2020-01-01T00:00:00", "bot": "PREFLIGHT_PROBE",
                                "model": "probe", "symbol": SYMBOL, "position_before": "FLAT",
                                "decision": "LONG", "action": "PROBE", "price": 1,
                                "account_value": 1, "roi_pct": 0, "raw_response": "preflight probe"}]}
        try:
            r = requests.post(url, json=probe,
                              headers={"X-Battle-Key": os.getenv("BATTLE_PUSH_KEY", "")}, timeout=15)
            result(r.status_code == 400, "push endpoint",
                   "auth OK + validation OK, nothing stored" if r.status_code == 400
                   else f"unexpected {r.status_code}: {r.text[:120]} (401 = key mismatch between servers)")
        except Exception as e:
            result(False, "push endpoint", f"unreachable: {e}")

    # 🏁 verdict
    cprint("\n" + "=" * 62, "magenta")
    if failures:
        cprint(f"🛑 NOT READY - {len(failures)} problem(s):", "red", attrs=["bold"])
        for f in failures:
            cprint(f"   - {f}", "red")
    else:
        cprint("🏁 ALL CHECKS PASSED - Moon Dev, this bird is ready to fly! 🚀", "green", attrs=["bold"])
    cprint("=" * 62 + "\n", "magenta")


if __name__ == "__main__":
    main()
