#!/usr/bin/env python3
"""
EdgeTrader Bot — scheduled paper trader using rule-based signals.

Runs a strategy loop on a schedule:
  - Pulls bars from Alpaca
  - Computes technical indicators (RSI, SMA crossover, volume spike)
  - Places paper trades based on rule-based signals
  - Manages stop-loss and take-profit exits
  - Logs every action to ~/.edgetrader/bot.log and a CSV journal

Usage:
  edgetrader-bot --watchlist AAPL,SPY,NVDA --strategy rsi_reversion
  edgetrader-bot --config strategy.yaml
  edgetrader-bot --dry-run                # Compute signals, don't place orders
  edgetrader-bot --once                   # Run one cycle and exit
"""
import os
import sys
import csv
import time
import json
import logging
import argparse
import configparser
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

try:
    import requests
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError:
    print("Missing dependencies. Install with: pip install requests rich")
    sys.exit(1)

console = Console()

CONFIG_DIR = Path.home() / ".edgetrader"
CONFIG_FILE = CONFIG_DIR / "config.ini"
LOG_FILE = CONFIG_DIR / "bot.log"
JOURNAL_FILE = CONFIG_DIR / "journal.csv"
STATE_FILE = CONFIG_DIR / "bot_state.json"

DATA_BASE = "https://data.alpaca.markets"


# ----- Logging -----
def setup_logging(verbose: bool = False):
    CONFIG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ----- Credentials -----
def load_creds(env: str = "paper"):
    cp = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        console.print("[red]No config found.[/red] Run [cyan]edgetrader configure[/cyan] first.")
        sys.exit(1)
    cp.read(CONFIG_FILE)
    section = f"alpaca_{env}"
    if section not in cp:
        console.print(f"[red]No {env} credentials.[/red]")
        sys.exit(1)
    return {"key": cp[section]["api_key"], "secret": cp[section]["secret_key"], "env": env}


def base_url(env: str) -> str:
    return "https://paper-api.alpaca.markets" if env == "paper" else "https://api.alpaca.markets"


def headers(creds):
    return {
        "APCA-API-KEY-ID": creds["key"],
        "APCA-API-SECRET-KEY": creds["secret"],
        "Content-Type": "application/json",
    }


# ----- Market clock -----
def market_is_open(creds) -> tuple[bool, dict]:
    """Returns (is_open, clock_payload). Source of truth: Alpaca's clock endpoint."""
    r = requests.get(f"{base_url(creds['env'])}/v2/clock", headers=headers(creds), timeout=10)
    r.raise_for_status()
    clock = r.json()
    return clock["is_open"], clock

def sleep_until_next_open(clock: dict, buffer_seconds: int = 60, max_sleep_seconds: int = 3600):
    """
    If the market is closed, sleep until near Alpaca's next_open.
    max_sleep_seconds keeps the bot from sleeping too long without rechecking.
    """
    next_open = clock.get("next_open")

    if not next_open:
        logging.info("Market closed, but no next_open returned. Sleeping 5 minutes.")
        time.sleep(300)
        return

    try:
        next_open_dt = datetime.fromisoformat(next_open.replace("Z", "+00:00"))
        wake_time = next_open_dt - timedelta(seconds=buffer_seconds)

        now = datetime.now(wake_time.tzinfo or timezone.utc)
        seconds_until_wake = int((wake_time - now).total_seconds())

        if seconds_until_wake <= 0:
            return

        sleep_for = min(seconds_until_wake, max_sleep_seconds)

        logging.info(
            f"Market closed. Sleeping {sleep_for} seconds before recheck. "
            f"Next open: {next_open}"
        )

        time.sleep(sleep_for)

    except Exception as e:
        logging.warning(f"Could not parse next_open={next_open}: {e}. Sleeping 5 minutes.")
        time.sleep(300)

