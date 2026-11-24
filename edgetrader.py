#!/usr/bin/env python3
"""
EdgeTrader CLI — day trading strategist with Alpaca integration
Usage: edgetrader <command> [options]
       edgetrader --help
"""
import os
import sys
import json
import argparse
import configparser
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

try:
    import requests
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.live import Live
    from rich.layout import Layout
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install requests rich")
    sys.exit(1)

console = Console()

# ----- Config management -----
CONFIG_DIR = Path.home() / ".edgetrader"
CONFIG_FILE = CONFIG_DIR / "config.ini"

def load_config():
    CONFIG_DIR.mkdir(exist_ok=True)
    cp = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cp.read(CONFIG_FILE)
    return cp

def save_config(cp):
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        cp.write(f)
    # Lock down permissions — secret key lives here
    os.chmod(CONFIG_FILE, 0o600)

def get_credentials(env: str = None):
    cp = load_config()
    env = env or cp.get("default", "env", fallback="paper")
    section = f"alpaca_{env}"
    if section not in cp:
        console.print(f"[red]No {env} credentials found.[/red] Run: [cyan]edgetrader configure[/cyan]")
        sys.exit(1)
    return {
        "key": cp[section]["api_key"],
        "secret": cp[section]["secret_key"],
        "env": env,
        "anthropic_key": cp.get("anthropic", "api_key", fallback=os.environ.get("ANTHROPIC_API_KEY", ""))
    }

def base_url(env: str) -> str:
    return "https://paper-api.alpaca.markets" if env == "paper" else "https://api.alpaca.markets"

DATA_BASE = "https://data.alpaca.markets"

def headers(creds):
    return {
        "APCA-API-KEY-ID": creds["key"],
        "APCA-API-SECRET-KEY": creds["secret"],
        "Content-Type": "application/json",
    }

# ----- Formatters -----
def fmt_money(n) -> str:
    n = float(n) if n else 0
    return f"${n:,.2f}"

def fmt_pct(n) -> str:
    n = float(n) if n else 0
    arrow = "▲" if n >= 0 else "▼"
    color = "green" if n >= 0 else "red"
    return f"[{color}]{arrow} {abs(n):.2f}%[/{color}]"

def color_money(n) -> str:
    n = float(n) if n else 0
    color = "green" if n >= 0 else "red"
    sign = "+" if n >= 0 else ""
    return f"[{color}]{sign}{fmt_money(n)}[/{color}]"

# ----- Commands -----
def cmd_configure(args):
    """Interactive setup wizard"""
    console.print(Panel.fit(
        "[bold green]⬡ EdgeTrader[/bold green] — credential setup",
        border_style="green"
    ))

    cp = load_config()

    env = Prompt.ask(
        "\n[bold]Which environment?[/bold]",
        choices=["paper", "live"],
        default="paper"
    )

    if env == "live":
        if not Confirm.ask("[red]⚠ LIVE trading uses real money. Continue?[/red]", default=False):
            return

    console.print(f"\n[dim]Get your keys from:[/dim] https://app.alpaca.markets")
    console.print(f"[dim]Make sure to switch to {env.upper()} mode in the upper-left dashboard toggle.[/dim]\n")

    api_key = Prompt.ask("[bold]API Key ID[/bold]")
    secret_key = Prompt.ask("[bold]Secret Key[/bold]", password=True)

    section = f"alpaca_{env}"
    if section not in cp:
        cp[section] = {}
    cp[section]["api_key"] = api_key
    cp[section]["secret_key"] = secret_key

    if "default" not in cp:
        cp["default"] = {}
    cp["default"]["env"] = env

    # Optional: Anthropic API key for AI signals
    console.print("\n[dim]Optional: Anthropic API key enables AI-powered screener signals.[/dim]")
    console.print("[dim]Get one at https://console.anthropic.com or skip with Enter.[/dim]")
    anth_key = Prompt.ask("[bold]Anthropic API Key[/bold]", default="", show_default=False)
    if anth_key:
        if "anthropic" not in cp:
            cp["anthropic"] = {}
        cp["anthropic"]["api_key"] = anth_key

    save_config(cp)

    # Test the connection
    console.print("\n[dim]Testing connection...[/dim]")
    creds = {"key": api_key, "secret": secret_key, "env": env}
    try:
        r = requests.get(f"{base_url(env)}/v2/account", headers=headers(creds), timeout=10)
        r.raise_for_status()
        acct = r.json()
        console.print(f"[green]✓ Connected![/green] Account [bold]{acct['account_number']}[/bold] — status: {acct['status']}")
        console.print(f"[dim]Config saved to {CONFIG_FILE} (chmod 600)[/dim]")
    except requests.HTTPError as e:
        console.print(f"[red]✗ Auth failed:[/red] {e.response.text}")
    except Exception as e:
        console.print(f"[red]✗ Error:[/red] {e}")

