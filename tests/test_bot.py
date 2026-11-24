"""
Tests for edgetrader_bot order safety. Run from the repo root:
    python -m unittest discover -s tests
No network access: every Alpaca call goes to FakeBroker.
"""
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import edgetrader_bot as bot  # noqa: E402

CREDS = {"key": "k", "secret": "s", "env": "paper"}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def make_bars(direction: str, n: int = 60, newest_age_minutes: int = 0):
    """Completed 5-minute bars ending now. 'down' gives an RSI buy, 'up' an RSI sell."""
    end = datetime.now(timezone.utc) - timedelta(minutes=newest_age_minutes)
    bars = []
    for i in range(n):
        t = end - timedelta(minutes=5 * (n - i))
        step = -0.5 if direction == "down" else 0.5
        volume = 1000 if i % 2 else 1200
        if i == n - 1:
            volume = 5000  # volume spike confirms the RSI buy
        bars.append({"t": t.isoformat().replace("+00:00", "Z"), "o": 100, "h": 100, "l": 100,
                     "c": 100 + step * i, "v": volume})
    return bars


class FakeBroker:
    def __init__(self):
        self.equity = "100000"
        self.positions = []
        self.positions_status = 200
        self.open_orders = []
        self.open_orders_status = 200
        self.orders_by_cid = {}       # client_order_id -> order dict
        self.bars = {}                # symbol -> bars (as returned by the API)
        self.post_mode = "accept"     # accept | fill | reject | timeout
        self.posted = []
        self.bar_params = []

    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith("/v2/clock"):
            close = datetime.now(timezone.utc) + timedelta(hours=3)
            return FakeResponse(200, {"is_open": True, "next_close": close.isoformat()})
        if url.endswith("/v2/account"):
            return FakeResponse(200, {"equity": self.equity, "last_equity": self.equity})
        if url.endswith("/v2/positions"):
            return FakeResponse(self.positions_status, self.positions)
        if url.endswith("/v2/orders:by_client_order_id"):
            order = self.orders_by_cid.get(params["client_order_id"])
            return FakeResponse(200, order) if order else FakeResponse(404, {"message": "not found"})
        if url.endswith("/v2/orders"):
            return FakeResponse(self.open_orders_status, self.open_orders)
        if "/bars" in url:
            self.bar_params.append(params)
            symbol = url.split("/stocks/")[1].split("/")[0]
            return FakeResponse(200, {"bars": self.bars.get(symbol, [])})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted.append(json)
        if self.post_mode == "timeout":
            raise requests.Timeout("timed out")
        if self.post_mode == "reject":
            return FakeResponse(403, {"message": "insufficient buying power"})
        order = {"id": f"oid-{len(self.posted)}", "client_order_id": json["client_order_id"],
                 "symbol": json["symbol"], "side": json["side"], "qty": json["qty"],
                 "status": "accepted", "filled_qty": "0", "filled_avg_price": None}
        if self.post_mode == "fill":
            order.update(status="filled", filled_qty=json["qty"], filled_avg_price="100")
        self.orders_by_cid[json["client_order_id"]] = order
        return FakeResponse(200, order)


def position(symbol, qty, current_price, avg_entry=100.0):
    qty = float(qty)
    return {"symbol": symbol, "qty": str(qty), "current_price": str(current_price),
            "market_value": str(qty * current_price),
            "unrealized_plpc": str((current_price - avg_entry) / avg_entry)}


class BotTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for name, value in {"CONFIG_DIR": d, "JOURNAL_FILE": d / "journal.csv",
                            "STATE_FILE": d / "bot_state.json"}.items():
            p = mock.patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.broker = FakeBroker()
        for name in ("get", "post"):
            p = mock.patch.object(bot.requests, name, getattr(self.broker, name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(bot.console, "print")
        p.start()
        self.addCleanup(p.stop)

    def run_cycle(self, watchlist, dry_run=False, **overrides):
        args = dict(strategy_name="rsi_reversion", dry_run=dry_run, max_per_trade=0.02,
                    take_profit_pct=0.75, stop_loss_pct=0.75, max_open_positions=5,
                    max_total_exposure=0.10, max_daily_trades=10, max_daily_loss=1.0,
                    no_new_trades_minutes_before_close=30, force_close_minutes_before_close=10)
        args.update(overrides)
        bot.run_cycle(CREDS, watchlist, **args)

    def journal_actions(self):
        if not bot.JOURNAL_FILE.exists():
            return []
        import csv
        with open(bot.JOURNAL_FILE, newline="") as f:
            return [r["action"] for r in csv.DictReader(f)]

    def set_state(self, state):
        bot.save_state({"cooldowns": {}, "orders": {}, "holdings": {}, **state})


class FailedReads(BotTestCase):
    def test_positions_read_failure_places_no_orders(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.positions_status = 500
        with self.assertRaises(requests.HTTPError):
            self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_open_orders_read_failure_places_no_orders(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.open_orders_status = 500
        with self.assertRaises(requests.HTTPError):
            self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_unreadable_journal_blocks_entries(self):
        self.broker.bars["AAA"] = make_bars("down")
        with mock.patch.object(bot, "today_entry_count", side_effect=OSError("locked")):
            self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_corrupt_state_stops_the_cycle(self):
        bot.STATE_FILE.write_text("{not json")
        with self.assertRaises(RuntimeError):
            self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])


class OrderOutcomes(BotTestCase):
    def test_accepted_entry_is_tracked_and_reserves_capacity(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.run_cycle(["AAA"])
        self.assertEqual(len(self.broker.posted), 1)
        self.assertTrue(self.broker.posted[0]["client_order_id"].startswith("etbot-"))
        state = bot.load_state()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["holdings"], {})
        self.assertIn("AAA", state["cooldowns"])
        self.assertEqual(self.journal_actions(), ["ENTER"])

    def test_rejected_entry_has_no_side_effects(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.post_mode = "reject"
        self.run_cycle(["AAA"])
        state = bot.load_state()
        self.assertEqual(state["orders"], {})
        self.assertEqual(state["holdings"], {})
        self.assertEqual(state["cooldowns"], {})
        self.assertEqual(self.journal_actions(), ["ENTER_REJECTED"])
        self.assertEqual(bot.today_entry_count(include_dry=False), 0)

    def test_uncertain_entry_blocks_entries_until_reconciled(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.bars["BBB"] = make_bars("down")
        self.broker.post_mode = "timeout"
        self.run_cycle(["AAA"])
        self.assertEqual(self.journal_actions(), ["ENTER_UNCERTAIN"])
        self.assertEqual(bot.today_entry_count(include_dry=False), 1)

        # Next cycle: the broker doesn't know the order yet, so no new entries at all.
        self.broker.post_mode = "accept"
        self.run_cycle(["BBB"])
        self.assertEqual(len(self.broker.posted), 1)

        # After the grace period the order is confirmed missing and entries resume.
        state = bot.load_state()
        (rec,) = state["orders"].values()
        rec["submitted_at"] = (bot.now_local() - timedelta(minutes=5)).isoformat()
        bot.save_state(state)
        self.run_cycle(["BBB"])
        self.assertEqual([p["symbol"] for p in self.broker.posted], ["AAA", "BBB"])
        self.assertIn("ENTER_NOT_FOUND", self.journal_actions())

    def test_uncertain_order_found_filled_becomes_a_holding(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.post_mode = "timeout"
        self.run_cycle(["AAA"])
        (cid,) = bot.load_state()["orders"]
        self.broker.orders_by_cid[cid] = {"id": "x", "status": "filled",
                                          "filled_qty": "20", "filled_avg_price": "90"}
        self.broker.positions = [position("AAA", 20, 90.2, avg_entry=90)]
        self.run_cycle([])
        state = bot.load_state()
        self.assertEqual(state["orders"], {})
        self.assertEqual(state["holdings"], {"AAA": {"qty": "20", "cost": "1800"}})
        self.assertEqual(self.journal_actions(), ["ENTER_UNCERTAIN", "ENTER_FILLED"])

    def test_partial_fills_are_applied_once(self):
        state = {"cooldowns": {}, "holdings": {}, "orders": {"c1": {
            "symbol": "AAA", "side": "buy", "qty": "10", "kind": "entry", "reason": "r",
            "signal_price": 100.0, "status": "pending", "order_id": "o",
            "submitted_at": bot.now_local().isoformat(), "filled_qty": "0", "filled_notional": "0"}}}
        bot.apply_order_update(state, "c1", {"status": "partially_filled",
                                             "filled_qty": "4", "filled_avg_price": "100"})
        bot.apply_order_update(state, "c1", {"status": "partially_filled",
                                             "filled_qty": "4", "filled_avg_price": "100"})
        self.assertEqual(state["holdings"]["AAA"]["qty"], "4")
        self.assertEqual(bot.pending_buy_notional(state), {"AAA": 600.0})
        bot.apply_order_update(state, "c1", {"status": "filled",
                                             "filled_qty": "10", "filled_avg_price": "101"})
        self.assertEqual(state["holdings"]["AAA"], {"qty": "10", "cost": "1010"})
        self.assertEqual(state["orders"], {})


class MarketData(BotTestCase):
    def test_requests_newest_bars_and_sorts_them(self):
        bars = make_bars("down")
        self.broker.bars["AAA"] = list(reversed(bars))
        got = bot.fetch_bars(CREDS, "AAA")
        self.assertEqual(self.broker.bar_params[0]["sort"], "desc")
        self.assertEqual(got, bars)

    def test_requests_bars_up_to_now(self):
        self.broker.bars["AAA"] = make_bars("down")
        bot.fetch_bars(CREDS, "AAA")
        end = datetime.fromisoformat(self.broker.bar_params[0]["end"].replace("Z", "+00:00"))
        self.assertLess(datetime.now(timezone.utc) - end, timedelta(minutes=1))

    def test_forming_bar_is_dropped(self):
        bars = make_bars("down")
        forming = dict(bars[-1], t=(datetime.now(timezone.utc) - timedelta(minutes=2))
                       .isoformat().replace("+00:00", "Z"))
        self.broker.bars["AAA"] = bars + [forming]
        self.assertEqual(bot.fetch_bars(CREDS, "AAA"), bars)

    def test_stale_bars_are_rejected(self):
        self.broker.bars["AAA"] = make_bars("down", newest_age_minutes=60)
        self.assertEqual(bot.fetch_bars(CREDS, "AAA"), [])

    def test_network_error_returns_no_bars(self):
        with mock.patch.object(bot.requests, "get", side_effect=requests.ConnectionError("down")):
            self.assertEqual(bot.fetch_bars(CREDS, "AAA"), [])


class DailyTradeCount(BotTestCase):
    def test_count_follows_new_york_trading_date(self):
        # 01:00 Manila on Nov 25 is 12:00 New York on Nov 24, mid-session.
        now = datetime(2026, 11, 25, 1, 0, tzinfo=bot.LOCAL_TZ)
        for ts in ("2026-11-24T23:00:00+08:00",   # 10:00 NY Nov 24: same session
                   "2026-11-24T08:00:00+08:00"):  # 19:00 NY Nov 23: previous session
            bot.journal_write({"timestamp": ts, "action": "ENTER", "symbol": "AAA"})
        with mock.patch.object(bot, "now_local", return_value=now):
            self.assertEqual(bot.today_entry_count(include_dry=False), 1)


class Ownership(BotTestCase):
    def test_only_bot_shares_are_sold(self):
        self.set_state({"holdings": {"BOT": {"qty": "5", "cost": "500"}}})
        self.broker.positions = [position("BOT", 8, 98.0), position("USER", 10, 90.0)]
        self.run_cycle([])
        self.assertEqual(len(self.broker.posted), 1)
        self.assertEqual(self.broker.posted[0]["symbol"], "BOT")
        self.assertEqual(self.broker.posted[0]["side"], "sell")
        self.assertEqual(self.broker.posted[0]["qty"], "5")
        self.assertIn("EXIT_SL_SUBMITTED", self.journal_actions())

    def test_short_position_is_never_sold(self):
        self.set_state({"holdings": {"BOT": {"qty": "5", "cost": "500"}}})
        self.broker.positions = [position("BOT", -5, 98.0)]
        self.run_cycle([])
        self.assertEqual(self.broker.posted, [])
        self.assertEqual(bot.load_state()["holdings"], {})
        self.assertEqual(self.journal_actions(), ["HOLDING_DROPPED"])

    def test_fractional_holding_is_sold_in_full(self):
        self.set_state({"holdings": {"BOT": {"qty": "2.5", "cost": "250"}}})
        self.broker.positions = [position("BOT", 2.5, 98.0)]
        self.run_cycle([])
        self.assertEqual(self.broker.posted[0]["qty"], "2.5")

    def test_exit_fill_clears_the_holding(self):
        self.set_state({"holdings": {"BOT": {"qty": "5", "cost": "500"}}})
        self.broker.positions = [position("BOT", 5, 98.0)]
        self.broker.post_mode = "fill"
        self.run_cycle([])
        self.assertEqual(bot.load_state()["holdings"], {})
        self.assertEqual(self.journal_actions(), ["EXIT_SL_SUBMITTED", "EXIT_SL"])


class Exposure(BotTestCase):
    def test_order_that_would_exceed_cap_is_skipped(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.positions = [position("USER", 90, 100.0)]  # 9% of 100k
        self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_pending_buy_reserves_capacity(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.set_state({"orders": {"c1": {
            "symbol": "ZZZ", "side": "buy", "qty": "90", "kind": "entry", "reason": "r",
            "signal_price": 100.0, "status": "pending", "order_id": "o",
            "submitted_at": bot.now_local().isoformat(), "filled_qty": "0", "filled_notional": "0"}}})
        self.broker.orders_by_cid["c1"] = {"id": "o", "status": "accepted",
                                           "filled_qty": "0", "filled_avg_price": None}
        self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_order_within_cap_is_placed(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.broker.positions = [position("USER", 70, 100.0)]  # 7% + 2% fits under 10%
        self.run_cycle(["AAA"])
        self.assertEqual(len(self.broker.posted), 1)


class Cooldown(BotTestCase):
    def test_cooldown_blocks_entry(self):
        self.broker.bars["AAA"] = make_bars("down")
        until = (bot.now_local() + timedelta(minutes=30)).isoformat()
        self.set_state({"cooldowns": {"AAA": until}})
        self.run_cycle(["AAA"])
        self.assertEqual(self.broker.posted, [])

    def test_cooldown_does_not_block_sell_signal(self):
        self.broker.bars["AAA"] = make_bars("up")
        until = (bot.now_local() + timedelta(minutes=30)).isoformat()
        self.set_state({"cooldowns": {"AAA": until},
                        "holdings": {"AAA": {"qty": "20", "cost": "2000"}}})
        self.broker.positions = [position("AAA", 20, 100.2)]  # inside TP/SL
        self.run_cycle(["AAA"])
        self.assertEqual(len(self.broker.posted), 1)
        self.assertEqual(self.broker.posted[0]["side"], "sell")
        self.assertIn("EXIT_SIGNAL_SUBMITTED", self.journal_actions())


class DryRun(BotTestCase):
    def test_dry_run_places_nothing_and_keeps_state(self):
        self.broker.bars["AAA"] = make_bars("down")
        self.run_cycle(["AAA"], dry_run=True)
        self.assertEqual(self.broker.posted, [])
        self.assertFalse(bot.STATE_FILE.exists())
        self.assertEqual(self.journal_actions(), ["ENTER_DRY"])
        self.assertEqual(bot.today_entry_count(include_dry=False), 0)


if __name__ == "__main__":
    unittest.main()
