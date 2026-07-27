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

POLL_SECONDS = 3                 # how often to pull the heartbeat
RENDER_SECONDS = 1               # screen refresh (countdown ticks every second)
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

def render(blob, source, alarms, fetch_error):
    sys.stdout.write("\033[H\033[J")  # home + clear, no flicker scrollback
    cprint("=" * 78, "magenta")
    cprint("📡 MOON DEV'S AI BATTLE WATCHTOWER 📡   moondev.com/ai", "magenta", attrs=["bold"])
    cprint(f"   watching: {source}", "magenta")
    cprint("=" * 78, "magenta")

    if not blob:
        cprint(f"\n❌ {fetch_error}\n", "red", attrs=["bold"])
        cprint("   Nothing is reporting. The arena is down, or the API can't be reached.", "red")
        return

    age = heartbeat_age(blob)
    healthy = not alarms
    pulse = "💓" if int(time.time()) % 2 == 0 else "🤍"
    status = colored(f"{pulse} ALIVE", "green", attrs=["bold"]) if healthy else \
        colored("🚨 PROBLEM", "red", attrs=["bold"])
    up = blob.get("uptime_s", 0)
    clock = "relay clock" if blob.get("server_age_s") is not None else "local clock"
    print(f"{status}  last beat {int(age)}s ago ({clock})   host: {blob.get('host', '?')} "
          f"(pid {blob.get('pid', '?')})  up {up // 3600}h {(up % 3600) // 60}m")
    if blob.get("server_received_at"):
        relay = dt.datetime.fromtimestamp(blob["server_received_at"]).strftime("%H:%M:%S")
        flag = colored(" STALE", "red", attrs=["bold"]) if blob.get("stale") else ""
        cprint(f"📨 relay received the last beat at {relay}{flag}", "blue")

    live = "LIVE 💵" if blob.get("live_trading") else "DRY RUN 📝"
    cprint(f"⚙️  {blob.get('symbol')} | {blob.get('interval_minutes')}m cadence | {live} "
           f"| cycles done: {blob.get('cycles_done', 0)} "
           f"| disk free {blob.get('disk_free_gb')}GB ({blob.get('disk_used_pct')}% used)", "cyan")

    # Countdown to the next scheduled bell
    nxt = blob.get("next_cycle_at")
    if nxt:
        remaining = max(0, nxt - time.time())
        mins, secs = divmod(int(remaining), 60)
        interval_s = blob.get("interval_minutes", 60) * 60
        frac = max(0.0, min(1.0, 1 - remaining / interval_s))
        bar = "█" * int(24 * frac) + "░" * (24 - int(24 * frac))
        wake = dt.datetime.fromtimestamp(nxt).strftime("%H:%M:%S")
        cprint(f"🔔 NEXT BATTLE {wake} local   in {mins:02d}:{secs:02d}  |{bar}|", "yellow")

    phase = blob.get("phase", "?")
    phase_color = {"THINKING": "yellow", "ERROR": "red", "SLEEPING": "blue"}.get(phase, "cyan")
    cprint(f"📍 phase: {phase}", phase_color, attrs=["bold"])
    cprint("-" * 78, "cyan")

    for name, f in (blob.get("fighters") or {}).items():
        state = f.get("state", "?")
        decision = f.get("decision") or "-"
        row = (f" {name:<9} [{(f.get('position_before') or '?'):<5}] "
               f"{state:<9} {decision:<8} {(f.get('action') or '-'):<22} "
               f"${f.get('value', 0):>8,.2f} {f.get('roi_pct', 0):+7.2f}%")
        if f.get("answer_s"):
            row += f" {f['answer_s']:>5}s"
        color = DECISION_COLORS.get(decision, STATE_COLORS.get(state, "white"))
        cprint(row, color, attrs=["bold"] if state in ("THINKING", "ANSWERED") else [])

    cprint("-" * 78, "cyan")
    last = parse_iso(blob.get("last_cycle_at"))
    if last:
        since = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
        cprint(f"🕒 last completed cycle: {last.strftime('%Y-%m-%d %H:%M:%S')} UTC ({ago(since)})",
               "green" if since < blob.get("interval_minutes", 60) * 60 + MISSED_GRACE_SECONDS else "red")
    else:
        cprint("🕒 no completed cycle yet this run", "yellow")

    if blob.get("last_error"):
        cprint(f"⚠️ last arena error: {blob['last_error'][:150]}", "yellow")

    if alarms:
        print()
        cprint("🚨" * 26, "red", attrs=["bold"])
        for _, msg in alarms:
            cprint(f"🚨 {msg}", "red", attrs=["bold", "reverse"])
        cprint("🚨" * 26, "red", attrs=["bold"])
    else:
        cprint("\n✅ all systems green - the battle never misses 🌙", "green", attrs=["bold"])


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
    last_poll = 0

    while True:
        try:
            if time.time() - last_poll >= POLL_SECONDS:
                blob, fetch_error = fetch(source)
                last_poll = time.time()
                stinger = sound_triggers(blob, prev, stinger)
                prev = blob

            alarms = check_alarms(blob, fetch_error)
            last_alarmed, was_alarming = alarm_sounds(alarms, last_alarmed, was_alarming)
            render(blob, source, alarms, fetch_error)
            time.sleep(RENDER_SECONDS)
        except KeyboardInterrupt:
            cprint("\n👋 Moon Dev: watchtower closing - the battle keeps fighting 🥊", "yellow")
            break


if __name__ == "__main__":
    main()