def cmd_account(args):
    """Show account summary"""
    creds = get_credentials(args.env)
    r = requests.get(f"{base_url(creds['env'])}/v2/account", headers=headers(creds), timeout=10)
    r.raise_for_status()
    a = r.json()

    pnl = float(a["equity"]) - float(a.get("last_equity") or a["equity"])
    pnl_pct = (pnl / float(a.get("last_equity") or a["equity"])) * 100 if float(a.get("last_equity") or 0) else 0

    env_color = "yellow" if creds["env"] == "paper" else "red"
    env_label = "PAPER" if creds["env"] == "paper" else "⚠ LIVE"

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("", style="dim")
    table.add_column("", style="bold")

    table.add_row("Account #", a["account_number"])
    table.add_row("Status", f"[green]{a['status']}[/green]")
    table.add_row("Environment", f"[{env_color}]{env_label}[/{env_color}]")
    table.add_row("", "")
    table.add_row("Portfolio value", fmt_money(a["portfolio_value"]))
    table.add_row("Cash", fmt_money(a["cash"]))
    table.add_row("Buying power", fmt_money(a["buying_power"]))
    table.add_row("Options BP", fmt_money(a.get("options_buying_power", 0)))
    table.add_row("", "")
    table.add_row("Day P&L", f"{color_money(pnl)} {fmt_pct(pnl_pct)}")
    table.add_row("Day trades", str(a.get("daytrade_count", 0)))
    table.add_row("PDT flag", "[red]Yes[/red]" if a.get("pattern_day_trader") else "[dim]No[/dim]")
    table.add_row("Options level", str(a.get("options_trading_level", 0)))

    console.print(Panel(table, title="[bold green]⬡ Account[/bold green]", border_style="green"))

def cmd_quote(args):
    """Get latest quote for a symbol"""
    creds = get_credentials(args.env)
    sym = args.symbol.upper()

    r = requests.get(
        f"{DATA_BASE}/v2/stocks/{sym}/snapshot",
        headers=headers(creds), timeout=10
    )
    if r.status_code != 200:
        console.print(f"[red]Error:[/red] {r.text}")
        return
    snap = r.json()

    quote = snap.get("latestQuote", {})
    trade = snap.get("latestTrade", {})
    bar = snap.get("dailyBar", {})
    prev_bar = snap.get("prevDailyBar", {})

    last = float(trade.get("p", 0))
    prev_close = float(prev_bar.get("c", 0))
    change = last - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("", style="dim")
    table.add_column("", style="bold")

    table.add_row("Symbol", f"[cyan]{sym}[/cyan]")
    table.add_row("Last", fmt_money(last))
    table.add_row("Change", f"{color_money(change)} {fmt_pct(change_pct)}")
    table.add_row("", "")
    table.add_row("Bid", f"{fmt_money(quote.get('bp',0))} × {quote.get('bs',0)}")
    table.add_row("Ask", f"{fmt_money(quote.get('ap',0))} × {quote.get('as',0)}")
    table.add_row("", "")
    table.add_row("Day open", fmt_money(bar.get("o", 0)))
    table.add_row("Day high", fmt_money(bar.get("h", 0)))
    table.add_row("Day low", fmt_money(bar.get("l", 0)))
    table.add_row("Volume", f"{int(bar.get('v',0)):,}")
    table.add_row("Prev close", fmt_money(prev_close))

    console.print(Panel(table, title=f"[bold]Quote: {sym}[/bold]", border_style="cyan"))