# ----- Bars + indicators -----
def fetch_bars(creds, symbol: str, timeframe: str = "5Min", limit: int = 100):
    """Fetch recent bars. Returns list of {t, o, h, l, c, v} dicts (oldest first)."""
    end = datetime.now(timezone.utc) - timedelta(minutes=16)  # IEX 15-min delay buffer
    start = end - timedelta(days=5)
    params = {
        "timeframe": timeframe,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
    }
    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{symbol}/bars",
        headers=headers(creds), params=params, timeout=15
    )
    if r.status_code != 200:
        logging.warning(f"Bars fetch failed for {symbol}: {r.status_code} {r.text[:200]}")
        return []
    return r.json().get("bars", [])


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI. Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # Initial average over the first `period` values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing for the rest
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def volume_zscore(volumes: list[int], period: int = 20) -> Optional[float]:
    """How many standard deviations above the recent average is the latest volume."""
    if len(volumes) < period + 1:
        return None
    sample = volumes[-period - 1:-1]  # exclude current bar
    avg = sum(sample) / len(sample)
    var = sum((v - avg) ** 2 for v in sample) / len(sample)
    sd = var ** 0.5
    if sd == 0:
        return 0
    return (volumes[-1] - avg) / sd


# ----- Signal definition -----
@dataclass
class Signal:
    symbol: str
    action: str            # "buy", "sell", "hold"
    price: float
    reason: str
    rsi_val: Optional[float] = None
    sma_fast: Optional[float] = None
    sma_slow: Optional[float] = None
    vol_z: Optional[float] = None


# ----- Strategies -----
def strategy_rsi_reversion(symbol: str, bars: list) -> Signal:
    """
    Mean reversion: buy when RSI < 30 (oversold) with positive volume confirmation,
    sell when RSI > 70 (overbought).
    Conservative — only fires on extremes.
    """
    if len(bars) < 30:
        return Signal(symbol, "hold", 0, "insufficient bars")

    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    last = closes[-1]

    r = rsi(closes, 14)
    vz = volume_zscore(volumes, 20)
    fast = sma(closes, 5)
    slow = sma(closes, 20)

    if r is None:
        return Signal(symbol, "hold", last, "RSI not ready", r, fast, slow, vz)

    if r < 30:
        if vz is None or vz > 0.5:
            return Signal(symbol, "buy", last,
                          f"RSI={r:.1f} oversold, vol_z={vz:.2f}" if vz is not None
                          else f"RSI={r:.1f} oversold",
                          r, fast, slow, vz)
        # Oversold but volume hasn't confirmed — hold rather than catch a falling knife
        return Signal(symbol, "hold", last,
                      f"RSI={r:.1f} oversold but vol_z={vz:.2f} too low",
                      r, fast, slow, vz)
    if r > 70:
        return Signal(symbol, "sell", last, f"RSI={r:.1f} overbought", r, fast, slow, vz)
    return Signal(symbol, "hold", last, f"RSI={r:.1f} neutral", r, fast, slow, vz)


def strategy_sma_crossover(symbol: str, bars: list) -> Signal:
    """
    Trend-following: buy when fast SMA crosses above slow, sell when it crosses below.
    Looks at the last two bars to detect the cross.
    """
    if len(bars) < 25:
        return Signal(symbol, "hold", 0, "insufficient bars")

    closes = [b["c"] for b in bars]
    last = closes[-1]
    prev_closes = closes[:-1]

    fast_now = sma(closes, 5)
    slow_now = sma(closes, 20)
    fast_prev = sma(prev_closes, 5)
    slow_prev = sma(prev_closes, 20)

    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return Signal(symbol, "hold", last, "SMA not ready")

    # Bullish cross: fast was below, now above
    if fast_prev <= slow_prev and fast_now > slow_now:
        return Signal(symbol, "buy", last,
                      f"5SMA crossed above 20SMA ({fast_now:.2f}>{slow_now:.2f})",
                      None, fast_now, slow_now)
    # Bearish cross
    if fast_prev >= slow_prev and fast_now < slow_now:
        return Signal(symbol, "sell", last,
                      f"5SMA crossed below 20SMA ({fast_now:.2f}<{slow_now:.2f})",
                      None, fast_now, slow_now)
    return Signal(symbol, "hold", last,
                  f"5SMA={fast_now:.2f} 20SMA={slow_now:.2f}",
                  None, fast_now, slow_now)


STRATEGIES = {
    "rsi_reversion": strategy_rsi_reversion,
    "sma_crossover": strategy_sma_crossover,
}


