#!/usr/bin/env python3
"""
🥊 MOON DEV'S AI TRADING BATTLES - ONE-SCREEN ARENA 🥊
moondev.com/ai | all six fighters in ONE process on ONE screen.

Run: python run_battle.py

WHY ONE SCREEN (Moon Dev's data dawg architecture):
  1. Fetch the market snapshot ONCE per cycle - no cache races, and all six
     fighters see the IDENTICAL dataset down to the same bid/ask tick.
  2. Ask all six models IN PARALLEL (threads) - the slow part (up to
     MODEL_TIMEOUT each) takes as long as the slowest model, not the sum.
  3. Execute orders SEQUENTIALLY - one private key signs all six
     subaccounts, so serializing the signed calls avoids Hyperliquid
     nonce collisions that six parallel processes could hit.
  4. One screen to restart when something needs a kick.

All battle logic lives in battle_core.py - this file is just the conductor.

Built with love by Moon Dev 🌙
"""

import os
import sys
import json
import time
import fcntl
import shutil
import socket
import requests
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

import eth_account
from termcolor import cprint
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from battle_core import (
    BATTLE_BOTS, SYMBOL, LEVERAGE, PLACE_TRADES, STARTING_BALANCE,
    ELIMINATION_DRAWDOWN_PCT, DECISION_INTERVAL_MINUTES, POSITION_SIZE_PERCENT,
    DATA_DIR, append_csv, build_market_snapshot, get_fighter_state,
    get_account_value, get_bot_decision, execute_decision, print_leaderboard,
    push_decisions, sleep_until_next_cycle, next_cycle_ts,
)

load_dotenv()

# ============================================================================
# 💓 HEARTBEAT - PROOF OF LIFE (Moon Dev)
# ============================================================================
# The arena runs SILENT on the server (afplay is a mac thing) - ALL the sounds
# and announcements live in watch_battle.py now. This box only publishes what
# it's doing, every HEARTBEAT_WRITE_SECONDS:
#   1. data/heartbeat.json  - local proof of life (atomic write)
#   2. POST -> BATTLE_HEARTBEAT_URL on the Moon Dev API - OUTBOUND ONLY, so the
#      battle box never opens an inbound port and never exposes its IP
# watch_battle.py on the mac reads either one and makes all the noise. If this
# thread ever stops, the watcher goes stale and screams - which is the point.
# THIS BATTLE NEVER MISSES A ROUND.

HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
HEARTBEAT_WRITE_SECONDS = 5      # local file refresh
HEARTBEAT_PUSH_SECONDS = 20      # outbound POST to the Moon Dev API

_hb_lock = threading.Lock()
_hb = {
    "phase": "BOOT",             # BOOT/SNAPSHOT/ACCOUNTS/THINKING/SLEEPING/ERROR
    "host": socket.gethostname(),
    "pid": os.getpid(),
    "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "symbol": SYMBOL,
    "interval_minutes": DECISION_INTERVAL_MINUTES,
    "live_trading": PLACE_TRADES,
    "cycles_done": 0,
    "last_cycle_at": None,
    "last_error": None,
    "last_error_at": None,
    "fighters": {},              # name -> {state, decision, action, value, ...}
}


def hb_set(**kw):
    """Update top-level heartbeat fields"""
    with _hb_lock:
        _hb.update(kw)