def cmd_positions(args):
    """List open positions"""
    creds = get_credentials(args.env)
    r = requests.get(f"{base_url(creds['env'])}/v2/positions", headers=headers(creds), timeout=10)
    r.raise_for_status()
    positions = r.json()

    if not positions:
        console.print("[dim]No open positions.[/dim]")
        return

    table = Table(title="Open positions", box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("Symbol", style="bold")
    table.add_column("Qty", justify="right")
    table.add_column("Avg entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L %", justify="right")
    table.add_column("Market val", justify="right")

    total_pnl = 0
    total_mv = 0
    for p in positions:
        pnl = float(p["unrealized_pl"])
        pct = float(p["unrealized_plpc"]) * 100
        total_pnl += pnl
        total_mv += float(p["market_value"])
        table.add_row(
            p["symbol"],
            p["qty"],
            fmt_money(p["avg_entry_price"]),
            fmt_money(p["current_price"]),
            color_money(pnl),
            fmt_pct(pct),
            fmt_money(p["market_value"]),
        )

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", "", "",
        color_money(total_pnl), "",
        fmt_money(total_mv),
    )
    console.print(table)

def cmd_orders(args):
    """List recent orders"""
    creds = get_credentials(args.env)
    status = args.status or "all"
    limit = args.limit or 20
    r = requests.get(
        f"{base_url(creds['env'])}/v2/orders",
        headers=headers(creds),
        params={"status": status, "limit": limit, "direction": "desc"},
        timeout=10
    )
    r.raise_for_status()
    orders = r.json()

    if not orders:
        console.print(f"[dim]No {status} orders.[/dim]")
        return

    table = Table(title=f"Recent orders ({status})", box=box.SIMPLE_HEAD, header_style="bold cyan")
    table.add_column("Symbol", style="bold")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Fill px", justify="right")
    table.add_column("Time", style="dim")

    for o in orders:
        side_color = "green" if o["side"] == "buy" else "red"
        status_color = {
            "filled": "green",
            "canceled": "yellow",
            "rejected": "red",
            "new": "cyan",
            "partially_filled": "yellow",
        }.get(o["status"], "white")
        fill_px = fmt_money(o["filled_avg_price"]) if o.get("filled_avg_price") else "—"
        ts = datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00")).strftime("%H:%M:%S")
        table.add_row(
            o["symbol"],
            f"[{side_color}]{o['side'].upper()}[/{side_color}]",
            o["qty"],
            o["type"].upper(),
            f"[{status_color}]{o['status'].upper()}[/{status_color}]",
            fill_px,
            ts,
        )
    console.print(table)

def build_option_symbol(underlying: str, expiry: str, opt_type: str, strike: float) -> str:
    """OCC option symbol format: AAPL250117C00150000"""
    expiry_fmt = expiry.replace("-", "")[2:]  # YYMMDD
    cp = opt_type[0].upper()
    strike_int = int(strike * 1000)
    return f"{underlying.upper()}{expiry_fmt}{cp}{strike_int:08d}"

