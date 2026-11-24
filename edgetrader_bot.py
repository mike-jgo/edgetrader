#!/usr/bin/env python3
"""
EdgeTrader Bot - scheduled paper trader using rule-based signals.

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
import uuid
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from dataclasses import dataclass, asdict

LOCAL_TZ = ZoneInfo("Asia/Manila")
MARKET_TZ = ZoneInfo("America/New_York")  # trading days follow the US session, not Manila midnight


def now_local() -> datetime:
    """Current time in the bot's local (Philippine) timezone."""
    return datetime.now(LOCAL_TZ)

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

CLIENT_ORDER_PREFIX = "etbot-"
BAR_TIMEFRAME = "5Min"
BAR_SIZE = timedelta(minutes=5)         # must match BAR_TIMEFRAME
MAX_BAR_AGE = timedelta(minutes=15)     # newest bar older than this (3 x 5Min) is stale
UNCERTAIN_ORDER_GRACE = timedelta(minutes=2)  # wait before deciding an uncertain order never arrived
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}

JOURNAL_FIELDS = [
    "timestamp", "action", "symbol", "qty", "signal_price", "fill_price",
    "order_status", "reason", "rsi", "sma_fast", "sma_slow", "vol_z",
    "order_id", "dry_run",
]
OLD_JOURNAL_FIELDS = [
    "timestamp", "action", "symbol", "qty", "price", "reason",
    "rsi", "sma_fast", "sma_slow", "vol_z", "order_id", "dry_run",
]


# ----- Logging -----
class _LocalTzFormatter(logging.Formatter):
    """Formatter that renders timestamps in LOCAL_TZ (Asia/Manila)."""

    def converter(self, timestamp):  # type: ignore[override]
        return datetime.fromtimestamp(timestamp, tz=LOCAL_TZ).timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=LOCAL_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logging(verbose: bool = False):
    CONFIG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = _LocalTzFormatter("%(asctime)s [%(levelname)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    # Reset handlers so reconfiguration in tests / repeated calls is clean.
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)


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

def _format_clock_ts_local(ts: str) -> str:
    """Render an Alpaca clock timestamp (with timezone offset) in Asia/Manila."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
    except Exception:
        return ts


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
            f"Next open: {_format_clock_ts_local(next_open)}"
        )

        time.sleep(sleep_for)

    except Exception as e:
        logging.warning(f"Could not parse next_open={next_open}: {e}. Sleeping 5 minutes.")
        time.sleep(300)

# ----- Bars + indicators -----
def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_bars(creds, symbol: str, limit: int = 100):
    """
    Fetch the most recent completed bars. Returns list of {t, o, h, l, c, v} dicts (oldest first).
    The IEX feed is real-time, so bars are requested up to now and the bar still forming is dropped.
    Returns [] when the request fails or the newest bar is older than MAX_BAR_AGE,
    so signals are never computed from stale data.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    params = {
        "timeframe": BAR_TIMEFRAME,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": limit,
        "sort": "desc",  # newest first, so `limit` keeps the latest bars, not the oldest
        "adjustment": "raw",
        "feed": "iex",
    }
    try:
        r = requests.get(
            f"{DATA_BASE}/v2/stocks/{symbol}/bars",
            headers=headers(creds), params=params, timeout=15
        )
    except requests.RequestException as e:
        logging.warning(f"Bars fetch error for {symbol}: {e}")
        return []
    if r.status_code != 200:
        logging.warning(f"Bars fetch failed for {symbol}: {r.status_code} {r.text[:200]}")
        return []
    try:
        bars = sorted(r.json().get("bars") or [], key=lambda b: _parse_ts(b["t"]))
        bars = [b for b in bars if _parse_ts(b["t"]) + BAR_SIZE <= end]  # drop the forming bar
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logging.warning(f"Bars for {symbol} could not be read: {e}")
        return []
    if not bars:
        return []
    newest = _parse_ts(bars[-1]["t"])
    if end - newest > MAX_BAR_AGE:
        logging.info(f"Bars for {symbol} are stale (newest starts {newest.isoformat()}), skipping")
        return []
    return bars


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
    Conservative - only fires on extremes.
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
        # Oversold but volume hasn't confirmed - hold rather than catch a falling knife
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
    """Return {symbol: position_dict}. Raises if the positions can't be read."""
    r = requests.get(f"{base_url(creds['env'])}/v2/positions", headers=headers(creds), timeout=10)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_account(creds) -> dict:
    r = requests.get(f"{base_url(creds['env'])}/v2/account", headers=headers(creds), timeout=10)
    r.raise_for_status()
    return r.json()


