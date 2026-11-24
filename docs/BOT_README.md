# EdgeTrader Bot README

`edgetrader_bot.py` is a scheduled, rule-based Alpaca trading bot. It is designed to scan a watchlist, compute simple technical signals, manage existing positions, and optionally place equity orders.

Use `--dry-run` first. The bot can place orders when dry-run is disabled.

## Start Here

The bot uses the same credentials file as the manual CLI:

```text
~/.edgetrader/config.ini
```

Configure it first:

```bash
python edgetrader.py configure
```

Install dependencies:

```bash
pip install requests rich
```

Run one dry-run cycle:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --once --dry-run
```

## What One Cycle Does

Each cycle:

1. Loads Alpaca credentials and `bot_state.json`.
2. Checks Alpaca's market clock.
3. Stops early if the market is closed.
4. Checks every order the bot placed earlier and applies any fills to the bot's holdings.
5. Reads the account, positions and open orders. If any of these reads fails, the cycle stops before placing an order.
6. Applies stop-loss, take-profit and force-close exits to shares the bot bought itself.
7. Pulls the latest IEX stock bars and skips any symbol whose newest bar is stale.
8. Computes watchlist signals with the selected strategy.
9. Blocks new entries if risk limits are hit.
10. Places buy or sell orders unless `--dry-run` is enabled.
11. Writes logs, journal rows, and state.

## Running The Bot

Single dry-run cycle:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --once --dry-run
```

Single live paper cycle:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --once
```

Continuous paper run every five minutes:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --interval 300
```

Continuous dry-run:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --interval 300 --dry-run
```

Live environment:

```bash
python edgetrader_bot.py --env live --watchlist SPY,QQQ --dry-run
```

If `--env live` is used without `--dry-run`, the bot requires this exact confirmation:

```text
I UNDERSTAND
```

## Strategies

### `rsi_reversion`

Mean-reversion strategy.

Behavior:

- buys when RSI is below 30 and volume confirmation is acceptable
- sells when RSI is above 70
- holds otherwise

Example:

```bash
python edgetrader_bot.py --strategy rsi_reversion --watchlist SPY,QQQ,AAPL --once --dry-run
```

### `sma_crossover`

Trend-following strategy.

Behavior:

- buys when 5-period SMA crosses above 20-period SMA
- sells when 5-period SMA crosses below 20-period SMA
- holds otherwise

Example:

```bash
python edgetrader_bot.py --strategy sma_crossover --watchlist NVDA,AMD,SMCI --once --dry-run
```

## Risk Controls

The bot has several entry controls. Exits are still allowed when new entries are blocked.

### Position Size

```bash
--max-per-trade 0.02
```

Maximum fraction of equity allocated to a new position. Default is `0.02`, or 2 percent.

### Take Profit

```bash
--take-profit 0.75
```

Exit when unrealized P&L reaches this percentage gain. Default is `0.75`.

### Stop Loss

```bash
--stop-loss 0.75
```

Exit when unrealized P&L reaches this percentage loss. Default is `0.75`.

### Maximum Open Positions

```bash
--max-open-positions 2
```

Blocks new entries when the number of open equity positions in the account, plus symbols with a pending bot buy, reaches the limit.

### Maximum Total Exposure

```bash
--max-total-exposure 0.10
```

Blocks a new entry when gross equity exposure in the account, plus pending bot buys, plus the new order itself, would exceed this fraction of account equity. Default is `0.10`, or 10 percent.

### Maximum Daily Trades

```bash
--max-daily-trades 3
```

Blocks new entries after this many entries for the current date. A live run counts `ENTER` and `ENTER_UNCERTAIN` journal rows; a dry run also counts `ENTER_DRY`. Rejected orders don't count. If the journal can't be read, new entries are blocked.

### Maximum Daily Loss

```bash
--max-daily-loss 1.0
```

Blocks new entries if account day P&L is less than or equal to negative this percentage.

### No New Trades Near Close

```bash
--no-new-trades-minutes-before-close 30
```

Blocks new entries when the market is within this many minutes of close.

## Full Example

```bash
python edgetrader_bot.py \
  --watchlist SPY,QQQ,AAPL,NVDA \
  --strategy rsi_reversion \
  --interval 300 \
  --max-per-trade 0.02 \
  --take-profit 0.75 \
  --stop-loss 0.75 \
  --max-open-positions 2 \
  --max-total-exposure 0.10 \
  --max-daily-trades 3 \
  --max-daily-loss 1.0 \
  --no-new-trades-minutes-before-close 30