def cmd_chain(args):
    """Show options chain for a ticker"""
    creds = get_credentials(args.env)
    sym = args.symbol.upper()

    params = {"feed": "indicative", "limit": 200}
    if args.expiry:
        params["expiration_date"] = args.expiry

    r = requests.get(
        f"{DATA_BASE}/v1beta1/options/snapshots/{sym}",
        headers=headers(creds),
        params=params,
        timeout=15
    )
    if r.status_code != 200:
        console.print(f"[red]Error:[/red] {r.text}")
        return
    data = r.json()
    snaps = data.get("snapshots", {})
    if not snaps:
        console.print(f"[yellow]No options data for {sym}.[/yellow] Make sure options trading is enabled.")
        return

    # Parse OCC symbols
    contracts = []
    for occ_sym, snap in snaps.items():
        # Format: AAPL250117C00150000
        # Find where the date starts (6 digits)
        import re
        m = re.match(r"([A-Z]+)(\d{6})([CP])(\d{8})", occ_sym)
        if not m:
            continue
        underlying, dt, cp, strike = m.groups()
        expiry = f"20{dt[:2]}-{dt[2:4]}-{dt[4:6]}"
        strike_val = int(strike) / 1000
        contracts.append({
            "sym": occ_sym, "type": "call" if cp == "C" else "put",
            "strike": strike_val, "expiry": expiry, "snap": snap
        })

    if not contracts:
        console.print(f"[yellow]No option contracts found for {sym}.[/yellow]")
        return

    # Filter by expiry if not given — pick nearest
    expiries = sorted(set(c["expiry"] for c in contracts))
    if not args.expiry and expiries:
        target_expiry = expiries[0]
        contracts = [c for c in contracts if c["expiry"] == target_expiry]
    else:
        target_expiry = args.expiry or expiries[0]

    # Strike filter
    if args.strikes:
        # Try to get current price to filter near ATM
        try:
            qr = requests.get(f"{DATA_BASE}/v2/stocks/{sym}/snapshot", headers=headers(creds), timeout=10)
            spot = float(qr.json()["latestTrade"]["p"])
        except:
            spot = sum(c["strike"] for c in contracts) / len(contracts)
        contracts = [c for c in contracts if abs(c["strike"] - spot) / spot < args.strikes / 100]

    calls = sorted([c for c in contracts if c["type"] == "call"], key=lambda x: x["strike"])
    puts = sorted([c for c in contracts if c["type"] == "put"], key=lambda x: x["strike"])

    strikes = sorted(set([c["strike"] for c in contracts]))
    call_map = {c["strike"]: c for c in calls}
    put_map = {p["strike"]: p for p in puts}

    table = Table(
        title=f"Options chain — {sym} (exp {target_expiry})",
        box=box.SIMPLE_HEAD, header_style="bold cyan"
    )
    table.add_column("C Bid", justify="right", style="green")
    table.add_column("C Ask", justify="right", style="green")
    table.add_column("C Δ", justify="right", style="dim green")
    table.add_column("C IV", justify="right", style="dim")
    table.add_column("Strike", justify="center", style="bold yellow")
    table.add_column("P IV", justify="right", style="dim")
    table.add_column("P Δ", justify="right", style="dim red")
    table.add_column("P Bid", justify="right", style="red")
    table.add_column("P Ask", justify="right", style="red")

    def cell(c, key, default="—", fmt=None):
        if not c:
            return default
        v = c["snap"].get(key)
        if v is None:
            return default
        if fmt:
            return fmt(v)
        return str(v)

    for strike in strikes:
        c = call_map.get(strike)
        p = put_map.get(strike)

        def quote(contract, side):
            if not contract:
                return ("—", "—")
            q = contract["snap"].get("latestQuote", {})
            bid = f"{q.get('bp', 0):.2f}" if q.get("bp") else "—"
            ask = f"{q.get('ap', 0):.2f}" if q.get("ap") else "—"
            return (bid, ask)

        def greek(contract):
            if not contract:
                return ("—", "—")
            g = contract["snap"].get("greeks", {}) or {}
            d = f"{g.get('delta', 0):.3f}" if g.get("delta") is not None else "—"
            iv = contract["snap"].get("impliedVolatility")
            iv_s = f"{iv * 100:.1f}%" if iv else "—"
            return (d, iv_s)

        c_bid, c_ask = quote(c, "call")
        p_bid, p_ask = quote(p, "put")
        c_d, c_iv = greek(c)
        p_d, p_iv = greek(p)

        table.add_row(c_bid, c_ask, c_d, c_iv, f"${strike:.1f}", p_iv, p_d, p_bid, p_ask)

    console.print(table)
    console.print(f"\n[dim]Available expirations: {', '.join(expiries[:8])}[/dim]")
    console.print("[dim]Filter: --expiry YYYY-MM-DD --strikes 10 (=±10% of spot)[/dim]")

def cmd_buy(args):
    cmd_order(args, side="buy")

def cmd_sell(args):
    cmd_order(args, side="sell")

