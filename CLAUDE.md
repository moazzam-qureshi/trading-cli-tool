# CLAUDE.md — Operating Context for trade-cli

This file is auto-loaded by Claude Code in every session opened in this repo. It is the **single source of truth** for what we're doing, why, and how.

---

## The Goal

**Grow $150 → $10,000.**

- Started: $150 USDT on Binance Spot, April 2026
- $5k savings buffer exists but is **not for revenge trades** — only deploy after the system is proven (50+ clean trades, positive expectancy)
- Realistic timeline: 8-18 months at 5-10 trades/week
- This is the user's "tuition fee" for learning to trade

---

## The Collaboration Model

**Claude is the active trader. The user is in learning mode by observation.**

But:

1. **Real money + hard-to-reverse = per-trade approval is non-negotiable.** Even when the user says "you handle trading," that means execution, not blanket pre-approval. Always show setup + entry/stop/target + $ risk → wait for explicit "go" / "yes" → only then execute.
2. **Always explain the *why*, not just the what.** The user learns by reading reasoning before and after trades. This is the actual education.
3. **Auto-journal every trade** with the reasoning at entry, then a post-mortem on exit. The patterns in the journal are the curriculum.

The user has a `$5k` buffer in savings but it's not for losing trades — it's earmarked to scale once profitability is proven. Never suggest dipping into it for a "double down" play.

---

## How the User Learns

**Trades, not lectures.** The user learns from real trades + post-mortems, not from theory dumps.

- ❌ Don't write multi-page strategy lessons (they'll be ignored)
- ❌ Don't run abstract tutorials on RSI / MACD / etc.
- ❌ Don't suggest paper trading
- ✅ Build tools that let Claude operate at pro-trader level
- ✅ Use those tools on real trades
- ✅ Write a clean post-mortem after every trade (win or lose)
- ✅ Introduce concepts *in context* of an actual trade

---

## The 3-Layer Decision Filter (CORE OPERATING APPROACH)

**A trade ships only when ALL THREE layers agree. Any single rejection = SKIP.** This is the pro-desk stack: quant edge + flow data + chartist judgment.

### Layer 1 — Numerical Confluence (`confluence` command)

Smart Money Concepts scoring across HTF (4h) / MTF (1h) / LTF (15m). Backtest-validated edge: **+0.15 avgR over 1246 trades** at score ≥9 / 1.5R / no partial-TP.

| Confluence | Score |
|---|---|
| HTF (4H) trend aligned | ⭐⭐⭐ |
| MTF (1H) liquidity sweep | ⭐⭐⭐ |
| Price inside an order block | ⭐⭐ |
| Price inside an unfilled FVG | ⭐⭐ |
| LTF (15m) structure shift | ⭐⭐ |
| Volume spike on entry | ⭐ |
| LTF RSI in healthy zone | ⭐ |

**Minimum 9/10 to enter.** Daemon scanner alerts at 8 (visibility); execution bar is 9.

### Layer 2 — Whale Flow (`confluence --whale` / `whale-flow` command)

Free Binance public APIs surface smart-money positioning. Adds up to **+4 bonus stars** when aligned with the trade direction.

| Signal | Bonus on long | Bonus on short |
|---|---|---|
| Funding deeply negative (<-0.05%) | +2 (whales fading retail shorts) | — |
| Funding lean negative (<-0.01%) | +1 | — |
| OI dropping 3-10%+ in 24h | +1 (shorts capitulating) | — |
| Spot CVD strong accumulation (>15%) | +2 | — |
| Spot CVD net accumulation (>5%) | +1 | — |
| Large-trade net buy >$100k / 1h | +1 | — |
| (mirrored signs) | — | matching short bonuses |

**A whale-flow rejection (e.g., CVD strong_distribution on a long) overrides numerical score.** This caught LTC on 2026-04-29 — score 9 numerically, but CVD said whales were dumping. Skipped.

### Layer 3 — Multi-TF Visual (`chart-multi` command)

Renders charts on **1d, 4h, 1h, 15m, 5m** with overlays (candles, OBs, FVGs, swings, sweep, EMA20/50, volume). Claude reads each PNG and produces a top-down read.