def hb_error(msg):
    """Record the last error so the watcher can show it in red - this is how
    Moon Dev finds out about a broken cycle WITHOUT reading the screen"""
    with _hb_lock:
        _hb["last_error"] = str(msg)[:400]
        _hb["last_error_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def hb_fighter(name, **kw):
    """Update one fighter's live state (THINKING -> ANSWERED -> DONE)"""
    with _hb_lock:
        _hb.setdefault("fighters", {}).setdefault(name, {}).update(kw)


def hb_new_cycle(names, cycle_iso):
    """Fresh cycle - every fighter starts from a clean slate so the watcher
    can't show last hour's decisions as if they were live"""
    with _hb_lock:
        _hb["fighters"] = {n: {"state": "WAITING"} for n in names}
        _hb["cycle_started_at"] = cycle_iso


def hb_snapshot():
    """The blob written to disk and pushed to the API"""
    with _hb_lock:
        blob = json.loads(json.dumps(_hb))
    blob["ts"] = time.time()
    blob["ts_iso"] = dt.datetime.now(dt.timezone.utc).isoformat()
    blob["next_cycle_at"] = next_cycle_ts()
    blob["uptime_s"] = round(time.time() - _BOOT_TS)
    # 💾 disk headroom - a full disk is what killed a round once. Never again.
    usage = shutil.disk_usage(DATA_DIR)
    blob["disk_free_gb"] = round(usage.free / 1e9, 2)
    blob["disk_used_pct"] = round(100 * usage.used / usage.total, 1)
    return blob


def _heartbeat_loop():
    """Daemon thread: write local, push outbound. Never touches the battle."""
    url = os.getenv("BATTLE_HEARTBEAT_URL")
    key = os.getenv("BATTLE_PUSH_KEY", "")
    tmp = HEARTBEAT_FILE.with_suffix(".tmp")
    last_push = 0
    while True:
        try:
            blob = hb_snapshot()
            tmp.write_text(json.dumps(blob))
            tmp.replace(HEARTBEAT_FILE)          # atomic - watcher never reads half a file
            if url and time.time() - last_push >= HEARTBEAT_PUSH_SECONDS:
                requests.post(url, json=blob, headers={"X-Battle-Key": key}, timeout=10)
                last_push = time.time()
        except Exception as e:
            # A heartbeat hiccup must NEVER take down the arena - it just goes
            # quiet, and a quiet heartbeat is exactly what the watcher alarms on
            cprint(f"⚠️ Moon Dev: heartbeat error (battle continues): {e}", "yellow")
        time.sleep(HEARTBEAT_WRITE_SECONDS)


_BOOT_TS = time.time()


def arena_cycle(fighters):
    cycle_time = dt.datetime.utcnow()
    hb_new_cycle(fighters.keys(), cycle_time.isoformat())
    hb_set(phase="SNAPSHOT")
    cprint(f"\n{'=' * 62}", "cyan")
    cprint(f"🥊 MOON DEV'S ARENA CYCLE - {cycle_time.strftime('%Y-%m-%d %H:%M:%S')} UTC 🥊", "cyan", attrs=["bold"])
    cprint("=" * 62, "cyan")

    # ONE snapshot for everybody - identical candles, positions, liqs AND bid/ask
    snapshot, price, mkt = build_market_snapshot()

    # Round up the fighters still standing - ONE /api/account call per fighter
    # gets value AND position together (was 2-3 hits each)
    hb_set(phase="ACCOUNTS")
    live, cycle_values = [], {}
    for name, f in fighters.items():
        value, position = get_fighter_state(f["vault"], SYMBOL)
        cycle_values[name] = value
        roi = (value / STARTING_BALANCE - 1) * 100
        append_csv(DATA_DIR / f"equity_{name}.csv",
                   {"timestamp": cycle_time.isoformat(), "bot": name, "model": f["model"],
                    "account_value": round(value, 2), "roi_pct": round(roi, 2)})

        if value <= STARTING_BALANCE * (1 - ELIMINATION_DRAWDOWN_PCT / 100):
            cprint(f"💀 {name} is below the {ELIMINATION_DRAWDOWN_PCT}% drawdown line (${value:,.2f}) - benched", "red")
            hb_fighter(name, state="BENCHED", model=f["model"], value=round(value, 2),
                       roi_pct=round(roi, 2))
            continue

        pos_str = "FLAT" if position is None else ("LONG" if position["is_long"] else "SHORT")
        hb_fighter(name, state="THINKING", model=f["model"], value=round(value, 2),
                   roi_pct=round(roi, 2), position_before=pos_str,
                   decision=None, action=None, answer_s=None)
        live.append({"name": name, **f, "value": value, "position": position, "pos_str": pos_str})

    if not live:
        cprint("💀 Every fighter is benched - nothing to do this cycle", "red")
        hb_set(phase="ALL_BENCHED", last_cycle_at=cycle_time.isoformat())
        print_leaderboard(cycle_values)
        return

    # 🧠 All six models think IN PARALLEL, and each fighter's trade fires the
    # MOMENT its model answers (as_completed) - a fast answer never waits on a
    # slow thinker. Execution itself stays on the MAIN thread, one order at a
    # time: one signer key across six subaccounts = zero nonce collisions.
    cprint(f"\n🧠 Moon Dev: asking all {len(live)} models in parallel - trades fire as answers land...", "cyan")
    hb_set(phase="THINKING")
    push_rows = []
    ask_start = time.time()
    with ThreadPoolExecutor(max_workers=len(live)) as pool:
        futures = {pool.submit(get_bot_decision, b["name"], b["model"],
                               b["position"], snapshot): b
                   for b in live}
        for future in as_completed(futures):
            b = futures[future]
            b["decision"], b["raw"] = future.result()
            b["answer_s"] = round(time.time() - ask_start, 1)
            # 💓 the watcher plays this fighter's stinger the moment it sees this
            hb_fighter(b["name"], state="ANSWERED", decision=b["decision"],
                       answer_s=b["answer_s"])
            cprint(f"\n{'-' * 62}", "cyan")
            cprint(f"💬 {b['name']} ({b['model']}) answered in {time.time() - ask_start:.1f}s:", "cyan", attrs=["bold"])
            print(b["raw"])
            cprint(f"🤖 {b['name']:<9} [{b['pos_str']}] decision: {b['decision']}", "white", attrs=["bold"])

            if PLACE_TRADES:
                try:
                    action = execute_decision(b["exchange"], b["vault"], b["decision"],
                                              b["position"], b["value"])
                except Exception as e:
                    # One bad order must not end the season for the other five
                    cprint(f"❌ {b['name']} execution error: {e}", "red")
                    action = f"EXECUTION_ERROR: {e}"
                    hb_error(f"{b['name']} execution: {e}")
            else:
                action = "PAPER_ONLY"
            cprint(f"✅ {b['name']:<9} action: {action}", "green")
            hb_fighter(b["name"], state="DONE", action=action)

            row = {
                "timestamp": cycle_time.isoformat(),
                "bot": b["name"],
                "model": b["model"],
                "symbol": SYMBOL,
                "position_before": b["pos_str"],
                "decision": b["decision"],
                "action": action,
                "price": round(price, 2),
                "account_value": round(b["value"], 2),
                "roi_pct": round((b["value"] / STARTING_BALANCE - 1) * 100, 2),
            }
            append_csv(DATA_DIR / f"decisions_{b['name']}.csv",
                       {**row, "raw_response": b["raw"][:500].replace("\n", " ")})
            push_rows.append({**row, "raw_response": b["raw"]})

            # 📚 THE DECADE DATASET - one master append-only file, one row per
            # AI decision with the exact market state it saw + its FULL
            # response. Cadence-proof: interval_minutes rides in every row, so
            # a future 5-min season just keeps appending. This file is the
            # long game - Moon Dev
            append_csv(DATA_DIR / "ai_decisions_dataset.csv", {
                "timestamp": cycle_time.isoformat(),
                "interval_minutes": DECISION_INTERVAL_MINUTES,
                "bot": b["name"],
                "model": b["model"],
                "symbol": SYMBOL,
                "mid_price": round(price, 2),
                **mkt,
                "position_before": b["pos_str"],
                "decision": b["decision"],
                "action": action,
                "account_value": round(b["value"], 2),
                "roi_pct": round((b["value"] / STARTING_BALANCE - 1) * 100, 2),
                "answer_seconds": b["answer_s"],
                "live_trading": PLACE_TRADES,
                "raw_response": b["raw"].replace("\r", "").replace("\n", "\\n"),
            })

    # 📤 One outbound POST per cycle -> moondev.com/ai decisions feed.
    # LIVE ONLY: dry runs stay off the public record - it's real trades forever
    if PLACE_TRADES:
        hb_set(phase="PUSHING")
        push_decisions(push_rows)
    else:
        cprint("📤 Dry run - decisions NOT pushed to the public feed (local CSVs still written)", "yellow")

    # Reuse this cycle's values - the leaderboard costs ZERO extra API calls
    print_leaderboard(cycle_values)

    # 💓 Cycle complete - THIS is the stamp the watcher checks against the
    # schedule to prove no round was missed
    with _hb_lock:
        _hb["cycles_done"] += 1
    hb_set(phase="CYCLE_COMPLETE", last_cycle_at=cycle_time.isoformat())


def main():
    # Single-instance lock - two arenas trading the same accounts would be chaos
    lock_file = open("/tmp/moondev_ai_battles_arena.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
    except BlockingIOError:
        cprint("❌ Moon Dev: The arena is ALREADY RUNNING! Kill it first.", "red")
        sys.exit(1)

    cprint("\n" + "=" * 62, "magenta")
    cprint("🥊 MOON DEV'S AI TRADING BATTLES - ONE-SCREEN ARENA 🥊", "magenta", attrs=["bold"])
    cprint("moondev.com/ai - all six fighters, one process", "magenta")
    cprint("=" * 62, "magenta")
    cprint(f"⚙️ Symbol: {SYMBOL} | Interval: {DECISION_INTERVAL_MINUTES}m | Leverage: {LEVERAGE}x "
           f"| Size: {POSITION_SIZE_PERCENT}% | Live: {PLACE_TRADES}", "cyan")

    if not os.getenv("HYPERLIQUID_PRIVATE_KEY"):
        raise ValueError("Moon Dev: HYPERLIQUID_PRIVATE_KEY missing from .env!")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise ValueError("Moon Dev: OPENROUTER_API_KEY missing from .env!")
    if not os.getenv("MOONDEV_API_KEY"):
        raise ValueError("Moon Dev: MOONDEV_API_KEY missing from .env!")

    account = eth_account.Account.from_key(os.getenv("HYPERLIQUID_PRIVATE_KEY"))
    fighters = {}
    for name, cfg in BATTLE_BOTS.items():
        vault = os.getenv(cfg["sub_acct_env"])
        if not vault:
            raise ValueError(f"Moon Dev: {cfg['sub_acct_env']} missing from .env!")
        exchange = Exchange(account, constants.MAINNET_API_URL, vault_address=vault)
        if PLACE_TRADES:
            exchange.update_leverage(LEVERAGE, SYMBOL, is_cross=True)
        value = get_account_value(vault)
        fighters[name] = {"model": cfg["model"], "vault": vault, "exchange": exchange}
        hb_fighter(name, state="CONNECTED", model=cfg["model"], value=round(value, 2))
        cprint(f"🏦 {name:<9} connected to {cfg['sub_acct_env']} ({cfg['model']}) | value: ${value:,.2f}", "green")

    # 💓 Start the heartbeat only AFTER every fighter is wired up - a heartbeat
    # that starts before the accounts connect would report a healthy arena that
    # is about to die on a missing key
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    hb_target = os.getenv("BATTLE_HEARTBEAT_URL") or "local file only (no BATTLE_HEARTBEAT_URL set)"
    cprint(f"💓 Heartbeat live every {HEARTBEAT_WRITE_SECONDS}s -> {HEARTBEAT_FILE}", "magenta")
    cprint(f"💓 Outbound push every {HEARTBEAT_PUSH_SECONDS}s -> {hb_target}", "magenta")

    if PLACE_TRADES:
        # Live mode never trades off-schedule: first decision waits for the
        # next :offset mark, so restarts can't fire surprise mid-hour trades
        sleep_until_next_cycle()

    while True:
        try:
            arena_cycle(fighters)
        except KeyboardInterrupt:
            cprint("\n👋 Moon Dev: arena shutting down gracefully...", "yellow")
            hb_set(phase="SHUTDOWN")
            break
        except Exception as e:
            cprint(f"❌ Moon Dev: arena cycle error: {e}", "red")
            hb_set(phase="ERROR")
            hb_error(f"arena cycle: {e}")

        hb_set(phase="SLEEPING")
        sleep_until_next_cycle()


if __name__ == "__main__":
    main()