def cmd_order(args, side: str = None):
    """Place an order"""
    side = side or args.side
    sym = args.symbol.upper()

    # Validate option fields BEFORE credentials: all-or-nothing.
    # Partial input (e.g. --expiry without --strike) silently falling through
    # to an equity order would be a costly footgun.
    opt_fields = {
        "--expiry": args.expiry,
        "--strike": args.strike,
        "--option-type": args.option_type,
    }
    provided = {k: v for k, v in opt_fields.items() if v is not None}
    if provided and len(provided) < 3:
        missing = [k for k, v in opt_fields.items() if v is None]
        console.print(
            f"[red]Incomplete option order:[/red] you provided "
            f"{', '.join(provided.keys())} but are missing {', '.join(missing)}."
        )
        console.print(
            "[dim]To place an option order, all of --expiry, --strike, and "
            "--option-type are required. To place an equity order, omit all three.[/dim]"
        )
        sys.exit(2)

    creds = get_credentials(args.env)

    # Build option symbol if option fields provided
    if args.expiry and args.strike and args.option_type:
        sym = build_option_symbol(sym, args.expiry, args.option_type, args.strike)
        is_option = True
    else:
        is_option = False

    payload = {
        "symbol": sym,
        "qty": str(args.qty),
        "side": side,
        "type": args.type,
        "time_in_force": args.tif or "day",
    }
    if args.type in ("limit", "stop_limit") and args.limit:
        payload["limit_price"] = str(args.limit)
    if args.type in ("stop", "stop_limit") and args.stop:
        payload["stop_price"] = str(args.stop)

    # Confirmation
    panel_color = "green" if side == "buy" else "red"
    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column("", style="dim")
    summary.add_column("", style="bold")
    summary.add_row("Symbol", sym)
    if is_option:
        summary.add_row("Type", f"[cyan]Option {args.option_type.upper()}[/cyan]")
        summary.add_row("Strike", fmt_money(args.strike))
        summary.add_row("Expiry", args.expiry)
    summary.add_row("Side", f"[{panel_color}]{side.upper()}[/{panel_color}]")
    summary.add_row("Qty", str(args.qty))
    summary.add_row("Order type", args.type.upper())
    if "limit_price" in payload:
        summary.add_row("Limit price", fmt_money(payload["limit_price"]))
    if "stop_price" in payload:
        summary.add_row("Stop price", fmt_money(payload["stop_price"]))
    summary.add_row("TIF", payload["time_in_force"].upper())
    summary.add_row("Account", f"[{'yellow' if creds['env']=='paper' else 'red'}]{creds['env'].upper()}[/]")

    console.print(Panel(summary, title=f"[bold]Review order[/bold]", border_style=panel_color))

    if creds["env"] == "live":
        console.print("[bold red]⚠ LIVE TRADING — real money at risk[/bold red]")

    if not args.yes:
        if not Confirm.ask(f"\n[bold]Submit this {side.upper()} order?[/bold]", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

    r = requests.post(
        f"{base_url(creds['env'])}/v2/orders",
        headers=headers(creds),
        json=payload,
        timeout=15
    )
    if r.status_code not in (200, 201):
        console.print(f"[red]Order failed:[/red] {r.text}")
        return

    o = r.json()
    console.print(f"[green]✓ Order submitted[/green] — ID [cyan]{o['id'][:8]}[/cyan]")
    console.print(f"[dim]Status:[/dim] {o['status']}  [dim]Symbol:[/dim] {o['symbol']}  [dim]Qty:[/dim] {o['qty']}")

def cmd_close(args):
    """Close a position"""
    # Validate inputs BEFORE hitting credentials, so the user sees the
    # most useful error message regardless of config state.
    if not args.all and not args.symbol:
        console.print("[red]Error:[/red] provide a symbol or use [cyan]--all[/cyan] to close every position.")
        console.print("[dim]Examples: edgetrader close AAPL    |    edgetrader close --all[/dim]")
        sys.exit(2)

    creds = get_credentials(args.env)

    if args.all:
        if not args.yes and not Confirm.ask(f"[red]Close ALL positions on {creds['env'].upper()}?[/red]", default=False):
            return
        r = requests.delete(f"{base_url(creds['env'])}/v2/positions", headers=headers(creds), timeout=15)
        if r.status_code not in (200, 207):
            console.print(f"[red]Error:[/red] {r.text}")
            return
        results = r.json()
        failed = [x for x in results if x.get("status") not in (200, 201)]
        console.print(f"[green]✓ Close-all submitted[/green] — {len(results) - len(failed)} orders placed")
        for x in failed:
            console.print(f"[red]✗ {x.get('symbol', '?')}:[/red] {x.get('body')}")
        return

    sym = args.symbol.upper()

    if not args.yes:
        if not Confirm.ask(f"Close entire {sym} position?", default=False):
            return

    r = requests.delete(
        f"{base_url(creds['env'])}/v2/positions/{sym}",
        headers=headers(creds),
        timeout=15
    )
    if r.status_code in (200, 207):
        console.print(f"[green]✓ Closed {sym}[/green]")
    else:
        console.print(f"[red]Error:[/red] {r.text}")

def cmd_cancel(args):
    """Cancel order(s)"""
    if not args.all and not args.order_id:
        console.print("[red]Error:[/red] provide an order ID or use [cyan]--all[/cyan].")
        console.print("[dim]Examples: edgetrader cancel <id>    |    edgetrader cancel --all[/dim]")
        sys.exit(2)

    creds = get_credentials(args.env)
    if args.all:
        if not Confirm.ask("Cancel ALL open orders?", default=False):
            return
        r = requests.delete(f"{base_url(creds['env'])}/v2/orders", headers=headers(creds), timeout=15)
        if r.status_code not in (200, 207):
            console.print(f"[red]Error:[/red] {r.text}")
            return
        results = r.json()
        failed = [x for x in results if x.get("status") not in (200, 204)]
        console.print(f"[green]✓ Cancel requested[/green] for {len(results) - len(failed)} orders")
        for x in failed:
            console.print(f"[red]✗ {x.get('id', '?')}:[/red] {x.get('body')}")
        return
    r = requests.delete(
        f"{base_url(creds['env'])}/v2/orders/{args.order_id}",
        headers=headers(creds), timeout=10
    )
    if r.status_code in (204, 200):
        console.print(f"[green]✓ Cancelled[/green] {args.order_id}")
    else:
        console.print(f"[red]Error:[/red] {r.text}")

def _extract_json(text: str) -> Optional[dict]:
    """
    Extract a JSON object from model output.
    Tries in order: direct parse, fenced block, balanced-brace scan.
    Returns None if nothing parses.
    """
    if not text:
        return None

    # 1) Direct parse (cleanest case)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 2) Fenced ```json ... ``` block
    import re
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3) Balanced-brace scan — find the first '{' and walk forward
    # tracking depth and string state to find its matching '}'.
    # This handles cases where the model wraps JSON in prose.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try next '{'
        start = text.find("{", start + 1)

    return None


