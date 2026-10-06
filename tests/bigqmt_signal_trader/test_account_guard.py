# coding: utf-8
"""账户级聚合风控测试：kill / 停买 / 日亏熔断 / 总仓 / 单票 / 卖出放行 / 批量 / passorder。"""
import datetime as dt
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pytest

from bigqmt_signal_trader import account_guard

NOW = dt.datetime(2026, 10, 8, 9, 30)
BUY = dict(stock_code="000001.SZ", volume=1000, price=10.0, action="BUY")
SELL = dict(stock_code="000001.SZ", volume=1000, price=10.0, action="SELL")


class FakeAsset:
    def __init__(self, total):
        self.total_asset = total


class FakePosition:
    def __init__(self, code, volume, market_value):
        self.stock_code = code
        self.volume = volume
        self.market_value = market_value


class FakeProvider:
    def __init__(self, total, positions):
        self.total = total
        self.positions = positions

    def get_asset(self, account):
        if self.total is None:
            raise RuntimeError("asset down")
        return FakeAsset(self.total)

    def get_positions(self, account):
        if self.positions is None:
            raise RuntimeError("positions down")
        return self.positions


class FakeHandler:
    def __init__(self, provider, account="A1", market_data=None):
        self.account_id = account
        self.position_provider = provider
        self.market_data = market_data

    def _order_action_from_params(self, params):
        return str(params.get("action") or "BUY").upper()


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v):
        self.store[k] = v

    def delete(self, k):
        self.store.pop(k, None)

    def mget(self, *keys):
        return [self.store.get(k) for k in keys]


def _handler(total=100_000.0, positions=None, account="A1", market_data=None):
    return FakeHandler(FakeProvider(total, positions or []), account=account,
                       market_data=market_data)


@pytest.fixture(autouse=True)
def _env():
    fr = FakeRedis()
    account_guard.set_redis(fr)
    account_guard.reset_cache()
    yield fr
    account_guard.set_redis(None)
    account_guard.reset_cache()


def test_kill_blocks_buy_and_sell_and_passorder(monkeypatch):
    h = _handler()
    account_guard._redis().set("bigqmt:risk:kill:A1", "20261008T0930|人工熔断")
    monkeypatch.setattr(account_guard, "_cfg",
                        lambda n, d: False if n == "BIGQMT_GUARD" else d)
    assert account_guard.check_submit_allowed(
        h, "submit_order", BUY, NOW)[1].startswith("KILL_SWITCH")
    assert account_guard.check_submit_allowed(
        h, "submit_order", SELL, NOW)[1].startswith("KILL_SWITCH")
    assert account_guard.check_submit_allowed(
        h, "passorder", {"op_type": 1}, NOW)[1].startswith("KILL_SWITCH")


def test_sell_always_passes():
    h = _handler(positions=[FakePosition("000001.SZ", 1000, 10_000.0)])
    assert account_guard.check_submit_allowed(h, "submit_order", SELL, NOW) is None


def test_guard_disabled_allows_buy(monkeypatch):
    monkeypatch.setattr(account_guard, "_cfg",
                        lambda n, d: False if n == "BIGQMT_GUARD" else d)
    h = _handler(positions=[FakePosition("000001.SZ", 9000, 90_000.0)])
    assert account_guard.check_submit_allowed(h, "submit_order", BUY, NOW) is None


def test_normal_buy_passes_and_sets_baseline():
    h = _handler()
    assert account_guard.check_submit_allowed(h, "submit_order", BUY, NOW) is None
    base = account_guard._redis().get("bigqmt:risk:baseline:A1")
    assert base and '"date": "20261008"' in base


def test_total_position_cap():
    h = _handler(positions=[FakePosition("600000.SH", 9000, 90_000.0)])
    ok = account_guard.check_submit_allowed(h, "submit_order", BUY, NOW)
    assert ok is not None and ok[1].startswith("TOTAL_POSITION_CAP")


def test_single_stock_cap():
    h = _handler(positions=[FakePosition("000001.SZ", 3000, 30_000.0)])
    ok = account_guard.check_submit_allowed(h, "submit_order",
                                            dict(BUY, volume=2000), NOW)
    assert ok[1].startswith("SINGLE_STOCK_CAP:000001.SZ")


def test_daily_loss_stop_triggers_and_persists():
    account_guard._redis().set("bigqmt:risk:baseline:A1",
                               '{"date": "20261008", "total": 100000.0}')
    h = _handler(total=89_000.0)
    ok = account_guard.check_submit_allowed(h, "submit_order", BUY, NOW)
    assert ok[1].startswith("DAILY_LOSS_STOP")
    assert account_guard._redis().get("bigqmt:risk:stopbuy:A1").startswith("20261008|")
    account_guard.reset_cache()
    ok2 = account_guard.check_submit_allowed(h, "submit_order", BUY, NOW)
    assert ok2[1].startswith("STOP_BUY")


def test_stale_stopbuy_ignored_next_day():
    account_guard._redis().set("bigqmt:risk:stopbuy:A1", "20261007|old")
    h = _handler()
    assert account_guard.check_submit_allowed(h, "submit_order", BUY, NOW) is None


def test_asset_failure_fail_closed():
    h = _handler(total=None)
    ok = account_guard.check_submit_allowed(h, "submit_order", BUY, NOW)
    assert ok[1].startswith("ASSET_UNAVAILABLE")


def test_latest_price_uses_tick():
    md = types.SimpleNamespace(
        get_full_tick=lambda codes: {"000001.SZ": {"lastPrice": 10.0}})
    h = _handler(market_data=md)
    assert account_guard.check_submit_allowed(
        h, "submit_order", dict(BUY, price=0), NOW) is None

    def boom(codes):
        raise RuntimeError("tick down")

    h2 = _handler(market_data=types.SimpleNamespace(get_full_tick=boom))
    ok = account_guard.check_submit_allowed(
        h2, "submit_order", dict(BUY, price=0), NOW)
    assert ok[1].startswith("PRICE_UNAVAILABLE")


def test_batch_rejects_on_single_bad_item():
    h = _handler(positions=[FakePosition("000001.SZ", 3000, 30_000.0)])
    params = {"orders": [dict(SELL), dict(BUY, volume=2000)]}
    ok = account_guard.check_submit_allowed(h, "submit_orders_batch", params, NOW)
    assert ok[1].startswith("SINGLE_STOCK_CAP")


def test_passorder_buy_gated_sell_skipped():
    h = _handler(positions=[FakePosition("000001.SZ", 9000, 90_000.0)])
    ok = account_guard.check_submit_allowed(
        h, "passorder",
        {"op_type": 0, "stock_code": "600000.SH", "volume": 2000, "price": 10.0},
        NOW)
    assert ok[1].startswith("TOTAL_POSITION_CAP")
    assert account_guard.check_submit_allowed(
        h, "passorder",
        {"op_type": 1, "stock_code": "000001.SZ", "volume": 1000, "price": 10.0},
        NOW) is None


def test_redis_cfg_override_tightens_limit():
    account_guard._redis().set("bigqmt:risk:cfg:A1", '{"max_single_ratio": 0.05}')
    h = _handler()
    ok = account_guard.check_submit_allowed(h, "submit_order", BUY, NOW)
    assert ok[1].startswith("SINGLE_STOCK_CAP")