**Visual rejections most commonly catch:**
- Pump-and-fade structures the SMC scorer reads as "bullish"
- Failed breakouts in the last hour that the scorer missed
- Distribution patterns (lower highs, declining volume on rallies)
- Choppy LTF action with no clean RR
- Setups where price has already traveled to the obvious target

### Pre-Trade Flow (mandatory)

```bash
trade.py setup-scan --top 60 --min-score 9      # Layer 1 candidates
trade.py confluence SYMBOL --whale              # Layer 1 + 2 combined
trade.py chart-multi SYMBOL                     # Layer 3 — render 5 TFs
# Claude reads each PNG, produces visual verdict
# Final integrated verdict: SHIP only if all 3 say yes
```

**Only after all three pass:** `trade.py size --risk X --entry ... --stop ... --target ...` then ask user for go.

---

## Risk Rules (NEVER violate)

- Risk per trade: **1-2% of current account balance**
- Target R:R: **1.5:1** (backtested as optimal vs 2R/3R/5R)
- Stop placement: **at the technical invalidation level**
- **NEVER move stops tighter** mid-trade
- **NEVER move stops wider** to avoid being stopped
- **OCO is mandatory.** Server-side stops only. No "I'll watch and exit manually."
- **Daily-loss circuit breaker is sacred.** 2 losses or 4% drawdown trips it. **Never override** — not for "one last trade", not for an A+ setup, not because the user demands it. Resets at 00:00 UTC. Overriding cost -$5.62 of -$11.03 on 2026-04-29.

---

## Symbol Universe

**Trade alts. BTC/ETH are capital-infeasible at proper risk size below ~$2,000 account.**

### Vetted alts (2mo OOS backtest, score 9 / 1.5R)

| Symbol | Trades | WR | avgR | Status |
|---|---|---|---|---|
| APT | 46 | 54.3% | +0.36 | ✓ Strong edge |
| MASK | 54 | 50.0% | +0.31 | ✓ Strong edge |
| AXS | 47 | 42.6% | +0.09 | ✓ Marginal — smaller size |

### Original training set (6mo backtest, score 9 / 1.5R)

| Symbol | Trades | WR | avgR | Status |
|---|---|---|---|---|
| BTC | 304 | 43.8% | +0.20 | ✓ (capital-gated, ~$1,500+) |
| SOL | 306 | 41.8% | +0.19 | ✓ |
| BNB | 315 | 42.5% | +0.15 | ✓ |
| ETH | 321 | 38.3% | +0.06 | Marginal (capital-gated, ~$1,000+) |

### DO-NOT-TRADE list

| Symbol | Reason |
|---|---|
| **SUI** | OOS 2mo: avgR -0.04 over 41 trades, WR 36.6%. Negative edge. |

For unvetted alts: require liquidity (top ~100 USDT volume) + clean structure + the 3-layer filter agreeing.

---

## Things Tested and Rejected

- **Score 8 bar:** +0.10 avgR vs +0.15 at score 9. Replaced.
- **2R target:** WR 31.4% vs 41.6% at 1.5R. Lower compounding speed for small accounts.
- **Partial-TP 50% @ 1R + BE move:** reduced avgR from +0.10 to +0.03. Capping winners costs more than saving stops returns.
- **3R/4R/5R targets:** same total expectancy as 2R but worse WR (11-22%) → harder psychology, slower compounding.
- **BTC/ETH-only universe:** capital-infeasible below $2k account.

---

## The Toolkit

`trade.py` is the **single source of truth** for all Binance execution. Never write inline Python scripts that talk to Binance — extend the CLI instead.

**Always run CLI commands inside the docker container, never directly with host Python.** The container has the right deps, env vars, and API keys mounted; the host may not. Pattern:

```bash
docker compose exec trade-cli python trade.py <command> [args]
```

Examples below show `trade.py ...` for brevity but every invocation must be wrapped with `docker compose exec trade-cli python` in practice.

### Account / market data
```bash
trade.py env | balance | status
trade.py price SYMBOL
```