def _validate_signals(obj) -> tuple[bool, str]:
    """
    Validate signals payload against the expected schema.
    Returns (ok, error_message).
    """
    if not isinstance(obj, dict):
        return False, "response is not a JSON object"
    if "signals" not in obj:
        return False, "missing 'signals' key"
    if not isinstance(obj["signals"], list):
        return False, "'signals' is not a list"
    if not obj["signals"]:
        return False, "'signals' list is empty"

    required = {"ticker", "signal"}
    valid_signals = {"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"}
    for i, s in enumerate(obj["signals"]):
        if not isinstance(s, dict):
            return False, f"signal[{i}] is not an object"
        missing = required - s.keys()
        if missing:
            return False, f"signal[{i}] missing fields: {missing}"
        # Normalize signal value for downstream rendering
        sig_norm = str(s["signal"]).replace(" ", "_").upper()
        if sig_norm not in valid_signals:
            return False, f"signal[{i}] has invalid signal value: {s['signal']!r}"
    return True, ""


def cmd_signals(args):
    """AI-powered screener using Claude with web search"""
    creds = get_credentials(args.env)
    if not creds.get("anthropic_key"):
        console.print("[red]Anthropic API key not configured.[/red]")
        console.print("Set [cyan]ANTHROPIC_API_KEY[/cyan] env var or run [cyan]edgetrader configure[/cyan]")
        return

    tickers = args.tickers.upper()
    strategy = args.strategy

    # Strict schema in the prompt itself — anchors the model to a known shape.
    # Combined with _extract_json + _validate_signals, this gives 3 layers of defense:
    #   1. Prompt asks for strict JSON
    #   2. Extractor handles fenced/wrapped output
    #   3. Validator rejects malformed schemas before render
    prompt = f"""You are an expert day trading analyst. Analyze these tickers for the "{strategy}" strategy: {tickers}.

For each ticker, search the web for current price and technical indicators, then return JSON.

Return ONLY a single JSON object. No prose before or after. No markdown fences.

Schema (all fields required):
{{
  "strategy": "{strategy}",
  "signals": [
    {{
      "ticker": "AAPL",
      "price": 195.50,
      "signal": "BUY",
      "strength": 72,
      "technical": "RSI 42, oversold bounce",
      "options_play": "200C exp 2026-05-17",
      "risk": "Earnings next week",
      "rationale": "One sentence explanation"
    }}
  ]
}}

The "signal" field MUST be exactly one of: STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL.
The "strength" field MUST be an integer 0-100.
Return one entry per ticker requested."""

    max_attempts = 2
    last_error = None
    last_text = None

    for attempt in range(1, max_attempts + 1):
        with console.status(
            f"[cyan]Running AI analysis (attempt {attempt}/{max_attempts})...",
            spinner="dots",
        ):
            try:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                        "x-api-key": creds["anthropic_key"],
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1500,
                        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
            except requests.RequestException as e:
                last_error = f"network error: {e}"
                continue

        if r.status_code != 200:
            last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            # 4xx errors won't be fixed by retrying
            if 400 <= r.status_code < 500:
                break
            continue

        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        last_text = text

        result = _extract_json(text)
        if result is None:
            last_error = "could not extract JSON from response"
            continue

        ok, err = _validate_signals(result)
        if not ok:
            last_error = f"schema validation failed: {err}"
            continue

        # Success
        _render_signals(result, strategy)
        return

    # All attempts failed — show clear error and a snippet of what we got
    console.print(f"[red]Screener failed after {max_attempts} attempts.[/red]")
    console.print(f"[dim]Last error:[/dim] {last_error}")
    if last_text:
        snippet = last_text[:300] + ("..." if len(last_text) > 300 else "")
        console.print(f"[dim]Last response snippet:[/dim]\n{snippet}")