OPEN_ORDERS_LIMIT = 500


def get_open_orders(creds) -> list:
    """Return list of open (working) orders from Alpaca. Raises if they can't be read in full."""
    r = requests.get(
        f"{base_url(creds['env'])}/v2/orders",
        headers=headers(creds),
        params={"status": "open", "limit": OPEN_ORDERS_LIMIT, "nested": "true"},
        timeout=10,
    )
    r.raise_for_status()
    orders = r.json() or []
    if len(orders) >= OPEN_ORDERS_LIMIT:
        raise RuntimeError(f"{len(orders)} open orders returned; the list may be incomplete")
    return orders


def get_order(creds, order_id: str) -> Optional[dict]:
    """Fetch a single order by id; returns None on failure."""
    if not order_id:
        return None
    try:
        r = requests.get(
            f"{base_url(creds['env'])}/v2/orders/{order_id}",
            headers=headers(creds), timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logging.warning(f"get_order({order_id}) error: {e}")
        return None


def get_order_by_client_id(creds, client_order_id: str) -> Optional[dict]:
    """
    Look up an order by the client_order_id the bot gave it.
    Returns None if the broker has no such order; raises on any other failure.
    """
    r = requests.get(
        f"{base_url(creds['env'])}/v2/orders:by_client_order_id",
        headers=headers(creds), params={"client_order_id": client_order_id}, timeout=10,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def place_order(creds, symbol: str, qty, side: str, client_order_id: str,
                order_type: str = "market", **kw) -> tuple[str, Optional[dict]]:
    """
    Submit an order. Returns (outcome, order):
      "accepted"  - the broker returned the order
      "rejected"  - the broker refused it (4xx), so nothing was placed
      "uncertain" - timeout, connection error or 5xx: the order may or may not exist,
                    so it is reconciled by client_order_id before the bot acts again
    """
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": kw.get("tif", "day"),
        "client_order_id": client_order_id,
    }
    if "limit_price" in kw:
        payload["limit_price"] = str(kw["limit_price"])
    if "stop_price" in kw:
        payload["stop_price"] = str(kw["stop_price"])
    try:
        r = requests.post(
            f"{base_url(creds['env'])}/v2/orders",
            headers=headers(creds), json=payload, timeout=15
        )
    except requests.RequestException as e:
        logging.error(
            f"Order submission uncertain: symbol={symbol} side={side} qty={qty} "
            f"client_order_id={client_order_id} error={e}"
        )
        return "uncertain", None
    if r.status_code >= 500:
        logging.error(
            f"Order submission uncertain: symbol={symbol} side={side} qty={qty} "
            f"client_order_id={client_order_id} {r.status_code} {r.text[:200]}"
        )
        return "uncertain", None
    if r.status_code not in (200, 201):
        logging.error(f"Order rejected: {r.status_code} {r.text}")
        return "rejected", None
    try:
        order = r.json()
    except ValueError:
        logging.error(f"Order response unreadable: client_order_id={client_order_id} {r.text[:200]}")
        return "uncertain", None
    fill_price = order_fill_price(order)
    status = order_status(order)
    order_id = order.get("id", "")
    if fill_price:
        logging.info(
            f"ORDER submitted: symbol={symbol} side={side} qty={qty} "
            f"status={status or 'unknown'} order_id={order_id} "
            f"filled_avg_price={fill_price}"
        )
    else:
        logging.info(
            f"ORDER submitted but not filled yet: symbol={symbol} side={side} qty={qty} "
            f"status={status or 'unknown'} order_id={order_id} filled_avg_price unavailable"
        )
    return "accepted", order


def order_fill_price(order: dict) -> str:
    """Return the broker-reported average fill price when available."""
    if not order:
        return ""
    return str(order.get("filled_avg_price") or "")


def order_status(order: dict) -> str:
    """Return the broker-reported order status when available."""
    if not order:
        return ""
    return str(order.get("status") or "")


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


def exposure_dollars(positions: dict) -> float:
    """Current gross equity exposure across the account, in dollars."""
    total = 0.0
    for pos in equity_positions(positions).values():
        try:
            total += abs(float(pos.get("market_value", 0)))
        except (TypeError, ValueError):
            continue
    return total


def entry_usage(positions: dict, pending: dict) -> tuple[int, float]:
    """
    Open position count and gross exposure in dollars, counting pending buys
    ({symbol: reserved dollars}) as if they had already filled.
    """
    open_count = len(set(equity_positions(positions)) | set(pending))
    return open_count, exposure_dollars(positions) + sum(pending.values())


def day_pnl_pct(account: dict) -> float:
    equity = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity") or equity or 0)

    if last_equity <= 0:
        return 0.0

    return ((equity - last_equity) / last_equity) * 100


def today_entry_count(include_dry: bool) -> int:
    """
    Count today's entries from the CSV journal: submitted and uncertain orders,
    plus dry-run entries when `include_dry`. Raises if the journal can't be read.
    """
    if not JOURNAL_FILE.exists():
        return 0

    actions = {"ENTER", "ENTER_UNCERTAIN"} | ({"ENTER_DRY"} if include_dry else set())
    # "Today" is the New York trading date: the session crosses midnight in Manila.
    today = now_local().astimezone(MARKET_TZ).date()
    count = 0

    with open(JOURNAL_FILE, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("action") not in actions:
                continue

            ts = row.get("timestamp", "")
            try:
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=LOCAL_TZ)  # journal rows are written in Manila time
                if parsed.astimezone(MARKET_TZ).date() == today:
                    count += 1
            except ValueError:
                continue

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


def is_force_close_window(clock: dict, minutes: int) -> bool:
    """
    True when the market is open and minutes until regular close is <= `minutes`.
    Used to force-close positions and block new entries late in the session.
    """
    if minutes <= 0:
        return False
    if not clock.get("is_open"):
        return False
    mins = minutes_until_market_close(clock)
    if mins is None:
        return False
    return mins <= minutes


def build_entry_block_reasons(
    open_count: int,
    exposure: float,
    account: dict,
    clock: dict,
    max_open_positions: int,
    max_total_exposure: float,
    max_daily_trades: int,
    max_daily_loss: float,
    no_new_trades_minutes_before_close: int,
    dry_run: bool,
) -> list[str]:
    """
    Return reasons why new entries should be blocked. Exits are still allowed.
    `open_count` and `exposure` (dollars) should already include pending buys.
    """
    equity = float(account.get("equity", 0) or 0)
    exposure_pct = (exposure / equity) * 100 if equity > 0 else 100.0
    pnl = day_pnl_pct(account)
    mins_to_close = minutes_until_market_close(clock)

    reasons = []

    if max_daily_loss > 0 and pnl <= -max_daily_loss:
        reasons.append(f"daily loss limit hit ({pnl:.2f}% <= -{max_daily_loss:.2f}%)")

    if max_open_positions > 0 and open_count >= max_open_positions:
        reasons.append(f"max open positions reached ({open_count}/{max_open_positions})")

    max_total_exposure_pct = max_total_exposure * 100
    if max_total_exposure > 0 and exposure_pct >= max_total_exposure_pct:
        reasons.append(f"max exposure reached ({exposure_pct:.2f}% >= {max_total_exposure_pct:.2f}%)")

    if max_daily_trades > 0:
        try:
            entries_today = today_entry_count(include_dry=dry_run)
        except Exception as e:
            logging.warning(f"Could not read journal for daily trade count: {e}")
            reasons.append("daily trade count unavailable (journal unreadable)")
        else:
            if entries_today >= max_daily_trades:
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
EXIT_BASE = {
    "tp": "EXIT_TP",
    "sl": "EXIT_SL",
    "signal": "EXIT_SIGNAL",
    "force": "EXIT_FORCE_CLOSE",
}


def journal_action(kind: str, outcome: str) -> str:
    """
    Journal action for an order event. `kind` is "entry" or a key of EXIT_BASE.
      entries: ENTER (submitted), ENTER_FILLED, ENTER_REJECTED, ENTER_UNCERTAIN, ENTER_CANCELED, ...
      exits:   EXIT_TP_SUBMITTED, EXIT_TP (filled), EXIT_TP_REJECTED, EXIT_TP_UNCERTAIN, ...
    """
    if kind == "entry":
        return "ENTER" if outcome == "submitted" else f"ENTER_{outcome.upper()}"
    base = EXIT_BASE[kind]
    return base if outcome == "filled" else f"{base}_{outcome.upper()}"


def journal_write(row: dict):
    CONFIG_DIR.mkdir(exist_ok=True)

    if JOURNAL_FILE.exists():
        try:
            with open(JOURNAL_FILE, newline="") as existing:
                header = next(csv.reader(existing), [])
        except OSError as e:
            logging.warning(f"Could not inspect journal header: {e}")
            header = []

        if header and header != JOURNAL_FIELDS:
            stamp = now_local().strftime("%Y%m%d-%H%M%S")
            archive = JOURNAL_FILE.with_name(f"journal.v1.{stamp}.csv")
            suffix = 1
            while archive.exists():
                archive = JOURNAL_FILE.with_name(f"journal.v1.{stamp}.{suffix}.csv")
                suffix += 1
            JOURNAL_FILE.rename(archive)
            if header == OLD_JOURNAL_FIELDS:
                logging.info(f"Archived old journal schema to {archive}")
            else:
                logging.warning(f"Archived incompatible journal schema to {archive}")

    new_file = not JOURNAL_FILE.exists() or JOURNAL_FILE.stat().st_size == 0
    with open(JOURNAL_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        if new_file:
            w.writeheader()
        # Fill missing keys with empty
        w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})