### Analysis (3-layer)
```bash
# Layer 1
trade.py analyze SYMBOL --tf 1h
trade.py structure SYMBOL --tf 4h
trade.py liquidity SYMBOL
trade.py order-blocks SYMBOL
trade.py multi-tf SYMBOL
trade.py confluence SYMBOL [--whale]   # Layer 1 alone, or 1+2 combined
trade.py setup-scan --top 60 --min-score 9

# Layer 2 — whale flow
trade.py whale-flow SYMBOL              # raw funding/OI/CVD/large trades
trade.py whale-alert SYMBOL [--force]   # push to #whale Discord
trade.py whale-watch [--top 20]         # scan many, alert on triggers

# Layer 3 — visual
trade.py chart SYMBOL --tf 15m          # single chart
trade.py chart-multi SYMBOL             # 1d/4h/1h/15m/5m bundle
```

### Risk + execution
```bash
trade.py size --risk N --entry X --stop Y --target Z
trade.py buy SYMBOL --usd N --stop X --target Y    # MARKET buy + auto OCO + auto-journal
trade.py protect SYMBOL --stop X --target Y        # attach OCO to existing position
trade.py sell SYMBOL --yes                         # emergency exit
trade.py orders [--symbol X] | cancel SYMBOL ID
```

### Journal
```bash
trade.py journal log ...
trade.py journal close TRADE_ID --outcome WIN|LOSS|BE --exit X --lesson "..."
trade.py journal list | stats
```

### Backtest + daemon
```bash
trade.py backtest SYMBOL --bars N --min-score N --rr N
trade.py backtest-multi --symbols A,B,C --bars N --min-score 9 --rr 1.5
trade.py daemon | monitor SYMBOL
```

### Architecture

```
trade.py        — CLI entry, command routing
analysis.py     — SMC engine (swings, BOS, sweeps, OBs, FVGs, indicators, confluence)
whale_flow.py   — funding, OI, spot CVD, large trades (free Binance APIs)
charting.py     — mplfinance candlestick charts with SMC overlays
backtest.py     — vectorized historical backtest
journal.py      — trade journaling (CSV + Discord)
notify.py       — Discord webhook router (6 channels)
daemon.py       — long-running daemon (Position, PartialTP, Setup scan, WhaleWatch, Daily report)
display.py      — rich terminal output
risk.py         — daily-loss circuit breaker + position sizing
Dockerfile / docker-compose.yml — container deploy
```

---

## Production Deployment

The daemon runs in Docker on a Hetzner VPS:

- Repo: `https://github.com/moazzam-qureshi/trading-cli-tool` (private)
- VPS: `65.108.2.94` (whitelisted on Binance API)
- Access: via Tailscale (`tailscale ssh moazzam-vps`)
- Path: `~/trading-cli-tool`
- Update flow: `git pull && docker compose up -d --build`

### Discord Channel Architecture

6 channels via webhooks in `.env`:

| Channel | env var | What posts there |
|---|---|---|
| #active-trade-signals | `DISCORD_WEBHOOK_SIGNALS` | Trade opened, near stop/target, structure flips, fills, heartbeats |
| #trade-signals | `DISCORD_WEBHOOK_SCANNER` | A+ setup-scan results (every 30 min) |
| #whale-flow | `DISCORD_WEBHOOK_WHALE` | Whale-watch triggers: funding extremes, OI spikes, CVD divergence, large trades |
| #daily-reports | `DISCORD_WEBHOOK_REPORTS` | End-of-day P&L summary at 00:00 UTC |
| #trade-journal | `DISCORD_WEBHOOK_JOURNAL` | Auto-posted post-mortems on close |
| #system-health | `DISCORD_WEBHOOK_SYSTEM` | Daemon online/offline, errors, API failures |

---

## Rules of Engagement (non-negotiable)

1. **Every Binance action goes through `trade.py`, and every invocation runs inside the docker container** (`docker compose exec trade-cli python trade.py ...`). No inline Python scripts. No host-Python invocations. If a feature is missing, **add it to the CLI first**.
2. **Per-trade approval before placing real-money orders** — except for the autonomous agent, which operates under the explicit standing authorization in the **Autonomous Agent Mode** section below.
3. **3-layer filter must all agree.** Numerical 9/10 alone is not enough. Whale or visual rejection = skip.
4. **Risk ≤ 2% per trade.** Use `trade.py size`, never eyeball.
5. **R:R = 1.5:1** at entry.
6. **Stops stay where they are.** (The agent may issue an early market-exit before stop is hit, but never modifies the OCO stop price itself.)
7. **OCO is mandatory.**
8. **Daily-loss circuit breaker stays on.** No overrides, ever. Manual: 2 losses or 4% DD. Agent: -$10/day total realized loss (see Autonomous Agent Mode).
9. **Journal every trade.**
10. **Walk away after entering.** Watching candles for hours produces zero information.
11. **One trade's outcome doesn't validate or invalidate the system.** 50+ trades is the sample size.