def _render_signals(result: dict, strategy: str):
    table = Table(
        title=f"Signals — {strategy}",
        box=box.SIMPLE_HEAD, header_style="bold cyan"
    )
    table.add_column("Ticker", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Signal")
    table.add_column("Conf", justify="right")
    table.add_column("Technical", style="dim")
    table.add_column("Options play", style="cyan")
    table.add_column("Risk", style="dim yellow")

    for s in result["signals"]:
        sig = str(s["signal"]).replace(" ", "_").upper()
        sig_color = {
            "STRONG_BUY": "bold green",
            "BUY": "green",
            "HOLD": "yellow",
            "SELL": "red",
            "STRONG_SELL": "bold red",
        }.get(sig, "white")
        price = s.get("price")
        price_str = fmt_money(price) if price is not None else "—"
        strength = s.get("strength", 0)
        table.add_row(
            str(s["ticker"]),
            price_str,
            f"[{sig_color}]{sig}[/{sig_color}]",
            f"{strength}%",
            str(s.get("technical", "")),
            str(s.get("options_play", "")),
            str(s.get("risk", "")),
        )
    console.print(table)
    console.print("\n[dim italic]⚠ AI signals for educational use. Not financial advice.[/dim italic]")

def cmd_watch(args):
    """Live-refreshing dashboard"""
    creds = get_credentials(args.env)
    interval = args.interval or 5

    def render():
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
        )
        layout["main"].split_row(Layout(name="acct"), Layout(name="pos"))

        # Account
        try:
            r = requests.get(f"{base_url(creds['env'])}/v2/account", headers=headers(creds), timeout=5)
            a = r.json()
            pnl = float(a["equity"]) - float(a.get("last_equity") or a["equity"])
            pnl_pct = (pnl / float(a.get("last_equity") or a["equity"])) * 100 if float(a.get("last_equity") or 0) else 0

            acct_t = Table(box=box.SIMPLE, show_header=False)
            acct_t.add_column("", style="dim"); acct_t.add_column("", style="bold")
            acct_t.add_row("Portfolio", fmt_money(a["portfolio_value"]))
            acct_t.add_row("Cash", fmt_money(a["cash"]))
            acct_t.add_row("Buying power", fmt_money(a["buying_power"]))
            acct_t.add_row("Day P&L", f"{color_money(pnl)} {fmt_pct(pnl_pct)}")
            layout["acct"].update(Panel(acct_t, title="Account", border_style="green"))
        except Exception as e:
            layout["acct"].update(Panel(f"[red]{e}[/red]", title="Account"))

        # Positions
        try:
            r = requests.get(f"{base_url(creds['env'])}/v2/positions", headers=headers(creds), timeout=5)
            positions = r.json()
            if not positions:
                layout["pos"].update(Panel("[dim]No positions[/dim]", title="Positions"))
            else:
                pt = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
                pt.add_column("Sym"); pt.add_column("Qty", justify="right"); pt.add_column("Curr", justify="right")
                pt.add_column("P&L", justify="right"); pt.add_column("%", justify="right")
                for p in positions[:10]:
                    pnl = float(p["unrealized_pl"])
                    pct = float(p["unrealized_plpc"]) * 100
                    pt.add_row(p["symbol"], p["qty"], fmt_money(p["current_price"]),
                              color_money(pnl), fmt_pct(pct))
                layout["pos"].update(Panel(pt, title="Positions", border_style="cyan"))
        except Exception as e:
            layout["pos"].update(Panel(f"[red]{e}[/red]", title="Positions"))

        env_label = "[yellow]PAPER[/yellow]" if creds["env"] == "paper" else "[red]LIVE[/red]"
        header = f"⬡ [bold green]EdgeTrader[/bold green] {env_label}  •  refresh {interval}s  •  {datetime.now().strftime('%H:%M:%S')}  •  Ctrl+C to exit"
        layout["header"].update(Panel(header, border_style="green"))
        return layout

    try:
        with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
            import time
            while True:
                time.sleep(interval)
                live.update(render())
    except KeyboardInterrupt:
        console.print("\n[dim]Watch stopped.[/dim]")

