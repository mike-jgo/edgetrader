# EdgeTrader

EdgeTrader is a small Python trading toolkit for Alpaca. It has two entry points:

- `edgetrader.py`: an interactive command-line trading assistant for account lookup, quotes, positions, orders, options chains, manual order placement, AI signals, and a live terminal dashboard.
- `edgetrader_bot.py`: a scheduled rule-based trading bot that scans a watchlist, computes indicators, places risk-limited equity trades, and journals activity.

This project is trading software. Use paper trading first. Live mode can place real orders with real money.

## Repository Layout

```text
.
|-- edgetrader.py       # Main CLI for manual trading workflows
|-- edgetrader_bot.py   # Scheduled strategy bot
|-- install.sh          # Bash installer for the edgetrader CLI only
|-- README.md           # Project overview
`-- docs/
    |-- CLI_README.md   # Manual CLI usage
    `-- BOT_README.md   # Bot usage and risk controls
```

## Requirements

- Python 3.8+
- Alpaca account and API keys
- Python packages:

```bash
pip install requests rich
```

The `signals` command also needs an Anthropic API key, either configured through `edgetrader configure` or exposed as `ANTHROPIC_API_KEY`.

## Install

On Linux, macOS, Git Bash, or WSL:

```bash
chmod +x install.sh
./install.sh
```

The installer:

- installs `requests` and `rich` with `python3 -m pip install --user`
- symlinks `edgetrader.py` to `~/.local/bin/edgetrader`
- does not install `edgetrader_bot.py`

If `~/.local/bin` is not on your `PATH`, add it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

On Windows PowerShell, install dependencies and run scripts directly:

```powershell
pip install requests rich
python .\edgetrader.py --help
python .\edgetrader_bot.py --help
```

If PowerShell cannot print the CLI help because of a Unicode console encoding issue, run:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python .\edgetrader.py --help
```

## First Run

Configure Alpaca credentials:

```bash
edgetrader configure
```

Or, if running directly:

```bash
python edgetrader.py configure
```

Credentials are stored in:

```text
~/.edgetrader/config.ini
```

The config file may contain:

```ini
[default]
env = paper

[alpaca_paper]
api_key = ...
secret_key = ...

[alpaca_live]
api_key = ...
secret_key = ...

[anthropic]
api_key = ...
```

## Common Manual CLI Commands

```bash
edgetrader account
edgetrader quote AAPL
edgetrader positions
edgetrader orders --status all --limit 20
edgetrader chain SPY --strikes 5
edgetrader buy AAPL --qty 10 --type market
edgetrader sell AAPL --qty 10 --type market
edgetrader close AAPL
edgetrader cancel --all
edgetrader watch
```

Use live credentials explicitly:

```bash
edgetrader account --env live
```

See [docs/CLI_README.md](docs/CLI_README.md) for full command usage.

## Common Bot Commands

Run one dry-run cycle:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --once --dry-run
```

Run continuously every five minutes:

```bash
python edgetrader_bot.py --watchlist SPY,QQQ,AAPL --strategy rsi_reversion --interval 300
```

See [docs/BOT_README.md](docs/BOT_README.md) for strategies, risk controls, logs, and journal output.

## How The Codebase Works

Both scripts use direct HTTP requests against Alpaca APIs with shared helper functions for credentials, base URLs, and request headers.

`edgetrader.py` is command-driven. It builds an `argparse` CLI, maps each subcommand to a handler, calls Alpaca account/trading/data endpoints, and renders terminal tables with Rich.

`edgetrader_bot.py` is loop-driven. Each cycle checks whether the market is open, fetches delayed IEX bars, computes rule-based signals, manages existing positions using stop-loss and take-profit rules, applies entry risk limits, optionally places orders, and writes logs/journal files.

## Runtime Files

EdgeTrader writes local state under:

```text
~/.edgetrader/
```

Important files:

- `config.ini`: Alpaca and optional Anthropic credentials
- `bot.log`: bot logs
- `journal.csv`: bot trade journal
- `bot_state.json`: bot cooldown state

## Safety Notes

- Paper trading is the default path and should be used before live trading.
- `edgetrader.py` supports both paper and live trading.
- `edgetrader_bot.py` defaults to paper, but can use `--env live`. If live and not dry-run, it requires typing `I UNDERSTAND`.
- The bot is intended for simple equity and ETF symbols only.
- AI signals are informational and are not financial advice.