def journal_order(rec: dict, outcome: str, order: Optional[dict] = None,
                  note: str = "", extra: Optional[dict] = None):
    """Write a journal row for an event on a tracked order."""
    row = {
        "timestamp": now_local().isoformat(timespec="seconds"),
        "action": journal_action(rec["kind"], outcome),
        "symbol": rec["symbol"], "qty": rec["qty"],
        "signal_price": rec["signal_price"],
        "fill_price": order_fill_price(order),
        "order_status": order_status(order) or outcome,
        "reason": rec["reason"] + (f" | {note}" if note else ""),
        "order_id": rec.get("order_id", ""),
        "dry_run": False,
    }
    row.update(extra or {})
    journal_write(row)


# ----- State -----
# bot_state.json holds:
#   cooldowns: {symbol: iso_timestamp} - no new entry in the symbol before this time
#   orders:    {client_order_id: record} - bot orders not yet finished at the broker
#   holdings:  {symbol: {"qty", "cost"}} - shares the bot bought and still holds
def load_state() -> dict:
    """Load bot state. A corrupt file raises rather than forgetting open orders and holdings."""
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"{STATE_FILE} is not valid JSON ({e}). Fix or move it before running the bot again."
            ) from e
    state.setdefault("cooldowns", {})
    state.setdefault("orders", {})
    state.setdefault("holdings", {})
    return state