# ----- CLI parser -----
def main():
    parser = argparse.ArgumentParser(
        prog="edgetrader",
        description="⬡ EdgeTrader — day trading CLI for Alpaca",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  edgetrader configure                          # Set up API keys
  edgetrader account                            # Show account summary
  edgetrader quote AAPL                         # Get latest quote
  edgetrader chain SPY --strikes 5              # Options chain ±5% of spot
  edgetrader chain SPY --expiry 2026-05-17      # Specific expiry
  edgetrader buy AAPL --qty 10 --type market    # Buy 10 shares
  edgetrader buy SPY --qty 1 --option-type call --strike 580 --expiry 2026-05-17 --type limit --limit 2.50
  edgetrader positions                          # Show open positions
  edgetrader close AAPL                         # Close one position
  edgetrader close --all                        # Close everything
  edgetrader cancel --all                       # Cancel all open orders
  edgetrader signals AAPL,TSLA,SPY -s momentum  # AI screener
  edgetrader watch                              # Live dashboard
  edgetrader account --env live                 # Use live account
        """,
    )
    parser.add_argument("--env", choices=["paper", "live"], help="Environment override")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("configure", help="Set up Alpaca + Anthropic credentials")
    sub.add_parser("account", help="Account summary")

    p_quote = sub.add_parser("quote", help="Get latest quote")
    p_quote.add_argument("symbol")

    p_pos = sub.add_parser("positions", help="List open positions")

    p_ord = sub.add_parser("orders", help="List recent orders")
    p_ord.add_argument("--status", choices=["open", "closed", "all"], default="all")
    p_ord.add_argument("-l", "--limit", type=int, default=20)

    p_chain = sub.add_parser("chain", help="Show options chain")
    p_chain.add_argument("symbol")
    p_chain.add_argument("--expiry", help="YYYY-MM-DD")
    p_chain.add_argument("--strikes", type=float, help="Filter ±N%% of spot")

    for cmd_name, side in [("buy", "buy"), ("sell", "sell"), ("order", None)]:
        p = sub.add_parser(cmd_name, help=f"Place {cmd_name} order")
        p.add_argument("symbol")
        p.add_argument("--qty", type=int, required=True)
        p.add_argument("--type", choices=["market", "limit", "stop", "stop_limit"], default="market")
        p.add_argument("--limit", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--tif", choices=["day", "gtc", "ioc", "fok", "opg", "cls"], default="day")
        # Options
        p.add_argument("--option-type", choices=["call", "put"])
        p.add_argument("--strike", type=float)
        p.add_argument("--expiry", help="YYYY-MM-DD")
        # Confirmation
        p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
        if cmd_name == "order":
            p.add_argument("--side", choices=["buy", "sell"], required=True)

    p_close = sub.add_parser("close", help="Close position")
    p_close.add_argument("symbol", nargs="?", default="")
    p_close.add_argument("--all", action="store_true", help="Close all positions")
    p_close.add_argument("-y", "--yes", action="store_true")

    p_can = sub.add_parser("cancel", help="Cancel order")
    p_can.add_argument("order_id", nargs="?", default="")
    p_can.add_argument("--all", action="store_true")

    p_sig = sub.add_parser("signals", help="AI-powered screener")
    p_sig.add_argument("tickers", help="Comma-separated")
    p_sig.add_argument("-s", "--strategy", choices=["momentum", "mean_reversion", "options_flow", "breakout"], default="momentum")

    p_watch = sub.add_parser("watch", help="Live dashboard")
    p_watch.add_argument("--interval", type=int, default=5)

    args = parser.parse_args()

    handlers = {
        "configure": cmd_configure,
        "account": cmd_account,
        "quote": cmd_quote,
        "positions": cmd_positions,
        "orders": cmd_orders,
        "chain": cmd_chain,
        "buy": cmd_buy,
        "sell": cmd_sell,
        "order": cmd_order,
        "close": cmd_close,
        "cancel": cmd_cancel,
        "signals": cmd_signals,
        "watch": cmd_watch,
    }

    try:
        handlers[args.cmd](args)
    except requests.HTTPError as e:
        console.print(f"[red]HTTP {e.response.status_code}:[/red] {e.response.text}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(130)

if __name__ == "__main__":
    main()
