# EdgeTrader CLI README

`edgetrader.py` is the manual trading CLI. It connects to Alpaca for account data, market data, orders, positions, options snapshots, and a live terminal dashboard.

## Start Here

Install dependencies:

```bash
pip install requests rich
```

Configure credentials:

```bash
python edgetrader.py configure
```

If installed with `install.sh`, use:

```bash
edgetrader configure
```

The setup wizard asks for:

- environment: `paper` or `live`
- Alpaca API key ID
- Alpaca secret key
- optional Anthropic API key for `signals`

Credentials are stored in `~/.edgetrader/config.ini`.

## Environments

The CLI uses the default environment saved during `configure`. Override it with `--env`:

```bash
edgetrader account --env paper
edgetrader account --env live
```

Paper base URL:

```text
https://paper-api.alpaca.markets
```

Live base URL:

```text
https://api.alpaca.markets
```

Market data base URL:

```text
https://data.alpaca.markets
```

## Commands

### Account

Shows account status, buying power, cash, portfolio value, day P&L, day trade count, PDT flag, and options level.

```bash
edgetrader account
edgetrader account --env live
```

### Quote

Shows the latest trade, bid/ask, daily bar, volume, and previous close for a stock symbol.

```bash
edgetrader quote AAPL
```

### Positions

Lists open positions and unrealized P&L.

```bash
edgetrader positions
```

### Orders

Lists recent orders.

```bash
edgetrader orders
edgetrader orders --status open
edgetrader orders --status closed --limit 50
```

Allowed statuses:

- `open`
- `closed`
- `all`

### Options Chain

Fetches option snapshots and renders calls and puts around available strikes.

```bash
edgetrader chain SPY
edgetrader chain SPY --expiry 2026-05-17
edgetrader chain SPY --strikes 5
```

`--strikes 5` filters contracts to within 5 percent of the current stock price.

### Buy, Sell, And Order

The `buy` and `sell` commands are shortcuts. The `order` command requires `--side`.

Equity examples:

```bash
edgetrader buy AAPL --qty 10 --type market
edgetrader sell AAPL --qty 10 --type market
edgetrader order AAPL --side buy --qty 10 --type limit --limit 185
```

Supported order types:

- `market`
- `limit`
- `stop`
- `stop_limit`

Supported time in force values:

- `day`
- `gtc`
- `ioc`
- `fok`
- `opg`
- `cls`

Option order example:

```bash
edgetrader buy SPY --qty 1 --option-type call --strike 580 --expiry 2026-05-17 --type limit --limit 2.50
```

For options, all three fields are required:

- `--expiry`
- `--strike`
- `--option-type`

If only some option fields are provided, the CLI exits before reading credentials or placing an order.

Use `-y` or `--yes` to skip confirmation:

```bash
edgetrader buy AAPL --qty 1 --type market --yes
```

### Close Positions

Close one position:

```bash
edgetrader close AAPL
```

Close all positions:

```bash
edgetrader close --all
```

Use `--yes` to skip confirmation for a single symbol:

```bash
edgetrader close AAPL --yes
```

### Cancel Orders

Cancel one order:

```bash
edgetrader cancel <order-id>
```

Cancel all open orders:

```bash
edgetrader cancel --all
```

### Signals

Runs an AI-powered screener through Anthropic's Messages API with web search.

```bash
edgetrader signals AAPL,TSLA,SPY --strategy momentum
```

Strategies accepted by this command:

- `momentum`
- `mean_reversion`
- `options_flow`
- `breakout`

Requirements:

- Anthropic API key in `~/.edgetrader/config.ini`, or
- `ANTHROPIC_API_KEY` environment variable

The command asks the model for strict JSON, extracts JSON from the response, validates the expected schema, then renders a signal table.

### Watch

Starts a live terminal dashboard that refreshes account and position data.

```bash
edgetrader watch
edgetrader watch --interval 10
```

Stop it with `Ctrl+C`.

## Direct Script Usage

If the CLI is not installed on `PATH`, run:

```bash
python edgetrader.py account
python edgetrader.py quote AAPL
```

On Windows PowerShell, set UTF-8 output first if help rendering fails:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python .\edgetrader.py --help
```

## Implementation Notes

Key sections in `edgetrader.py`:

- config management: creates and reads `~/.edgetrader/config.ini`
- formatters: money, percent, and color formatting for Rich
- command handlers: `cmd_account`, `cmd_quote`, `cmd_positions`, `cmd_orders`, `cmd_chain`, `cmd_order`, `cmd_close`, `cmd_cancel`, `cmd_signals`, `cmd_watch`
- CLI parser: builds all subcommands in `main()`

## Safety Notes

- Live mode submits real orders.
- Order commands show a confirmation prompt unless `--yes` is used.
- Live mode displays an additional warning before submission.
- The CLI does not calculate whether a trade is suitable for your account.

