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

1. Loads Alpaca credentials.
2. Checks Alpaca's market clock.
3. Stops early if the market is closed.
4. Pulls recent IEX stock bars with a delay buffer.
5. Reads current account and positions.
6. Applies stop-loss and take-profit exits to existing equity positions.
7. Computes watchlist signals with the selected strategy.
8. Blocks new entries if risk limits are hit.
9. Places buy or sell orders unless `--dry-run` is enabled.
10. Writes logs, journal rows, and cooldown state.

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

Blocks new entries when the number of open equity positions reaches the limit.

### Maximum Total Exposure

```bash
--max-total-exposure 0.10
```

Blocks new entries when gross equity exposure reaches this fraction of account equity. Default is `0.10`, or 10 percent.

### Maximum Daily Trades

```bash
--max-daily-trades 3
```

Blocks new entries after this many `ENTER` or `ENTER_DRY` journal rows for the current date.

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
- `bot_state.json`: cooldown timestamps
- `config.ini`: shared credentials file

Journal columns:

- `timestamp`
- `action`
- `symbol`
- `qty`
- `price`
- `reason`
- `rsi`
- `sma_fast`
- `sma_slow`
- `vol_z`
- `order_id`
- `dry_run`

## Market Data

The bot fetches bars from Alpaca's stock bars endpoint:

```text
https://data.alpaca.markets/v2/stocks/{symbol}/bars
```

It requests:

- timeframe: `5Min`
- feed: `iex`
- adjustment: `raw`
- a recent five-day window
- an end timestamp buffered by 16 minutes for delayed IEX data

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
- state helpers: JSON cooldown state
- `run_cycle`: the main unit of bot work
- `main`: argument parsing and continuous loop

## Safety Notes

- Start with `--dry-run`.
- Keep `--env paper` until behavior is understood.
- Live mode can place real orders.
- The bot uses market orders by default.
- The strategies are simple examples, not a complete trading system.

