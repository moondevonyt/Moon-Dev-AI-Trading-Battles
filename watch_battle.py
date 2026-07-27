#!/usr/bin/env python3
"""
📡 MOON DEV'S AI BATTLE WATCHTOWER 📡
moondev.com/ai | the loud half of the battle.

Run: python watch_battle.py                  # watches the local heartbeat file
     python watch_battle.py <heartbeat_url>  # watches the server over the Moon Dev API
     WATCH_HEARTBEAT_URL=... python watch_battle.py

WHY THIS EXISTS (Moon Dev):
  Two rounds got missed - one to a bug, one to a full disk - and nobody knew
  until hours later. That can never happen again. run_battle.py now runs
  SILENT on the server and publishes a heartbeat; THIS script runs on the mac
  all day, reads that heartbeat, and does everything fun and everything loud:

    🔊 sounds      - battle bell, a stinger per fighter as each model answers,
                     cash register when the cycle closes
    🗣️ announcements - macOS `say` calls every decision out loud
    🎨 the arena    - six fighters, live states, countdown, leaderboard
    🚨 ALARMS       - and this is the whole point:
         NO HEARTBEAT  -> the arena is dead or the network is gone
         STALE         -> alive but WEDGED (hung model call, frozen socket) -
                          the failure no restart-on-crash supervisor catches
         MISSED ROUND  -> a scheduled :13 came and went with no cycle
         DISK LOW      -> the exact thing that ate round #2, caught early
         ERROR         -> the arena recorded an exception

  The screen only lights up because REAL data is landing. Nothing here is
  simulated - if the battle is silent, this screen is silent, and then it
  starts screaming.

Zero dependency on battle_core - this runs anywhere with requests + termcolor.

Built with love by Moon Dev 🌙
"""

import os
import sys
import json
import time
import shutil
import subprocess
import datetime as dt
from pathlib import Path

import requests
from termcolor import cprint, colored
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# ⚙️ WATCHTOWER CONFIG - Moon Dev
# ============================================================================

POLL_SECONDS = 3                 # how often to pull the heartbeat (network)
RENDER_SECONDS = 0.5             # screen refresh - 2 frames/sec so it MOVES
STALE_SECONDS = 90               # heartbeat older than this = WEDGED
MISSED_GRACE_SECONDS = 300       # cycle this late past schedule = MISSED ROUND
DISK_WARN_GB = 5                 # below this = DISK LOW (round #2's killer)
ALARM_REPEAT_SECONDS = 30        # re-scream every 30s until it's fixed
SPEAK = True                     # macOS `say` announcements
SOUNDS = True                    # afplay sound effects

HEARTBEAT_FILE = Path(__file__).parent / "data" / "heartbeat.json"

# 🔊 Moon Dev's stream flair - all of it moved off the server and onto the mac.
# Public repo: point MOON_SOUND_DIR at your own folder, or run silent (missing
# files are skipped, the alarms still print in screaming red)
SOUND_DIR = Path(os.getenv("MOON_SOUND_DIR", "/Users/md/Dropbox/dev/github/Untitled/sounds"))
SND_BATTLE_START = SOUND_DIR / "UI_CLASSIC_Feedback_Positive_04.wav"
SND_DECISION = [SOUND_DIR / "final_fant1.MP3", SOUND_DIR / "final_fant2.MP3"]
SND_CYCLE_DONE = SOUND_DIR / "cashreg.wav"
SND_ALARM = SOUND_DIR / "shots.wav"
SND_STALE = SOUND_DIR / "echoradar.wav"
SND_RECOVERED = SOUND_DIR / "yahoooo.wav"

_AFPLAY = shutil.which("afplay")
_SAY = shutil.which("say")

# ============================================================================
# 🎓 THE TICKER - the fun half (Moon Dev)
# ============================================================================
# The arena's countdown hype used to live in sleep_until_next_cycle(), which
# now runs silent on a server nobody watches. It belongs here. Half trash talk,
# half actual rules: somebody who leaves this on a second monitor should learn
# how the benchmark works without ever opening the README.

TICKER_TICKS = 12                # frames each line stays up (12 x 0.5s = 6s)
_TICKER_COLORS = ["cyan", "magenta", "yellow", "green"]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"       # spins next to any model that's still thinking