def save_state(state: dict):
    """Write state atomically so a crash mid-write can't corrupt it."""
    CONFIG_DIR.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


# ----- Order tracking -----
def dec(value) -> Decimal:
    return Decimal(str(value or 0))


def _apply_fill(state: dict, symbol: str, side: str, qty: Decimal, notional: Decimal):
    """Add bought shares to, or remove sold shares from, the bot's holdings."""
    h = state["holdings"].get(symbol, {"qty": "0", "cost": "0"})
    held, cost = dec(h["qty"]), dec(h["cost"])
    if side == "buy":
        held += qty
        cost += notional
    else:
        avg = cost / held if held > 0 else Decimal(0)
        held -= qty
        cost -= avg * qty
    if held > 0:
        state["holdings"][symbol] = {"qty": str(held), "cost": str(cost)}
    else:
        state["holdings"].pop(symbol, None)


def apply_order_update(state: dict, client_order_id: str, order: dict):
    """Fold a broker snapshot of a tracked order into state: apply new fills, close out finished orders."""
    rec = state["orders"][client_order_id]
    rec["order_id"] = order.get("id") or rec.get("order_id", "")
    rec["status"] = "pending"

    filled = dec(order.get("filled_qty"))
    notional = filled * dec(order.get("filled_avg_price"))
    new_qty = filled - dec(rec["filled_qty"])
    if new_qty > 0:
        _apply_fill(state, rec["symbol"], rec["side"], new_qty, notional - dec(rec["filled_notional"]))
        rec["filled_qty"], rec["filled_notional"] = str(filled), str(notional)

    status = order_status(order).lower()
    if status in TERMINAL_ORDER_STATUSES:
        logging.info(
            f"ORDER {status}: symbol={rec['symbol']} side={rec['side']} qty={rec['qty']} "
            f"filled_qty={filled} order_id={rec['order_id']}"
        )
        journal_order(rec, status, order)
        del state["orders"][client_order_id]


