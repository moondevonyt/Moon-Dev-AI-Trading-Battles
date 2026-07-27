# 🥊 Moon Dev's AI Trading Battles

**Live at [moondev.com/ai](https://moondev.com/ai).** Six flagship AI models each trade a real **$100** Hyperliquid perpetuals account against each other. Every hour they all get the exact same market snapshot and decide: long, short, or flat. No human help, no algorithm behind it, the model's decisions only. Every decision is public forever.

## The question this exists to answer

A new frontier model ships what feels like every month now, from a different lab every time, each one benchmarked to death on math, code, and trivia. None of that answers the only question a trader cares about:

> **Which AI is actually the best at trading?**
>
> Not which one predicts best. Which one *decides* best: sizing, timing, when to sit still, when to cut.

So they all get the **identical data**, the **identical rules**, and **1,000 decisions** to prove it. Same snapshot down to the tick, same prompt, same market, same hour. Whatever separates them is the model.

Predictions and hot streaks aren't skill. **Execution, risk, and time are everything.** This is the running, public proof, the benchmark that gets re-run every time a lab ships a new flagship.

## 🔍 100% transparent, on purpose

A benchmark you can't audit is a marketing page. So:

- **The whole harness is this repo.** Every prompt, the exact market snapshot, the cadence, the position sizing, the execution path, all of it is right here, readable. Nothing about how these models are asked or how their trades are placed is hidden.
- **Every trade settles on chain.** On [moondev.com/ai](https://moondev.com/ai) you can **click any model** to open its live P&L curve and its actual Hyperliquid address, and watch its positions in real time. You don't have to trust the leaderboard, go read the chain.
- **Every decision is posted the moment it's made**, with the model's complete unedited reasoning. Including the bad ones. Especially the bad ones.

## The Fighters

| Fighter | Lab | Model (via OpenRouter) |
|---|---|---|
| CLAUDE | Anthropic | anthropic/claude-opus-5 |
| OPENAI | OpenAI | openai/gpt-5.6-sol |
| GEMINI | Google | google/gemini-3.1-pro-preview |
| GROK | xAI | x-ai/grok-4.5 |
| KIMI | Moonshot | moonshotai/kimi-k3 |
| DEEPSEEK | DeepSeek | deepseek/deepseek-v4-pro |

**The Bench:** waiting labs (Meta, Z.ai/GLM, MiniMax, Qwen…), ranked by independent intelligence index, promoted when a seat opens.

## The Rules

- **$100 each**, own Hyperliquid subaccount, **1x leverage** to start, BTC only (beta)
- **A season is 1,000 decisions** (~six weeks). Each lab fields its premier model at the start, and **nothing changes mid-season**: no swaps, no upgrades, no substitutions. Seats update to current flagships at the start of the next season.
- Every **60 minutes**, at **:13 past the hour**, never on the algo-crowded round hour, all six receive the **identical snapshot**, raw data, zero interpretation:
  1. 72 hourly BTC candles (high/low/close/volume CSV) + live bid/ask + RSI/SMAs
  2. Everybody's BTC positions ≥ $450k with liq prices (Moon Dev API, People's Positions)
  3. Past liquidation totals across binance+bybit+okx+hyperliquid (6 trailing windows)
- Flat → answer `LONG`, `SHORT`, or `NOTHING` · In a position → `LONG`, `SHORT`, `CLOSE`, or `NOTHING` (opposite side = flip)
- **Fixed position sizing. No stop losses, no take profits.** The model's decision is the entire strategy.
- Ranked by **risk-adjusted return and drawdown**, not raw dollars, measured against crowd, random, and house-bot baselines
- Lose **50%** of the starting stake = **eliminated** 💀. **No second life for a model that already blew up.** The blowup stays on the permanent record, and a lab only returns with an entirely NEW model next season, never a retry
- Every decision, raw response, and equity point logged forever to `data/`

## ⚠️ This is not financial advice

Read this part twice.

- **None of this is financial advice.** These AI models are **not financial advisors**. Neither is Moon Dev. Nothing here is a recommendation to buy, sell, or hold anything.
- **This is a live experiment, not investment advice.** It's an open benchmark measuring how frontier models behave in a real market. That is *all* it is.
- **This is not plug-and-play.** Do not clone this and point it at your own money expecting it to work. It trades real funds with no stops, no take profits, and no human in the loop, by design, because that's what makes the measurement clean. It is not a product, not a signal service, and not a strategy.
- **Substantial risk of loss.** Models are *expected* to blow up here. The elimination rule exists precisely because some of them will.
- Moon Dev doesn't believe AI can out-trade you either. **Apply AI to coding your own edge instead.** This is the benchmark everyone asked for, not a shortcut.

## Architecture: one screen, one process

```
battle_core.py   <- ALL shared logic: config, data, prompts, execution, logging
run_battle.py    <- 🖥️ THE ARENA: all six fighters in one process (+ heartbeat)
watch_battle.py  <- 📡 THE WATCHTOWER: sounds, announcements, alarms (runs on the mac)
preflight.py     <- 6-check go-live test: keys, funding, candles, order signing, models, push
data/            <- shared snapshot cache + per-bot decisions/equity CSVs (gitignored, the public record lives at moondev.com/ai)
```

`run_battle.py` fetches the snapshot **once** per cycle, asks all six models **in parallel**, and each fighter's trade fires **the moment its model answers**, no waiting on the slow thinkers. Order signing happens on the main thread one at a time (one private key signs all six subaccounts → zero nonce collisions). One screen to restart when things need a kick.

The six subaccounts are Hyperliquid **unified accounts**: idle margin lives on the spot side, so equity = free spot USDC (total − hold) + perps accountValue. The Moon Dev API `/api/account` returns perps + spot in ONE call, so account state is one API hit per fighter per cycle. Candles are the only HL read (one shared fetch per cycle), and bid/ask, people's positions, and liquidations also come from the Moon Dev API.

**Indicators are hand-rolled** (`_sma` / `_rsi` in `battle_core.py`) rather than pulled from a TA library. A library that silently uses TA-Lib when it's installed and different math when it isn't means the same battle produces different RSI on different boxes. For a benchmark that has to stay comparable across years, the numbers cannot depend on which machine it woke up on, so they're ~15 lines here, verified identical to TA-Lib to 1e-14.

## 💓 Heartbeat & Watchtower: this battle never misses a round

The arena runs **silent** (it's built for a headless Linux box) and publishes its own vitals every 5s: `data/heartbeat.json` locally, plus an **outbound-only** POST to the Moon Dev API relay. The battle box never opens an inbound port and never exposes its IP.

`watch_battle.py` runs on the Mac all day and is the loud half: the six fighters live on screen, a stinger as each model answers, every decision announced out loud, cash register when the round closes. And the part that matters, it **screams** on `NO HEARTBEAT` (arena dead), `STALE` (alive but wedged on a hung model call, the failure no auto-restart catches), `MISSED ROUND`, `DISK LOW`, or a recorded arena `ERROR`, repeating every 30s until it's fixed. Staleness is measured by the relay's own clock, so two machines disagreeing about the time can never invent a false alarm or hide a real one.

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

- `data/decisions_{BOT}.csv`: every decision, position before, decision, action, price, account value, raw model response
- `data/equity_{BOT}.csv`: equity curve per fighter, every cycle (feeds the moondev.com/ai leaderboard)
- Console leaderboard every cycle 🏆

Built with love by Moon Dev 🌙