BATTLE_FACTS = [
    "🥊 six AIs. one market. no mercy.",
    "🧠 CLAUDE is doing breathing exercises...",
    "📏 THE STAKE: $100 each, 1x leverage, BTC only. same stake, same market.",
    "🤖 OPENAI is reading the tape...",
    "🎯 A SEASON: 1,000 decisions, about six weeks. no swaps, no upgrades mid-season.",
    "🔮 GEMINI is counting liquidations...",
    "💀 THE LINE: down 50% and you're out. no second life for a model that blew up.",
    "⚡ GROK is talking trash to the orderbook...",
    "🔔 THE BELL: :13 past the hour, never :00 - that's where the algo herds cluster.",
    "🌙 KIMI is thinking... still thinking...",
    "📸 THE SNAPSHOT: 72 hourly candles + everyone's big positions + liquidation totals.",
    "🐋 DEEPSEEK is watching the whales...",
    "🚫 NO STOPS. NO TAKE PROFITS. the model's decision IS the whole strategy.",
    "⚖️ RANKED on risk-adjusted return and drawdown, not raw dollars.",
    "🔗 every trade settles on chain - click any model at moondev.com/ai to watch its wallet.",
    "🪑 THE BENCH: Meta, Z.ai, MiniMax, Qwen - waiting for a seat to open.",
    "🔬 the whole harness is open source. every prompt, every line, readable.",
    "📊 same data. same rules. may the best model win.",
    "🧾 NOT financial advice. these AIs are not advisors. neither is Moon Dev.",
    "📼 every decision. public. forever.",
    "🌙 Moon Dev's AI Trading Battles - moondev.com/ai",
]


def battle_panel(blob, tick):
    """The old arena countdown, reborn on the mac, and it NEVER sits still.

    Every tick something moves: the ⚔️/🥊 pulse flips, the bar's leading edge
    shimmers, the seconds fall. Every TICKER_TICKS the whole block changes
    color and serves the next line of trash talk or rules. This runs the entire
    time the arena is idle between rounds, which is 59 minutes of every hour -
    that idle stretch is exactly when somebody watching should be learning how
    the benchmark works. Returns lines; paint() puts them on screen."""
    i = tick // TICKER_TICKS
    color = _TICKER_COLORS[i % len(_TICKER_COLORS)]
    pulse = "⚔️" if tick % 2 == 0 else "🥊"
    lines = [colored("━" * 78, color)]

    nxt = (blob or {}).get("next_cycle_at")
    if nxt:
        remaining = max(0, nxt - time.time())
        mins, secs = divmod(int(remaining), 60)
        interval_s = (blob or {}).get("interval_minutes", 60) * 60
        frac = max(0.0, min(1.0, 1 - remaining / interval_s))
        filled = int(40 * frac)
        # the leading edge shimmers so the bar is alive even when it isn't moving
        head = "▓▒▓█"[tick % 4]
        bar = ("█" * (filled - 1) + head if filled else "") + "░" * (40 - filled)
        wake = dt.datetime.fromtimestamp(nxt).strftime("%H:%M:%S")
        # inside the last minute the clock BLINKS - the bell is about to ring
        attrs = ["bold", "blink"] if remaining <= 60 else ["bold"]
        lines.append(colored(f"  {pulse}  NEXT BATTLE {wake}   {mins:02d}:{secs:02d}  |{bar}|",
                             color, attrs=attrs))
    lines.append(colored(f"     {BATTLE_FACTS[i % len(BATTLE_FACTS)]}", color, attrs=["bold"]))
    lines.append(colored("━" * 78, color))
    return lines


def standings(fighters):
    """Live leader/last/pot off the REAL heartbeat values - no invented numbers,
    so this line is blank until the fighters actually report equity"""
    vals = {n: f["value"] for n, f in fighters.items() if f.get("value") is not None}
    if len(vals) < 2:
        return None
    ranked = sorted(vals.items(), key=lambda kv: kv[1], reverse=True)
    (win_n, win_v), (lose_n, lose_v) = ranked[0], ranked[-1]
    pot = sum(vals.values())
    return (f"🏆 leader {win_n} ${win_v:,.2f}   💀 last {lose_n} ${lose_v:,.2f}   "
            f"🪙 pot ${pot:,.2f} across {len(vals)}")


STATE_COLORS = {
    "WAITING": "white", "THINKING": "yellow", "ANSWERED": "cyan",
    "DONE": "green", "CONNECTED": "green", "BENCHED": "red",
}
DECISION_COLORS = {"LONG": "green", "SHORT": "red", "CLOSE": "yellow", "NOTHING": "white"}