# ----- Position + order helpers -----
def get_positions(creds) -> dict:
    """Return {symbol: position_dict}."""
    r = requests.get(f"{base_url(creds['env'])}/v2/positions", headers=headers(creds), timeout=10)
    if r.status_code != 200:
        return {}
    return {p["symbol"]: p for p in r.json()}


def get_account(creds) -> dict:
    r = requests.get(f"{base_url(creds['env'])}/v2/account", headers=headers(creds), timeout=10)
    r.raise_for_status()
    return r.json()


def place_order(creds, symbol: str, qty: int, side: str, order_type: str = "market", **kw):
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": kw.get("tif", "day"),
    }
    if "limit_price" in kw:
        payload["limit_price"] = str(kw["limit_price"])
    if "stop_price" in kw:
        payload["stop_price"] = str(kw["stop_price"])
    r = requests.post(
        f"{base_url(creds['env'])}/v2/orders",
        headers=headers(creds), json=payload, timeout=15
    )
    if r.status_code not in (200, 201):
        logging.error(f"Order failed: {r.status_code} {r.text}")
        return None
    return r.json()


# ----- Risk + sizing -----
def position_size(account: dict, price: float, risk_pct: float = 0.02, max_per_trade: float = 0.02) -> int:
    """
    Size position so that one trade is at most `max_per_trade` fraction of equity.
    risk_pct is reserved for future stop-loss-aware sizing.
    """
    equity = float(account["equity"])
    max_dollars = equity * max_per_trade
    qty = int(max_dollars / price)
    return max(qty, 0)


def is_equity_symbol(symbol: str) -> bool:
    """Basic guard: the bot is intended to trade simple equity/ETF symbols only."""
    return symbol.isalpha() and len(symbol) <= 5


def equity_positions(positions: dict) -> dict:
    return {s: p for s, p in positions.items() if is_equity_symbol(s)}


def total_exposure_pct(positions: dict, equity: float) -> float:
    """Current gross equity exposure as a percentage of account equity."""
    if equity <= 0:
        return 100.0

    total = 0.0
    for pos in equity_positions(positions).values():
        try:
            total += abs(float(pos.get("market_value", 0)))
        except (TypeError, ValueError):
            continue

    return (total / equity) * 100


def day_pnl_pct(account: dict) -> float:
    equity = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity") or equity or 0)

    if last_equity <= 0:
        return 0.0

    return ((equity - last_equity) / last_equity) * 100