def reconcile_orders(creds, state: dict) -> list[str]:
    """
    Check every order the bot is tracking with the broker and apply what happened.
    Returns problems that must block new entries: orders that couldn't be checked,
    or that the broker doesn't know about yet.
    """
    problems = []
    for cid, rec in list(state["orders"].items()):
        try:
            order = get_order_by_client_id(creds, cid)
        except requests.RequestException as e:
            problems.append(f"{rec['symbol']} {rec['side']} order {cid} could not be checked ({e})")
            continue

        if order is not None:
            apply_order_update(state, cid, order)
            continue

        age = now_local() - datetime.fromisoformat(rec["submitted_at"])
        if rec["status"] != "pending" and age >= UNCERTAIN_ORDER_GRACE:
            logging.warning(f"{rec['symbol']} order {cid} never reached the broker; forgetting it")
            journal_order(rec, "not_found")
            del state["orders"][cid]
        else:
            problems.append(f"{rec['symbol']} {rec['side']} order {cid} not found at the broker yet")
    save_state(state)
    return problems


def submit_tracked_order(creds, state: dict, symbol: str, qty, side: str, kind: str,
                         reason: str, signal_price, extra: Optional[dict] = None) -> str:
    """
    Submit an order and track it in state until the broker reports it finished.
    The record is saved before sending, so a crash mid-submit is reconciled next cycle.
    Returns "accepted", "rejected" or "uncertain".
    """
    cid = f"{CLIENT_ORDER_PREFIX}{uuid.uuid4().hex}"
    rec = {
        "symbol": symbol, "side": side, "qty": str(qty), "kind": kind,
        "reason": reason, "signal_price": float(signal_price),
        "status": "submitting", "order_id": "",
        "submitted_at": now_local().isoformat(),
        "filled_qty": "0", "filled_notional": "0",
    }
    state["orders"][cid] = rec
    save_state(state)

    outcome, order = place_order(creds, symbol, qty, side, cid)
    if outcome == "rejected":
        del state["orders"][cid]
        journal_order(rec, "rejected", extra=extra)
    elif outcome == "uncertain":
        rec["status"] = "uncertain"
        journal_order(rec, "uncertain",
                      note="reconciling with the broker before trading this symbol again",
                      extra=extra)
    else:
        rec["status"] = "pending"
        rec["order_id"] = order.get("id", "")
        journal_order(rec, "submitted", order, extra=extra)
        apply_order_update(state, cid, order)
    save_state(state)
    return outcome


def pending_buy_notional(state: dict) -> dict:
    """{symbol: dollars} still reserved by the bot's unfilled buy orders."""
    out = {}
    for rec in state["orders"].values():
        if rec["side"] != "buy":
            continue
        unfilled = dec(rec["qty"]) - dec(rec["filled_qty"])
        if unfilled > 0:
            out[rec["symbol"]] = out.get(rec["symbol"], 0.0) + float(unfilled) * float(rec["signal_price"])
    return out


def busy_sides(open_orders: list, state: dict) -> dict:
    """{symbol: {sides}} with a working order, from the broker or the bot's own tracking."""
    busy = {}
    for o in open_orders:
        busy.setdefault((o.get("symbol") or "").upper(), set()).add((o.get("side") or "").lower())
    for rec in state["orders"].values():
        busy.setdefault(rec["symbol"], set()).add(rec["side"])
    return busy


def sellable_qty(state: dict, positions: dict, symbol: str) -> Decimal:
    """Shares the bot may sell: its own holding, capped by the long position the account has."""
    h = state["holdings"].get(symbol)
    pos = positions.get(symbol)
    if not h or not pos:
        return Decimal(0)
    return max(min(dec(h["qty"]), dec(pos.get("qty"))), Decimal(0))


def cooldown_until(cooldowns: dict, symbol: str) -> Optional[datetime]:
    """When the symbol's entry cooldown ends, or None if it isn't cooling down."""
    raw = cooldowns.get(symbol)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Compare in the same wall-clock zone we wrote them in.
    now_cmp = now_local() if until.tzinfo else datetime.now()
    return until if now_cmp < until else None