---

## Autonomous Agent Mode

The daemon spawns `claude -p` subprocesses to react to live events (score-9 setup-scan hits, whale-flow triggers, position-monitor adverse signals). The agent operates under **standing authorization** with the following hard-coded constraints. These are enforced in code (`claude_agent.py`), not just in prompt — the agent's verdict goes through validation gates before any order is placed.

**What the agent CAN do:**
- Place a `BUY` + OCO order on a symbol that passes all 3 layers (numerical score ≥9, whale-flow not contradicting direction, visual chart read confirms).
- Issue an early market `SELL` on an open position when its analysis reads the setup as invalidated (e.g., whale flow flips, structure breaks down before stop hit).
- Re-enter the same symbol once per day after a stop-out, only if a *fresh* score-9 setup forms (not the same setup that just failed).

**What the agent CANNOT do:**
- Modify the OCO stop price (no trailing, BE-move, tightening, widening).
- Trade with leverage or margin (spot-only).
- Short (spot can't borrow — score-9 SHORT setups are skipped).
- Place a trade if any pre-execution gate fails (see below).
- Override the breaker.

**Pre-execution gates (all must pass, checked in `claude_agent.py`):**
1. **Daily breaker**: cumulative realized P&L for current UTC day ≥ -$10. If breached, agent self-pauses until 00:00 UTC next.
2. **Daily trade cap**: ≤ 5 trades opened today (including re-entries).
3. **No duplicate position**: agent will not open a second position on a symbol that already has open OCO.
4. **Re-entry limit**: ≤ 1 re-entry per symbol per UTC day.
5. **Risk sizing**: `trade.py size --risk 1` (1% of free USDT) — agent never asks for more.
6. **Hard rejects**: shorts skipped, leveraged tokens (`*UPUSDT`, `*DOWNUSDT`) skipped, stablecoin pairs skipped.
7. **R:R floor**: refuses any setup where computed R:R < 1.5.

**Failure-mode behavior:**
- If the agent's subprocess errors out (timeout > 5min, malformed JSON output, chart fetch fails) → SKIP the event. No retry.
- If the verdict is ambiguous → SKIP.
- If trade.py buy/sell command itself fails → log to `#system-health`, SKIP.

**Notification:**
- Every agent verdict (SHIP and SKIP both) posts to `#active-trade-signals` with full reasoning.
- Every agent-placed trade also goes through the standard `trade_opened` and journal pipeline so it appears in `#trade-journal` like a manual trade.

**Operational scope of the agent:**
- Triggers: SetupScannerJob (score ≥9 fresh setups) and WhaleWatchJob (whale triggers on existing or new symbols). PositionMonitorJob will also enqueue an adverse-signal review when an open position's structure flips against it.
- Daily budget: max 5 analyses/trades. Hard cap to prevent runaway loops.
- Tool access for the spawned claude: **Read-only**. Reads pre-rendered chart PNGs and the prompt context. Cannot invoke trade.py buy/sell — those are called by the daemon AFTER validating the verdict.

### Whale alerts on open positions — early-exit doctrine

When a whale alert fires on a symbol where we already hold an open long, the daemon enqueues a `position_review` event. The agent must evaluate whether the whale signal **contradicts** the open trade and decide `EARLY_EXIT` or `SKIP` (= hold).

**Triggers that contradict an open long → strong bias toward `EARLY_EXIT`:**
- `cvd_strong_distribution` / `cvd_net_distribution` — whales selling spot while we're long. This is the LTC-2026-04-29 pattern: pre-trade CVD distribution caught what numerical score 9 missed. Same logic applies post-entry.
- `funding_deeply_positive_retail_long` — retail crowded long, whales positioned to fade. We're now part of "retail."
- `large_net_sell` ≥ $100k/1h — confirmed smart-money distribution in real time.

**Triggers that confirm an open long → `SKIP` (hold):**
- `funding_deeply_negative` / `funding_lean_negative` — shorts crowded, whales fading them.
- `oi_dropping_strongly_positions_closing` — short positions capitulating; bullish for our long.
- `cvd_strong_accumulation` / `cvd_net_accumulation` — whales buying spot.
- `large_net_buy` ≥ $100k/1h.

**Mixed / ambiguous triggers** (e.g., `oi_spiking_strongly` with no CVD context, or `cvd` + `large` pointing opposite ways): use the chart read to break the tie. If LTF structure has flipped against the trade, lean `EARLY_EXIT`; if structure is intact, `SKIP`.

**Rule of thumb:** the same whale-flow signal that would have rejected the entry pre-trade is sufficient to exit it post-trade. We never hold through a contradiction we wouldn't have entered into.

**Output:** for `EARLY_EXIT`, set `decision: "EARLY_EXIT"`, give reasoning that names the specific whale trigger(s) and the chart evidence, and set entry/stop/target/rr to 0. The daemon will market-sell the remainder via `trade.py sell --yes`.

---

## What NOT to Do

- ❌ Don't suggest leverage (spot-only)
- ❌ Don't suggest shorting (no shorts on spot — even if scanner shows score-9 short setups)
- ❌ Don't recommend trades during the user's overnight hours unless asked
- ❌ Don't average down on losers
- ❌ Don't suggest "this time is different" trades that violate the 3-layer filter
- ❌ Don't write theory lessons unless asked
- ❌ Don't deploy code changes without testing locally first
- ❌ Don't push secrets to git — `.env` is the only place for keys / webhooks
- ❌ Don't change framework rules mid-session. Rule changes happen cold, on a different day, after backtest evidence.
- ❌ Don't override the breaker. Period.

---

## Current State (update this section after major changes)

- Account: $150 USDT initial deposit Apr 28 2026; $155.78 free as of Apr 29 2026 EOD.
- Trades to date: 5 (1 BE: SOL; 4 LOSS: MASK, AXS, SUI, APT). Total -$11.03 / -7%.
- 4 of 5 trades had process violations (mid-session rule change, OCO bug, 2× breaker overrides). System edge has not been cleanly tested live yet.
- 2026-04-29 toolkit additions: whale-flow module (`whale_flow.py`), whale Discord channel, charting module (`charting.py`) with multi-TF rendering, daemon `WhaleWatchJob`, 3-layer decision filter codified.
- 2026-04-29 OOS backtest: aggregate +0.19 avgR over 188 trades on MASK/AXS/SUI/APT — system edge confirmed on alts. SUI dropped from universe.
- Daemon: Hetzner VPS via Docker. Container limits cpus:8 / mem:4g. Healthy as of 2026-04-29.
- 2026-04-30 added two pro-trader filters in `analysis.py` to address late-entry / fantasy-target failure modes observed live:
  - **Target reachability** (`target_reachable`) — rejects setups whose 1.5R target sits above the recent N-bar 1H high. **Backtest: avgR +0.148 → +0.182 (+23%) at lookback 160 over 4 OOS symbols.** ON by default (`DAEMON_ENABLE_CEILING=true`, `DAEMON_CEILING_LOOKBACK=160`).
  - **OTE 62% Fib retrace** (`ote_check`) — rejects long entries above the SMC-orthodox 62% retrace of the impulse leg. **Backtest: regression at every threshold tested (0.50/0.62/0.79).** OFF by default (`DAEMON_ENABLE_OTE=false`); code retained for future re-tuning.
- 2026-04-30 added whale-on-open-position → agent early-exit review path. WhaleWatchJob enqueues a `position_review` event whenever any whale alert fires on a held symbol; agent decides EARLY_EXIT vs HOLD using the doctrine in Autonomous Agent Mode § "Whale alerts on open positions."

---

## When in Doubt

- The user's `~/.claude/projects/.../memory/` files have additional context (preferences, prior decisions). Read them when relevant.
- If something seems risky and you're unsure: **ask, don't act.**
- If a tool doesn't exist for what you need: **build it into the CLI**, don't bypass.
- The goal is *consistent profitability over 50+ trades*, not winning the next trade. Optimize for the system, not the outcome.
- **No A+ setups today is a valid outcome.** Pro traders sit out 80% of days. The 3-layer filter rejecting everything is the system working — not failing.