```

PowerShell line continuation:

```powershell
python .\edgetrader_bot.py `
  --watchlist SPY,QQQ,AAPL,NVDA `
  --strategy rsi_reversion `
  --interval 300 `
  --dry-run
```

## Runtime Files

The bot writes files under:

```text
~/.edgetrader/
```

Files:

- `bot.log`: Python logging output
- `journal.csv`: trade and dry-run journal rows
- `bot_state.json`: cooldown timestamps, orders the bot is still tracking, and the shares the bot holds (`holdings`). It is written atomically. If it is corrupt, the bot stops instead of starting over with no record of its orders.
- `config.ini`: shared credentials file

Journal columns:

- `timestamp`
- `action`
- `symbol`
- `qty`
- `signal_price`
- `fill_price`
- `order_status`
- `reason`
- `rsi`
- `sma_fast`
- `sma_slow`
- `vol_z`
- `order_id`
- `dry_run`

## Orders And Ownership

Every bot order carries a `client_order_id` starting with `etbot-`, and is recorded in `bot_state.json` before it is sent.

- **Accepted** orders are journaled as `ENTER` or `EXIT_*_SUBMITTED`, then tracked until they finish. Fills are journaled as `ENTER_FILLED` or `EXIT_*` (for example `EXIT_TP`); other endings as `ENTER_CANCELED`, `EXIT_SL_EXPIRED` and so on.
- **Rejected** orders (a 4xx from Alpaca) are journaled as `ENTER_REJECTED` or `EXIT_*_REJECTED`. They don't start a cooldown or count toward the daily trade limit.
- **Uncertain** orders (a timeout, connection error or 5xx) are journaled as `ENTER_UNCERTAIN` or `EXIT_*_UNCERTAIN`. The next cycle looks them up by `client_order_id`. Until they are found, or are confirmed missing after two minutes (`*_NOT_FOUND`), new entries are blocked.

Exits only sell shares the bot bought, capped at the long position in the account. The bot never sells positions you opened yourself, and it never sells into a short. If the account no longer holds a symbol the bot thinks it owns, the bot drops its record and journals `HOLDING_DROPPED`.

Positions opened by versions of the bot before ownership tracking are not in `holdings`, so the bot doesn't manage them. Close them yourself.

The one-hour cooldown after an entry only blocks new entries in that symbol. Sell signals are still acted on.

## Market Data

The bot fetches bars from Alpaca's stock bars endpoint:

```text
https://data.alpaca.markets/v2/stocks/{symbol}/bars
```

It requests:

- timeframe: `5Min`
- feed: `iex`
- adjustment: `raw`
- a recent five-day window, newest bars first (`sort=desc`), so the 100-bar limit keeps the latest bars
- an end timestamp buffered by 16 minutes for delayed IEX data

Bars are sorted oldest first before indicators are computed. If the newest bar starts more than 15 minutes before the end timestamp, the symbol is skipped for that cycle. This also skips the first few minutes after the open, while the newest bars are still from the previous session.

## Symbol Scope

The bot is intended for simple equity and ETF symbols only. It skips non-equity-looking symbols when managing positions.

The basic symbol guard is:

```text
symbol.isalpha() and len(symbol) <= 5
```

## Implementation Notes

Key sections in `edgetrader_bot.py`:

- logging setup: file and stdout logging
- credential loading: shared `~/.edgetrader/config.ini`
- market clock helpers: open/closed checks and sleep until next open
- indicator helpers: RSI, SMA, volume z-score
- strategy functions: `strategy_rsi_reversion`, `strategy_sma_crossover`
- risk helpers: position sizing, exposure, daily trade count, daily loss, close-time block
- journal helpers: CSV append with fixed columns
- state helpers: JSON state with cooldowns, tracked orders and holdings
- order tracking: submission outcomes, reconciliation by `client_order_id`, and fill accounting
- `run_cycle`: the main unit of bot work
- `main`: argument parsing and continuous loop

## Safety Notes

- Start with `--dry-run`.
- Keep `--env paper` until behavior is understood.
- Live mode can place real orders.
- The bot uses market orders by default.
- The strategies are simple examples, not a complete trading system.