# ----- Main loop -----
def _submit_exit(creds, state: dict, symbol: str, qty, kind: str, base_reason: str,
                 price, dry_run: bool, busy: dict):
    """
    Submit an exit (sell) order for shares the bot owns, and journal it.
    Skips submission if there is already a working sell order for `symbol`.
    """
    sides = busy.setdefault(symbol, set())
    if dry_run:
        journal_write({
            "timestamp": now_local().isoformat(timespec="seconds"),
            "action": journal_action(kind, "dry"), "symbol": symbol, "qty": str(qty),
            "signal_price": price, "fill_price": "", "order_status": "dry_run",
            "reason": base_reason, "dry_run": True,
        })
        sides.add("sell")
        return

    if "sell" in sides:
        logging.info(f"SKIP exit {symbol}: open sell order already exists")
        journal_write({
            "timestamp": now_local().isoformat(timespec="seconds"),
            "action": "EXIT_SKIPPED_OPEN_ORDER", "symbol": symbol, "qty": str(qty),
            "signal_price": price, "fill_price": "", "order_status": "skipped_open_order",
            "reason": f"{base_reason} | open sell order already exists",
            "dry_run": False,
        })
        return

    sides.add("sell")
    submit_tracked_order(creds, state, symbol, qty, "sell", kind, base_reason, price)