def play(path):
    """Fire-and-forget sound - never blocks the watchtower"""
    if SOUNDS and _AFPLAY and path.exists():
        subprocess.Popen([_AFPLAY, str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(text):
    """macOS announcement - Moon Dev hears the battle from across the room"""
    if SPEAK and _SAY:
        subprocess.Popen([_SAY, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ============================================================================
# 📡 HEARTBEAT SOURCE - local file or the Moon Dev API (outbound only)
# ============================================================================

def resolve_source():
    """CLI arg > WATCH_HEARTBEAT_URL env > local data/heartbeat.json"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    url = os.getenv("WATCH_HEARTBEAT_URL")
    return url if url else str(HEARTBEAT_FILE)


def fetch(source):
    """Pull the latest heartbeat. Returns (blob, error_string)."""
    try:
        if source.startswith("http"):
            r = requests.get(source, headers={"X-Battle-Key": os.getenv("BATTLE_PUSH_KEY", "")},
                             timeout=10)
            # The relay's failure codes mean different things - say WHICH, so a
            # bad key never gets mistaken for a dead battle box at 3am
            if r.status_code == 401:
                return None, "401 UNAUTHORIZED - BATTLE_PUSH_KEY here doesn't match the API server"
            if r.status_code == 404:
                return None, "404 - the relay has never received a beat (arena not pushing yet?)"
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:120]}"
            return r.json(), None
        p = Path(source)
        if not p.exists():
            return None, f"no heartbeat file at {p}"
        return json.loads(p.read_text()), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def parse_iso(s):
    """Heartbeat timestamps are UTC (naive from utcnow, or ISO with tz)"""
    if not s:
        return None
    stamp = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def heartbeat_age(blob):
    """Seconds since the last beat.

    When we're reading the Moon Dev API relay, trust ITS measurement
    (server_age_s) over subtracting two machines' clocks: the mac and the
    battle box each have their own idea of `now`, and a few minutes of drift
    on either one would invent a STALE alarm out of thin air - or, far worse,
    hide a real one. The relay stamps arrival with a single clock, so the
    number is honest no matter what either box thinks the time is."""
    if blob.get("server_age_s") is not None:
        return blob["server_age_s"]
    return time.time() - blob.get("ts", 0)


def ago(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s ago"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"


# ============================================================================
# 🚨 THE ALARMS - the reason this script exists
# ============================================================================

def check_alarms(blob, fetch_error):
    """Every way this battle can silently break, in one place. Returns a list
    of (KEY, message) - empty list means everything is healthy."""
    alarms = []
    if fetch_error or not blob:
        alarms.append(("NO_HEARTBEAT", f"NO HEARTBEAT - {fetch_error or 'empty response'}"))
        return alarms

    age = heartbeat_age(blob)
    if age > STALE_SECONDS:
        via = " (per the relay's own clock)" if blob.get("server_age_s") is not None else ""
        alarms.append(("STALE", f"HEARTBEAT STALE - {int(age)}s old{via}. The arena is "
                                f"alive-but-wedged or the box is gone"))

    # Did a scheduled round come and go without a cycle? Only ask once there IS
    # a completed cycle to measure from. A freshly booted arena has no
    # last_cycle_at yet, and crying MISSED ROUND at every restart would train
    # Moon Dev to ignore the one alarm that matters most.
    interval_s = blob.get("interval_minutes", 60) * 60
    last = parse_iso(blob.get("last_cycle_at"))
    if last:
        since = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
        if since > interval_s + MISSED_GRACE_SECONDS:
            alarms.append(("MISSED", f"MISSED ROUND - last cycle was {ago(since)}, "
                                     f"cadence is {blob.get('interval_minutes')}m"))

    free = blob.get("disk_free_gb")
    if free is not None and free < DISK_WARN_GB:
        alarms.append(("DISK", f"DISK LOW - {free}GB free. This is what ate a round before"))

    if blob.get("last_error"):
        err_at = parse_iso(blob.get("last_error_at"))
        if err_at and (dt.datetime.now(dt.timezone.utc) - err_at).total_seconds() < 2 * interval_s:
            alarms.append(("ERROR", f"ARENA ERROR - {blob['last_error'][:150]}"))
    return alarms


# ============================================================================
# 🎨 THE SCREEN
# ============================================================================

def render(blob, source, alarms, fetch_error, tick=0):
    """Build the WHOLE frame, then paint it in ONE write.

    Painting line by line (clear screen, then 25 separate prints) is what made
    this feel dead: the terminal shows a blank screen for a moment every
    redraw, so the eye reads flicker instead of motion. One write per frame at
    RENDER_HZ, with each line erased to its own end, and it moves like the
    arena countdown always did."""
    out = []
    out.append(colored("=" * 78, "magenta"))
    out.append(colored("📡 MOON DEV'S AI BATTLE WATCHTOWER 📡   moondev.com/ai", "magenta", attrs=["bold"]))
    out.append(colored(f"   watching: {source}", "magenta"))
    out.append(colored("=" * 78, "magenta"))

    if not blob:
        out.append("")
        out.append(colored(f"❌ {fetch_error}", "red", attrs=["bold", "blink"]))
        out.append("")
        out.append(colored("   Nothing is reporting. The arena is down, or the API can't be reached.", "red"))
        # Even dead, the rules keep teaching - the screen is never blank
        out.extend(battle_panel(None, tick))
        paint(out)
        return

    age = heartbeat_age(blob)
    healthy = not alarms
    pulse = "💓" if tick % 2 == 0 else "🤍"
    status = colored(f"{pulse} ALIVE", "green", attrs=["bold"]) if healthy else \
        colored("🚨 PROBLEM", "red", attrs=["bold", "blink"])
    up = blob.get("uptime_s", 0)
    clock = "relay clock" if blob.get("server_age_s") is not None else "local clock"
    out.append(f"{status}  last beat {int(age)}s ago ({clock})   host: {blob.get('host', '?')} "
               f"(pid {blob.get('pid', '?')})  up {up // 3600}h {(up % 3600) // 60}m")
    if blob.get("server_received_at"):
        relay = dt.datetime.fromtimestamp(blob["server_received_at"]).strftime("%H:%M:%S")
        flag = colored(" STALE", "red", attrs=["bold"]) if blob.get("stale") else ""
        out.append(colored(f"📨 relay received the last beat at {relay}", "blue") + flag)

    live = "LIVE 💵" if blob.get("live_trading") else "DRY RUN 📝"
    out.append(colored(f"⚙️  {blob.get('symbol')} | {blob.get('interval_minutes')}m cadence | {live} "
                       f"| cycles done: {blob.get('cycles_done', 0)} "
                       f"| disk free {blob.get('disk_free_gb')}GB ({blob.get('disk_used_pct')}% used)", "cyan"))

    phase = blob.get("phase", "?")
    phase_color = {"THINKING": "yellow", "ERROR": "red", "SLEEPING": "blue"}.get(phase, "cyan")
    thinking = phase == "THINKING"
    out.append(colored(f"📍 phase: {phase}" + (f"  {SPINNER[tick % len(SPINNER)]} models are deciding..."
                                               if thinking else ""),
                       phase_color, attrs=["bold"]))
    out.append(colored("-" * 78, "cyan"))

    for name, f in (blob.get("fighters") or {}).items():
        state = f.get("state", "?")
        decision = f.get("decision") or "-"
        # A model still thinking gets a live spinner, so you can SEE it working
        shown = f"{SPINNER[tick % len(SPINNER)]} {state}" if state == "THINKING" else f"  {state}"
        row = (f" {name:<9} [{(f.get('position_before') or '?'):<5}] "
               f"{shown:<11} {decision:<8} {(f.get('action') or '-'):<22} "
               f"${f.get('value', 0):>8,.2f} {f.get('roi_pct', 0):+7.2f}%")
        if f.get("answer_s"):
            row += f" {f['answer_s']:>5}s"
        color = DECISION_COLORS.get(decision, STATE_COLORS.get(state, "white"))
        out.append(colored(row, color, attrs=["bold"] if state in ("THINKING", "ANSWERED") else []))

    out.append(colored("-" * 78, "cyan"))
    board = standings(blob.get("fighters") or {})
    if board:
        out.append(colored(board, "white", attrs=["bold"]))
    last = parse_iso(blob.get("last_cycle_at"))
    if last:
        since = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
        out.append(colored(f"🕒 last completed cycle: {last.strftime('%Y-%m-%d %H:%M:%S')} UTC ({ago(since)})",
                           "green" if since < blob.get("interval_minutes", 60) * 60 + MISSED_GRACE_SECONDS else "red"))
    else:
        out.append(colored("🕒 no completed cycle yet this run", "yellow"))

    if blob.get("last_error"):
        out.append(colored(f"⚠️ last arena error: {blob['last_error'][:150]}", "yellow"))

    out.append("")
    if alarms:
        # Alarm banner INVERTS every tick - impossible to have on screen and
        # not notice, which is the entire job
        flash = ["bold", "reverse"] if tick % 2 == 0 else ["bold"]
        out.append(colored("🚨" * 26, "red", attrs=["bold"]))
        for _, msg in alarms:
            out.append(colored(f"🚨 {msg}", "red", attrs=flash))
        out.append(colored("🚨" * 26, "red", attrs=["bold"]))
    else:
        out.append(colored("✅ all systems green - the battle never misses 🌙", "green", attrs=["bold"]))

    # 🥊 The countdown + hype panel goes LAST, on purpose. It's the only thing
    # on screen that moves every frame, and the eye sits at the bottom of a
    # terminal - so the moving thing belongs where you're already looking.
    out.extend(battle_panel(blob, tick))
    paint(out)


def paint(lines):
    """One atomic write: cursor home, every line erased to its own end (\\033[K),
    then clear whatever the last frame left below (\\033[J). No blank moment,
    so it reads as motion instead of flicker."""
    frame = "\033[H" + "".join(f"{ln}\033[K\n" for ln in lines) + "\033[J"
    sys.stdout.write(frame)
    sys.stdout.flush()


# ============================================================================
# 🔊 SOUND TRIGGERS - fire only on REAL state changes in the heartbeat
# ============================================================================

def sound_triggers(blob, prev, stinger):
    """Compare this heartbeat to the last one and make noise on real changes.
    Returns the next stinger index."""
    if not blob:
        return stinger

    # New cycle started - DING DING DING
    if blob.get("cycle_started_at") and blob.get("cycle_started_at") != (prev or {}).get("cycle_started_at"):
        play(SND_BATTLE_START)
        speak("Battle time. Six A Eyes, one market.")

    prev_f = (prev or {}).get("fighters") or {}
    for name, f in (blob.get("fighters") or {}).items():
        was = prev_f.get(name, {})
        # This fighter just answered - stinger + call the decision out loud
        if f.get("decision") and f.get("decision") != was.get("decision"):
            play(SND_DECISION[stinger % 2])
            stinger += 1
            speak(f"{name} says {f['decision']}")

    # Cycle closed out
    if blob.get("last_cycle_at") and blob.get("last_cycle_at") != (prev or {}).get("last_cycle_at"):
        play(SND_CYCLE_DONE)
        fighters = (blob.get("fighters") or {})
        if fighters:
            leader = max(fighters.items(), key=lambda kv: kv[1].get("value") or 0)
            speak(f"Round complete. {leader[0]} leads.")
    return stinger


def alarm_sounds(alarms, last_alarmed, was_alarming):
    """Scream on every new alarm, then re-scream every ALARM_REPEAT_SECONDS
    until Moon Dev fixes it. Cheer once when it comes back."""
    now = time.time()
    if not alarms:
        if was_alarming:
            play(SND_RECOVERED)
            speak("Heartbeat recovered. The battle is back.")
        return {}, False

    for key, msg in alarms:
        if now - last_alarmed.get(key, 0) >= ALARM_REPEAT_SECONDS:
            play(SND_STALE if key in ("STALE", "NO_HEARTBEAT") else SND_ALARM)
            speak({
                "NO_HEARTBEAT": "Moon Dev. No heartbeat. The arena is down.",
                "STALE": "Moon Dev. Heartbeat stale. The arena is wedged.",
                "MISSED": "Moon Dev. The battle missed a round.",
                "DISK": "Moon Dev. Disk space is low on the battle server.",
                "ERROR": "Moon Dev. The arena reported an error.",
            }.get(key, "Moon Dev. Check the battle."))
            last_alarmed[key] = now
    return last_alarmed, True


def main():
    source = resolve_source()
    cprint("\n📡 MOON DEV'S AI BATTLE WATCHTOWER starting...", "magenta", attrs=["bold"])
    cprint(f"   source: {source}", "magenta")
    cprint(f"   stale after {STALE_SECONDS}s | missed round after "
           f"cadence + {MISSED_GRACE_SECONDS // 60}m | disk warn under {DISK_WARN_GB}GB", "magenta")
    if not _AFPLAY:
        cprint("   (no afplay found - running silent)", "yellow")
    time.sleep(1)

    blob, fetch_error, prev = None, None, None
    stinger, last_alarmed, was_alarming = 0, {}, False
    last_poll, tick = 0, 0

    while True:
        try:
            if time.time() - last_poll >= POLL_SECONDS:
                blob, fetch_error = fetch(source)
                last_poll = time.time()
                stinger = sound_triggers(blob, prev, stinger)
                prev = blob

            alarms = check_alarms(blob, fetch_error)
            last_alarmed, was_alarming = alarm_sounds(alarms, last_alarmed, was_alarming)
            render(blob, source, alarms, fetch_error, tick)
            tick += 1
            time.sleep(RENDER_SECONDS)
        except KeyboardInterrupt:
            cprint("\n👋 Moon Dev: watchtower closing - the battle keeps fighting 🥊", "yellow")
            break


if __name__ == "__main__":
    main()
