# 💓 Heartbeat relay spec — for the Moon Dev API server

Hand this to whoever/whatever builds `api.moondev.com`. It's ~15 lines of work:
**one endpoint to receive, one to serve, one row of storage.** No database
design needed — only the LATEST heartbeat matters.

## Why

`run_battle.py` runs headless on the battle server and pushes its vitals
**outbound every 20s**. `watch_battle.py` on Moon Dev's mac reads them back.
The battle box never opens an inbound port and never exposes its IP — the API
server is the mailbox in the middle.

## 1. Receive — `POST /api/battle/heartbeat`

- Header: `X-Battle-Key: <same shared secret as the decisions push>` → 401 if wrong
- Body: the JSON blob below (~1-3KB)
- Action: **overwrite** the single stored heartbeat (one row / one file / one
  Redis key — history is not needed, and never let this table grow)
- Also stamp `server_received_at` (unix float, server clock) onto what's stored
- Return `200 {"ok": true}`

## 2. Serve — `GET /api/battle/heartbeat`

- Same `X-Battle-Key` header check (this is ops data, keep it private)
- Return the stored blob verbatim, plus the `server_received_at` stamp
- If nothing has ever been received: `404` or `{}` — the watchtower treats
  either as `NO HEARTBEAT` and alarms, which is the correct behavior

That's the whole contract. If the endpoints don't exist yet, the arena keeps
running fine — it just writes `data/heartbeat.json` locally and the push is
skipped when `BATTLE_HEARTBEAT_URL` is blank.

## The blob

```json
{
  "ts": 1785170741.22,          // unix float, battle-box clock — staleness is measured off this
  "ts_iso": "2026-07-27T16:45:41+00:00",
  "phase": "THINKING",          // BOOT/SNAPSHOT/ACCOUNTS/THINKING/PUSHING/CYCLE_COMPLETE/SLEEPING/ERROR/SHUTDOWN
  "host": "battle-server",
  "pid": 24112,
  "started_at": "2026-07-27T16:00:00+00:00",
  "uptime_s": 3600,
  "symbol": "BTC",
  "interval_minutes": 60,
  "live_trading": true,
  "cycles_done": 412,
  "cycle_started_at": "2026-07-27T16:13:00",
  "last_cycle_at": "2026-07-27T16:13:00",   // UTC, stamped when a cycle COMPLETES
  "next_cycle_at": 1785172380,               // unix ts of the next :13 bell
  "last_error": null,
  "last_error_at": null,
  "disk_free_gb": 251.95,
  "disk_used_pct": 74.7,
  "fighters": {
    "CLAUDE": {
      "state": "DONE",           // WAITING/THINKING/ANSWERED/DONE/BENCHED/CONNECTED
      "model": "anthropic/claude-opus-5",
      "value": 104.1,
      "roi_pct": 4.1,
      "position_before": "FLAT",
      "decision": "LONG",
      "action": "OPENED_LONG",
      "answer_s": 12.3
    }
  }
}
```

No raw model responses ride in the heartbeat — those go through the existing
`/decisions` push. This blob stays small enough to POST every 20s forever.

## Optional bonus (nice, not required)

The API server already knows the cadence. If it wants a belt-and-suspenders
alert of its own: if `server_received_at` is more than **5 minutes** old, or
`now - last_cycle_at > interval_minutes + 5min`, fire a push/email to Moon Dev.
That catches the one case the mac watchtower can't — when the mac itself is
asleep.

Built with love by Moon Dev 🌙