def run_cycle(creds, watchlist: list[str], strategy_name: str,
              dry_run: bool, max_per_trade: float, take_profit_pct: float,
              stop_loss_pct: float, max_open_positions: int,
              max_total_exposure: float, max_daily_trades: int,
              max_daily_loss: float, no_new_trades_minutes_before_close: int,
              force_close_minutes_before_close: int):
    """One full pass: check market, reconcile orders, manage the bot's holdings, place new entries."""
    strategy = STRATEGIES[strategy_name]
    state = load_state()
    cooldowns = state["cooldowns"]  # {symbol: iso_timestamp}

    is_open, clock = market_is_open(creds)
    if not is_open:
        next_open = clock.get("next_open", "")
        logging.info(f"Market closed. Next open: {_format_clock_ts_local(next_open)}")
        return

    # Fold in what happened to orders from earlier cycles before reading positions.
    unresolved = [] if dry_run else reconcile_orders(creds, state)

    # Any failed read below raises and ends the cycle before an order is placed.
    account = get_account(creds)
    equity = float(account["equity"])
    positions = get_positions(creds)
    busy = busy_sides(get_open_orders(creds), state)
    holdings = state["holdings"]
    logging.info(
        f"Equity ${equity:,.2f} | {len(positions)} account positions | "
        f"{len(holdings)} bot holdings | {len(state['orders'])} tracked orders | "
        f"strategy={strategy_name}"
    )

    force_close = is_force_close_window(clock, force_close_minutes_before_close)
    if force_close:
        mins = minutes_until_market_close(clock)
        mins_str = f"{mins:.1f}m" if mins is not None else "?"
        logging.info(
            f"force close window active "
            f"({mins_str} <= {force_close_minutes_before_close}m to close)"
        )

    # Step 1: manage the bot's own holdings - take-profit / stop-loss / force-close
    for symbol, h in list(holdings.items()):
        owned = dec(h["qty"])
        avg = dec(h["cost"]) / owned
        pos = positions.get(symbol)
        broker_qty = dec(pos.get("qty")) if pos else Decimal(0)

        if broker_qty <= 0:
            if busy.get(symbol):
                continue  # an order is still working; reconcile next cycle
            logging.warning(
                f"{symbol}: bot records {owned} shares but the account holds {broker_qty}; "
                f"dropping the record"
            )
            if not dry_run:
                journal_write({
                    "timestamp": now_local().isoformat(timespec="seconds"),
                    "action": "HOLDING_DROPPED", "symbol": symbol, "qty": str(owned),
                    "reason": f"account holds {broker_qty}", "dry_run": False,
                })
                del holdings[symbol]
            continue

        if broker_qty < owned:
            logging.warning(
                f"{symbol}: bot records {owned} shares but the account holds {broker_qty}; "
                f"managing {broker_qty}"
            )
            if not dry_run:
                holdings[symbol] = {"qty": str(broker_qty), "cost": str(avg * broker_qty)}
        qty = min(owned, broker_qty)

        price = dec(pos.get("current_price"))
        if avg <= 0 or price <= 0:
            logging.warning(f"{symbol}: no usable entry or current price, skipping exit checks")
            continue
        pnl_pct = float((price - avg) / avg * 100)

        if force_close:
            kind = "force"
            reason = (
                f"force close window ({pnl_pct:+.2f}% pnl, "
                f"<= {force_close_minutes_before_close}m to close)"
            )
        elif pnl_pct >= take_profit_pct:
            kind = "tp"
            reason = f"take-profit hit ({pnl_pct:+.2f}% >= {take_profit_pct}%)"
        elif pnl_pct <= -stop_loss_pct:
            kind = "sl"
            reason = f"stop-loss hit ({pnl_pct:+.2f}% <= -{stop_loss_pct}%)"
        else:
            continue
        logging.info(f"EXIT {symbol}: {reason}")
        _submit_exit(creds, state, symbol, qty, kind, reason, float(price), dry_run, busy)

    # Re-fetch positions in case we just exited
    if not dry_run:
        save_state(state)
        positions = get_positions(creds)

    dry_planned = {}  # {symbol: dollars} of dry-run entries made this cycle

    def current_usage() -> tuple[int, float]:
        return entry_usage(positions, {**pending_buy_notional(state), **dry_planned})

    def current_block_reasons() -> list[str]:
        open_count, exposure = current_usage()
        reasons = build_entry_block_reasons(
            open_count, exposure, account, clock,
            max_open_positions, max_total_exposure, max_daily_trades,
            max_daily_loss, no_new_trades_minutes_before_close, dry_run,
        )
        if force_close:
            reasons.append("force close window active")
        if unresolved:
            reasons.append("unresolved orders: " + "; ".join(unresolved))
        return reasons

    entry_block_reasons = current_block_reasons()
    if entry_block_reasons:
        logging.info("New entries blocked: " + "; ".join(entry_block_reasons))

    # Step 2: scan watchlist for signals
    table = Table(title=f"Signals - {strategy_name}", box=box.ASCII, header_style="bold cyan")
    table.add_column("Symbol", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Action")
    table.add_column("Reason", style="dim")

    for symbol in watchlist:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        bars = fetch_bars(creds, symbol)
        if not bars:
            table.add_row(symbol, "-", "[dim]no data[/dim]", "no recent bars")
            continue

        sig = strategy(symbol, bars)

        display_action = sig.action.upper()
        display_reason = sig.reason
        is_entry = sig.action == "buy" and symbol not in positions and symbol not in holdings
        blocked_for_symbol = []

        if is_entry:
            # Cooldown: don't re-enter the same name within an hour. Exits are not affected.
            until = cooldown_until(cooldowns, symbol)
            if until:
                blocked_for_symbol.append(f"cooldown until {until.strftime('%H:%M')}")
            blocked_for_symbol += entry_block_reasons
            if blocked_for_symbol:
                display_action = "BLOCKED"
                display_reason = f"{sig.reason} | blocked: {'; '.join(blocked_for_symbol)}"

        action_color = {"BUY": "green", "SELL": "red", "HOLD": "dim", "BLOCKED": "yellow"}[display_action]
        table.add_row(symbol, f"${sig.price:.2f}",
                      f"[{action_color}]{display_action}[/{action_color}]", display_reason)

        # Enter on BUY signals; exit the bot's own holdings on SELL signals.
        if is_entry:
            if blocked_for_symbol:
                logging.info(f"SKIP {symbol}: entry blocked - {'; '.join(blocked_for_symbol)}")
                continue

            qty = position_size(account, sig.price, max_per_trade=max_per_trade)
            if qty <= 0:
                logging.warning(f"{symbol}: sizing returned 0 qty, skipping")
                continue

            # Check the cap with this order included, not just what is held already.
            _, exposure = current_usage()
            after_pct = (exposure + qty * sig.price) / equity * 100 if equity > 0 else 100.0
            if max_total_exposure > 0 and after_pct > max_total_exposure * 100:
                logging.info(
                    f"SKIP {symbol}: order would take exposure to {after_pct:.2f}% "
                    f"(> {max_total_exposure * 100:.2f}%)"
                )
                continue

            indicators = {"rsi": sig.rsi_val, "sma_fast": sig.sma_fast,
                          "sma_slow": sig.sma_slow, "vol_z": sig.vol_z}

            # Avoid submitting a buy when there is already a working order for this symbol.
            if busy.get(symbol):
                logging.info(f"SKIP {symbol}: open order already exists for symbol")
                journal_write({
                    "timestamp": now_local().isoformat(timespec="seconds"),
                    "action": "ENTER_SKIPPED_OPEN_ORDER", "symbol": symbol, "qty": qty,
                    "signal_price": sig.price,
                    "fill_price": "", "order_status": "skipped_open_order",
                    "reason": f"{sig.reason} | open order already exists",
                    **indicators, "dry_run": dry_run,
                })
                continue

            logging.info(f"ENTER {symbol}: BUY {qty} @ ~${sig.price:.2f} - {sig.reason}")
            if dry_run:
                journal_write({
                    "timestamp": now_local().isoformat(timespec="seconds"),
                    "action": "ENTER_DRY", "symbol": symbol, "qty": qty,
                    "signal_price": sig.price, "fill_price": "", "order_status": "dry_run",
                    "reason": sig.reason, **indicators, "dry_run": True,
                })
                dry_planned[symbol] = qty * sig.price
            else:
                outcome = submit_tracked_order(creds, state, symbol, qty, "buy", "entry",
                                               sig.reason, sig.price, extra=indicators)
                if outcome != "rejected":
                    cooldowns[symbol] = (now_local() + timedelta(hours=1)).isoformat()
            busy.setdefault(symbol, set()).add("buy")

            # Later symbols in the same cycle must respect the caps with this entry counted.
            entry_block_reasons = current_block_reasons()
        elif sig.action == "sell" and symbol in holdings and not busy.get(symbol):
            qty = sellable_qty(state, positions, symbol)
            if qty <= 0:
                continue
            logging.info(f"EXIT {symbol}: signal sell")
            _submit_exit(creds, state, symbol, qty, "signal", sig.reason, sig.price, dry_run, busy)

    console.print(table)
    if not dry_run:
        save_state(state)


def main():
    p = argparse.ArgumentParser(
        prog="edgetrader-bot",
        description="Scheduled paper trading bot - rule-based signals, risk-managed",
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
    p.add_argument("--force-close-minutes-before-close", type=int, default=10,
                   help="Force close existing equity positions this many minutes before "
                        "market close, and block new entries (default 10)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)

    if args.env == "live" and not args.dry_run:
        console.print("[bold red]LIVE TRADING MODE - real money[/bold red]")
        confirm = input("Type 'I UNDERSTAND' to proceed: ")
        if confirm != "I UNDERSTAND":
            console.print("Aborted.")
            sys.exit(0)

    creds = load_creds(args.env)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]

    mode = "DRY RUN" if args.dry_run else f"LIVE-{args.env.upper()}"
    console.print(f"[bold green]EdgeTrader Bot[/bold green] - {mode} | "
                  f"strategy={args.strategy} | watchlist={','.join(watchlist)}")
    console.print(f"[dim]Logs: {LOG_FILE}  Journal: {JOURNAL_FILE}[/dim]\n")

    if args.once:
        try:
            run_cycle(creds, watchlist, args.strategy, args.dry_run,
                      args.max_per_trade, args.take_profit, args.stop_loss,
                      args.max_open_positions, args.max_total_exposure,
                      args.max_daily_trades, args.max_daily_loss,
                      args.no_new_trades_minutes_before_close,
                      args.force_close_minutes_before_close)
        except Exception as e:
            logging.exception(f"Cycle failed: {e}")
        return

    # Loop forever
    cycle = 0
    while True:
        cycle += 1
        console.print(f"\n[dim]-- Cycle {cycle} @ {now_local().strftime('%Y-%m-%d %H:%M:%S %Z')} --[/dim]")

        try:
            is_open, clock = market_is_open(creds)

            if not is_open:
                next_open = clock.get("next_open", "")
                logging.info(f"Market closed. Next open: {_format_clock_ts_local(next_open)}")
                sleep_until_next_open(clock)
                continue

            run_cycle(creds, watchlist, args.strategy, args.dry_run,
                      args.max_per_trade, args.take_profit, args.stop_loss,
                      args.max_open_positions, args.max_total_exposure,
                      args.max_daily_trades, args.max_daily_loss,
                      args.no_new_trades_minutes_before_close,
                      args.force_close_minutes_before_close)

            time.sleep(args.interval)

        except KeyboardInterrupt:
            console.print("\n[dim]Bot stopped.[/dim]")
            break
        except Exception as e:
            logging.exception(f"Cycle failed: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