def today_entry_count() -> int:
    """Count today's entries from the CSV journal."""
    if not JOURNAL_FILE.exists():
        return 0

    today = datetime.now().date()
    count = 0

    try:
        with open(JOURNAL_FILE, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("action") not in {"ENTER", "ENTER_DRY"}:
                    continue

                ts = row.get("timestamp", "")
                try:
                    if datetime.fromisoformat(ts).date() == today:
                        count += 1
                except ValueError:
                    continue
    except Exception as e:
        logging.warning(f"Could not read journal for daily trade count: {e}")

    return count


def minutes_until_market_close(clock: dict) -> Optional[float]:
    next_close = clock.get("next_close")

    if not next_close:
        return None

    try:
        close_dt = datetime.fromisoformat(next_close.replace("Z", "+00:00"))
        now = datetime.now(close_dt.tzinfo or timezone.utc)
        return (close_dt - now).total_seconds() / 60
    except Exception as e:
        logging.warning(f"Could not parse next_close={next_close}: {e}")
        return None


def build_entry_block_reasons(
    positions: dict,
    account: dict,
    clock: dict,
    max_open_positions: int,
    max_total_exposure: float,
    max_daily_trades: int,
    max_daily_loss: float,
    no_new_trades_minutes_before_close: int,
) -> list[str]:
    """Return reasons why new entries should be blocked. Exits are still allowed."""
    equity = float(account.get("equity", 0) or 0)
    open_count = len(equity_positions(positions))
    exposure = total_exposure_pct(positions, equity)
    entries_today = today_entry_count()
    pnl = day_pnl_pct(account)
    mins_to_close = minutes_until_market_close(clock)

    reasons = []

    if max_daily_loss > 0 and pnl <= -max_daily_loss:
        reasons.append(f"daily loss limit hit ({pnl:.2f}% <= -{max_daily_loss:.2f}%)")

    if max_open_positions > 0 and open_count >= max_open_positions:
        reasons.append(f"max open positions reached ({open_count}/{max_open_positions})")

    max_total_exposure_pct = max_total_exposure * 100
    if max_total_exposure > 0 and exposure >= max_total_exposure_pct:
        reasons.append(f"max exposure reached ({exposure:.2f}% >= {max_total_exposure_pct:.2f}%)")

    if max_daily_trades > 0 and entries_today >= max_daily_trades:
        reasons.append(f"daily trade limit reached ({entries_today}/{max_daily_trades})")

    if (
        no_new_trades_minutes_before_close > 0
        and mins_to_close is not None
        and mins_to_close <= no_new_trades_minutes_before_close
    ):
        reasons.append(
            f"too close to market close ({mins_to_close:.1f}m <= "
            f"{no_new_trades_minutes_before_close}m)"
        )

    return reasons


# ----- Journal -----
def journal_write(row: dict):
    new_file = not JOURNAL_FILE.exists()
    with open(JOURNAL_FILE, "a", newline="") as f:
        fields = ["timestamp", "action", "symbol", "qty", "price", "reason",
                  "rsi", "sma_fast", "sma_slow", "vol_z", "order_id", "dry_run"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        # Fill missing keys with empty
        w.writerow({k: row.get(k, "") for k in fields})


# ----- State (cooldowns to prevent over-trading the same name) -----
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ----- Main loop -----
def run_cycle(creds, watchlist: list[str], strategy_name: str,
              dry_run: bool, max_per_trade: float, take_profit_pct: float,
              stop_loss_pct: float, max_open_positions: int,
              max_total_exposure: float, max_daily_trades: int,
              max_daily_loss: float, no_new_trades_minutes_before_close: int):
    """One full pass: check market, fetch signals, manage positions, place new entries."""
    strategy = STRATEGIES[strategy_name]
    state = load_state()
    cooldowns = state.get("cooldowns", {})  # {symbol: iso_timestamp}

    is_open, clock = market_is_open(creds)
    if not is_open:
        next_open = clock.get("next_open", "")
        logging.info(f"Market closed. Next open: {next_open}")
        return

    account = get_account(creds)
    equity = float(account["equity"])
    positions = get_positions(creds)
    logging.info(f"Equity ${equity:,.2f} | {len(positions)} open positions | strategy={strategy_name}")

    # Step 1: manage existing positions — check for take-profit / stop-loss
    for symbol, pos in positions.items():
        # Skip option positions — bot trades equities only here
        if not is_equity_symbol(symbol):
            continue
        pnl_pct = float(pos["unrealized_plpc"]) * 100
        qty = abs(int(float(pos["qty"])))

        if pnl_pct >= take_profit_pct:
            reason = f"take-profit hit ({pnl_pct:+.2f}% >= {take_profit_pct}%)"
            logging.info(f"EXIT {symbol}: {reason}")
            if not dry_run:
                order = place_order(creds, symbol, qty, "sell")
                journal_write({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "action": "EXIT_TP", "symbol": symbol, "qty": qty,
                    "price": pos["current_price"], "reason": reason,
                    "order_id": order["id"] if order else "", "dry_run": False,
                })
            else:
                journal_write({"timestamp": datetime.now().isoformat(timespec="seconds"),
                               "action": "EXIT_TP_DRY", "symbol": symbol, "qty": qty,
                               "price": pos["current_price"], "reason": reason, "dry_run": True})

        elif pnl_pct <= -stop_loss_pct:
            reason = f"stop-loss hit ({pnl_pct:+.2f}% <= -{stop_loss_pct}%)"
            logging.info(f"EXIT {symbol}: {reason}")
            if not dry_run:
                order = place_order(creds, symbol, qty, "sell")
                journal_write({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "action": "EXIT_SL", "symbol": symbol, "qty": qty,
                    "price": pos["current_price"], "reason": reason,
                    "order_id": order["id"] if order else "", "dry_run": False,
                })

    # Re-fetch positions in case we just exited
    if not dry_run:
        positions = get_positions(creds)

    entry_block_reasons = build_entry_block_reasons(
        positions, account, clock,
        max_open_positions, max_total_exposure, max_daily_trades,
        max_daily_loss, no_new_trades_minutes_before_close,
    )

    if entry_block_reasons:
        logging.info("New entries blocked: " + "; ".join(entry_block_reasons))

    # Step 2: scan watchlist for entry signals
    table = Table(title=f"Signals — {strategy_name}", box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("Symbol", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Action")
    table.add_column("Reason", style="dim")

    for symbol in watchlist:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        # Cooldown: don't re-enter the same name within an hour
        cooldown_until = cooldowns.get(symbol)
        if cooldown_until:
            try:
                until = datetime.fromisoformat(cooldown_until)
                if datetime.now() < until:
                    table.add_row(symbol, "—", "[dim]cooldown[/dim]",
                                  f"until {until.strftime('%H:%M')}")
                    continue
            except ValueError:
                pass

        bars = fetch_bars(creds, symbol)
        if not bars:
            table.add_row(symbol, "—", "[dim]no data[/dim]", "bars unavailable")
            continue

        sig = strategy(symbol, bars)

        display_action = sig.action.upper()
        display_reason = sig.reason
        blocked_for_symbol = []

        if sig.action == "buy" and symbol not in positions and entry_block_reasons:
            blocked_for_symbol = entry_block_reasons
            display_action = "BLOCKED"
            display_reason = f"{sig.reason} | blocked: {'; '.join(blocked_for_symbol)}"

        action_color = {"BUY": "green", "SELL": "red", "HOLD": "dim", "BLOCKED": "yellow"}[display_action]
        table.add_row(symbol, f"${sig.price:.2f}",
                      f"[{action_color}]{display_action}[/{action_color}]", display_reason)

        # Only enter on BUY signals; exits are managed via TP/SL above.
        if sig.action == "buy" and symbol not in positions:
            if blocked_for_symbol:
                logging.info(f"SKIP {symbol}: entry blocked — {'; '.join(blocked_for_symbol)}")
                continue

            qty = position_size(account, sig.price, max_per_trade=max_per_trade)
            if qty <= 0:
                logging.warning(f"{symbol}: sizing returned 0 qty, skipping")
                continue

            logging.info(f"ENTER {symbol}: BUY {qty} @ ~${sig.price:.2f} — {sig.reason}")
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "action": "ENTER" if not dry_run else "ENTER_DRY",
                "symbol": symbol, "qty": qty, "price": sig.price, "reason": sig.reason,
                "rsi": sig.rsi_val, "sma_fast": sig.sma_fast, "sma_slow": sig.sma_slow,
                "vol_z": sig.vol_z, "dry_run": dry_run,
            }

            if not dry_run:
                order = place_order(creds, symbol, qty, "buy")
                row["order_id"] = order["id"] if order else ""
                cooldowns[symbol] = (datetime.now() + timedelta(hours=1)).isoformat()

            journal_write(row)

            # Update local state so later symbols in the same cycle respect exposure/position/trade caps.
            positions[symbol] = {
                "symbol": symbol,
                "qty": str(qty),
                "market_value": str(qty * sig.price),
            }
            entry_block_reasons = build_entry_block_reasons(
                positions, account, clock,
                max_open_positions, max_total_exposure, max_daily_trades,
                max_daily_loss, no_new_trades_minutes_before_close,
            )
        elif sig.action == "sell" and symbol in positions:
            qty = abs(int(float(positions[symbol]["qty"])))
            logging.info(f"EXIT {symbol}: signal sell")
            if not dry_run:
                order = place_order(creds, symbol, qty, "sell")
                journal_write({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "action": "EXIT_SIGNAL", "symbol": symbol, "qty": qty,
                    "price": sig.price, "reason": sig.reason,
                    "order_id": order["id"] if order else "", "dry_run": False,
                })

    console.print(table)
    state["cooldowns"] = cooldowns
    save_state(state)


def main():
    p = argparse.ArgumentParser(
        prog="edgetrader-bot",
        description="Scheduled paper trading bot — rule-based signals, risk-managed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  edgetrader-bot --watchlist SPY,QQQ,AAPL --once --dry-run
    Single cycle, no real orders. Best way to test signals.

  edgetrader-bot --watchlist SPY,QQQ --strategy rsi_reversion --interval 300
    Run forever, scan every 5 minutes during market hours.

  edgetrader-bot --watchlist NVDA,AMD,SMCI --strategy sma_crossover \\
    --max-per-trade 0.05 --take-profit 2 --stop-loss 1
    Aggressive trend-following on chip stocks, 5% per trade max,
    2% TP / 1% SL per position.
        """,
    )
    p.add_argument("--watchlist", default="SPY,QQQ,AAPL,NVDA,MSFT",
                   help="Comma-separated tickers to scan")
    p.add_argument("--strategy", choices=list(STRATEGIES.keys()), default="rsi_reversion")
    p.add_argument("--interval", type=int, default=300, help="Seconds between cycles (default 300=5min)")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="Compute signals without placing orders")
    p.add_argument("--env", choices=["paper", "live"], default="paper",
                   help="Default paper. Live requires explicit confirm.")
    p.add_argument("--max-per-trade", type=float, default=0.02,
                   help="Max fraction of equity per position (0.02 = 2%%)")
    p.add_argument("--take-profit", type=float, default=0.75,
                   help="Exit position when up N%% (default 0.75)")
    p.add_argument("--stop-loss", type=float, default=0.75,
                   help="Exit position when down N%% (default 0.75)")
    p.add_argument("--max-open-positions", type=int, default=2,
                   help="Maximum number of open equity positions (default 2)")
    p.add_argument("--max-total-exposure", type=float, default=0.10,
                   help="Maximum total equity exposure as fraction of account equity (default 0.10 = 10%%)")
    p.add_argument("--max-daily-trades", type=int, default=3,
                   help="Maximum new entries per day (default 3)")
    p.add_argument("--max-daily-loss", type=float, default=1.0,
                   help="Block new entries if daily account P&L is <= -N%% (default 1.0)")
    p.add_argument("--no-new-trades-minutes-before-close", type=int, default=30,
                   help="Block new entries this many minutes before market close (default 30)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)

    if args.env == "live" and not args.dry_run:
        console.print("[bold red]⚠ LIVE TRADING MODE — real money[/bold red]")
        confirm = input("Type 'I UNDERSTAND' to proceed: ")
        if confirm != "I UNDERSTAND":
            console.print("Aborted.")
            sys.exit(0)

    creds = load_creds(args.env)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]

    mode = "DRY RUN" if args.dry_run else f"LIVE-{args.env.upper()}"
    console.print(f"[bold green]⬡ EdgeTrader Bot[/bold green] — {mode} | "
                  f"strategy={args.strategy} | watchlist={','.join(watchlist)}")
    console.print(f"[dim]Logs: {LOG_FILE}  Journal: {JOURNAL_FILE}[/dim]\n")

    if args.once:
        try:
            run_cycle(creds, watchlist, args.strategy, args.dry_run,
                      args.max_per_trade, args.take_profit, args.stop_loss,
                      args.max_open_positions, args.max_total_exposure,
                      args.max_daily_trades, args.max_daily_loss,
                      args.no_new_trades_minutes_before_close)
        except Exception as e:
            logging.exception(f"Cycle failed: {e}")
        return

    # Loop forever
    cycle = 0
    while True:
        cycle += 1
        console.print(f"\n[dim]── Cycle {cycle} @ {datetime.now().strftime('%H:%M:%S')} ──[/dim]")

        try:
            is_open, clock = market_is_open(creds)

            if not is_open:
                next_open = clock.get("next_open", "")
                logging.info(f"Market closed. Next open: {next_open}")
                sleep_until_next_open(clock)
                continue

            run_cycle(creds, watchlist, args.strategy, args.dry_run,
                      args.max_per_trade, args.take_profit, args.stop_loss,
                      args.max_open_positions, args.max_total_exposure,
                      args.max_daily_trades, args.max_daily_loss,
                      args.no_new_trades_minutes_before_close)

            time.sleep(args.interval)

        except KeyboardInterrupt:
            console.print("\n[dim]Bot stopped.[/dim]")
            break
        except Exception as e:
            logging.exception(f"Cycle failed: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
