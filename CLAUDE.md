# CLAUDE.md — Operating Context for trade-cli

This file is auto-loaded by Claude Code in every session opened in this repo. It is the **single source of truth** for what we're doing, why, and how.

---

## The Goal

**Grow $150 → $10,000.**

- Started: $150 USDT on Binance Spot, April 2026
- $5k savings buffer exists but is **not for revenge trades** — only deploy after the system is proven
- Realistic timeline: 6-18 months at 5-10 trades/week
- This is the user's "tuition fee" for learning to trade

Milestones: $150 → $300 → $500 → $1k → $2.5k → $5k → $10k

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

**Trades, not lectures.** The user is the type who learns from real trades + post-mortems, NOT from theory dumps.

Don't:
- ❌ Write multi-page strategy lessons (they'll be ignored)
- ❌ Run abstract tutorials on RSI / MACD / etc.
- ❌ Suggest paper trading — the user wants real-money lessons

Do:
- ✅ Build tools that let Claude operate at pro-trader level
- ✅ Use those tools on real trades
- ✅ Write a clean post-mortem after every trade (win or lose) covering setup, reasoning, what played out, lesson
- ✅ Introduce concepts *in context* of an actual trade — "we entered here because of X order block"

---

## The Trading Framework

**Smart Money Concepts (SMC) is the chosen framework.** Indicators are confirmation only, never primary signal.

### A+ Setup Recipe (scored by `confluence` command)

| Confluence | Score | Required |
|---|---|---|
| HTF (4H) trend aligned with direction | ⭐⭐⭐ | Yes |
| MTF (1H) liquidity sweep before entry | ⭐⭐⭐ | High value |
| Price inside an order block | ⭐⭐ | Bonus |
| Price inside an unfilled FVG | ⭐⭐ | Bonus |
| LTF (15m) structure shift in direction | ⭐⭐ | High value |
| Volume spike on entry candle | ⭐ | Bonus |
| LTF RSI in healthy zone (not extreme) | ⭐ | Bonus |

**Minimum 8/10 to enter. Below 8 = skip.** No exceptions, even for "pretty" setups.

### Risk Rules (NEVER violate)

- Risk per trade: **1-2% of current account balance**
- Minimum R:R: **2:1** (target reward must be ≥ 2× the stop distance)
- Stop placement: **at the technical invalidation level** (e.g., below the swept low)
- **NEVER move stops tighter** mid-trade. Stop is at invalidation. If it hits, the setup was wrong, period.
- **NEVER move stops wider** to avoid being stopped out. That's revenge trading.

---

## The Toolkit

`trade.py` is the **single source of truth** for all Binance execution. Never write inline Python scripts that talk to Binance — extend the CLI instead.

### Commands

```bash
# Account / market data
trade.py env                              # check testnet vs live
trade.py balance                          # USDT + holdings
trade.py price SYMBOL                     # current ticker
trade.py status                           # full account view

# Analysis (SMC + indicators)
trade.py analyze SYMBOL --tf 1h           # full dashboard
trade.py structure SYMBOL --tf 4h         # swings, BOS, CHoCH
trade.py liquidity SYMBOL                 # equal H/L, sweeps
trade.py order-blocks SYMBOL              # OBs + FVGs
trade.py multi-tf SYMBOL                  # 1D/4H/1H/15m
trade.py confluence SYMBOL                # A+ setup grader (must be ≥ 8 to trade)
trade.py setup-scan --top 30 --min-score 8  # find candidates

# Risk + execution
trade.py size --risk 2 --entry X --stop Y --target Z   # position sizer
trade.py buy SYMBOL --usd N --stop X --target Y        # MARKET buy + auto OCO
trade.py protect SYMBOL --stop X --target Y            # attach OCO to existing position
trade.py sell SYMBOL --yes                             # emergency exit (cancels OCO + market sell)
trade.py orders [--symbol X]                           # list open orders
trade.py cancel SYMBOL ORDER_ID

# Journal (the learning loop)
trade.py journal log ...                  # log entry with reasoning
trade.py journal close TRADE_ID --outcome WIN|LOSS|BE --exit X --lesson "..."
trade.py journal list
trade.py journal stats

# Daemon (use this in Docker on VPS)
trade.py daemon                            # long-running monitor
trade.py monitor SYMBOL                    # watch a single position interactively
```

All commands accept `--json` for machine output; default is rich terminal output.

### Architecture

```
trade.py        — CLI entry, command routing
analysis.py     — SMC engine (swings, BOS, sweeps, OBs, FVGs, indicators, confluence)
journal.py      — trade journaling (CSV + markdown post-mortems)
notify.py       — Discord webhook router (5 channels)
daemon.py       — long-running daemon (Position monitor, Setup scanner, Daily report)
display.py      — rich-based terminal output
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

5 channels via webhooks in `.env`:

| Channel | env var | What posts there |
|---|---|---|
| #active-trade-signals | `DISCORD_WEBHOOK_SIGNALS` | Trade opened, near stop/target, structure flips, fills, heartbeats |
| #trade-signals | `DISCORD_WEBHOOK_SCANNER` | A+ setup-scan results (every 30 min) |
| #daily-reports | `DISCORD_WEBHOOK_REPORTS` | End-of-day P&L summary at 00:00 UTC |
| #trade-journal | `DISCORD_WEBHOOK_JOURNAL` | Auto-posted post-mortems on close |
| #system-health | `DISCORD_WEBHOOK_SYSTEM` | Daemon online/offline, errors, API failures |

---

## Rules of Engagement (Important)

These are non-negotiable habits:

1. **Every Binance action goes through `trade.py`.** No inline Python scripts. If a feature is missing, **add it to the CLI first**, then use it.
2. **Per-trade approval before placing real-money orders.** Always.
3. **Confluence ≥ 8/10 to enter.** No exceptions.
4. **Risk ≤ 2% per trade.** Use `trade.py size` to calculate, never eyeball.
5. **R:R ≥ 2:1** at entry. If math doesn't work, skip.
6. **Stops stay where they are.** Don't move tighter, don't move wider, don't cancel hoping price recovers.
7. **OCO is mandatory.** No "I'll watch the chart and exit manually." Server-side stops are non-negotiable.
8. **Journal every trade.** Entry: reasoning. Exit: outcome + lesson.
9. **Walk away after entering.** Watching 15m candles for hours produces zero information and lots of bad decisions.
10. **One trade's outcome doesn't validate or invalidate the system.** The system is the process. Edge shows up in 50+ trades.

---

## What NOT to Do

- ❌ Don't suggest leverage (we're spot-only on Binance)
- ❌ Don't suggest shorting (no shorts on spot)
- ❌ Don't recommend trades during the user's overnight hours unless they explicitly ask
- ❌ Don't average down on losers (martingale = blowup)
- ❌ Don't suggest "this time is different" trades that violate the 8/10 confluence rule
- ❌ Don't write theory lessons unless asked — show, don't tell
- ❌ Don't deploy code changes without testing locally first
- ❌ Don't push secrets to git — `.env` is the only place for keys

---

## Current State (update this section after major changes)

- Account starting balance: $150 USDT (initial deposit, ~Apr 28 2026)
- First trade: **RIFUSDT** — entered as a "trend chase" before the toolkit existed. Closed near breakeven (-$0.05 in fees). Lesson: never trade without confluence scoring first.
- Second trade: **SOLUSDT long** — entered Apr 28 2026 at $83.74 with confluence 9/10 (later rescored 10/10 after entry as volume confirmed). Stop $82.95, target $85.50, R:R 2.07:1.
- Daemon: deployed to Hetzner VPS via Docker on Apr 28 2026.

---

## When in Doubt

- The user's `~/.claude/projects/.../memory/` files have additional context (goals, learning style, collaboration model) — read those first if available.
- If something seems risky and you're unsure: **ask, don't act.**
- If a tool doesn't exist for what you need: **build it into the CLI**, don't bypass.
- The goal is *consistent profitability over 50+ trades*, not winning the next trade. Optimize for the system, not the outcome.
