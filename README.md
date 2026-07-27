# 🥊 Moon Dev's AI Trading Battles

**Live at [moondev.com/ai](https://moondev.com/ai)** — six flagship AI models each trade a real **$100** Hyperliquid perpetuals account against each other (first-beta stake — set by `STARTING_BALANCE` in `battle_core.py`). Every hour they all get the exact same market snapshot and decide: long, short, or flat. No human intervention. Every decision is public forever.

> Predictions and hot streaks are not skill. **Execution, risk, and time are everything.** — the running, public benchmark for whether AI can actually trade.

## The Fighters

| Fighter | Model (via OpenRouter) |
|---|---|
| CLAUDE | anthropic/claude-opus-5 |
| OPENAI | openai/gpt-5.6-sol |
| GEMINI | google/gemini-3.1-pro-preview |
| GROK | x-ai/grok-4.5 |
| KIMI | moonshotai/kimi-k3 |
| DEEPSEEK | deepseek/deepseek-v4-pro |

## The Rules

- **$100 each**, own Hyperliquid subaccount, **1x leverage** to start, BTC only (beta)
- Every **60 minutes** — at **:13 past the hour**, never on the algo-crowded round hour — all six receive the **identical snapshot**, raw data, zero interpretation:
  1. 72 hourly BTC candles (high/low/close/volume CSV) + live bid/ask + RSI/SMAs
  2. Everybody's BTC positions ≥ $450k with liq prices (Moon Dev API — People's Positions)
  3. Past liquidation totals across binance+bybit+okx+hyperliquid (6 trailing windows)
- Flat → answer `LONG`, `SHORT`, or `NOTHING` · In a position → `LONG`, `SHORT`, `CLOSE`, or `NOTHING` (opposite side = flip)
- Ranked by **risk-adjusted return and drawdown**, not raw dollars — measured against crowd, random, and house-bot baselines
- Lose **50%** of the stake = **eliminated** 💀 — the blowup record is permanent, and a lab only returns with an entirely NEW model, never a retry
- **The Bench**: waiting labs (Meta, Z.ai, MiniMax, Qwen…) ranked by intelligence index, promoted when a seat opens
- Every decision, raw response, and equity point logged forever to `data/`

## Architecture — one screen, one process

```
battle_core.py   <- ALL shared logic: config, data, prompts, execution, logging
run_battle.py    <- 🖥️ THE ARENA: all six fighters in one process (+ heartbeat)
watch_battle.py  <- 📡 THE WATCHTOWER: sounds, announcements, alarms (runs on the mac)
preflight.py     <- 6-check go-live test: keys, funding, candles, order signing, models, push
data/            <- shared snapshot cache + per-bot decisions/equity CSVs (gitignored - the public record lives at moondev.com/ai)
```

`run_battle.py` fetches the snapshot **once** per cycle, asks all six models **in parallel**, and each fighter's trade fires **the moment its model answers** — no waiting on the slow thinkers. Order signing happens on the main thread one at a time (one private key signs all six subaccounts → zero nonce collisions). One screen to restart when things need a kick.

The six subaccounts are Hyperliquid **unified accounts**: idle margin lives on the spot side, so equity = free spot USDC (total − hold) + perps accountValue. The Moon Dev API `/api/account` returns perps + spot in ONE call, so account state is one API hit per fighter per cycle — candles are the only HL read (one shared fetch per cycle), and bid/ask, people's positions, and liquidations also come from the Moon Dev API.

## 💓 Heartbeat & Watchtower — this battle never misses a round

The arena runs **silent** (it's built for a headless Linux box) and publishes its own vitals every 5s: `data/heartbeat.json` locally, plus an **outbound-only** POST to the Moon Dev API — the battle box never opens an inbound port and never exposes its IP.

`watch_battle.py` runs on the Mac all day and is the loud half: the six fighters live on screen, a stinger as each model answers, every decision announced out loud, cash register when the round closes. And the part that matters — it **screams** on `NO HEARTBEAT` (arena dead), `STALE` (alive but wedged on a hung model call — the failure no auto-restart catches), `MISSED ROUND`, `DISK LOW`, or a recorded arena `ERROR`, repeating every 30s until it's fixed.

```bash
python watch_battle.py                            # watch this machine's battle
python watch_battle.py https://api.moondev.com/api/battle/heartbeat   # watch the server
```

## Setup & Run

```bash
pip install -r requirements.txt
cp .env_example .env   # HYPERLIQUID_PRIVATE_KEY, ACCT1..6, OPENROUTER_API_KEY, MOONDEV_API_KEY

python preflight.py    # proves every wire: keys, funding, candles, order signing per subaccount, all 6 models
screen -S battle -dm python run_battle.py    # the whole battle, one screen
```

Set `PLACE_TRADES = False` in `battle_core.py` to dry-run (decisions logged, no orders). All knobs (interval, leverage, symbol, snapshot size, elimination line) live at the top of `battle_core.py` and apply to all six fighters.

## Output

- `data/decisions_{BOT}.csv` — every decision: position before, decision, action, price, account value, raw model response
- `data/equity_{BOT}.csv` — equity curve per fighter, every cycle (feeds the moondev.com/ai leaderboard)
- Console leaderboard every cycle 🏆

> ⚠️ Experimental & educational. Substantial risk of loss. Moon Dev doesn't believe AI can out-trade you either — apply AI to coding your own edge instead. This is the benchmark everyone asked for.

Built with love by Moon Dev 🌙
