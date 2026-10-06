# coding: utf-8
"""交易日闸门：非交易日/日历不可用必须拒绝下单，cancel 不受影响。

背景：2026-10-06 国庆休市日，上游策略的卖出路径把真实委托打进了券商
（客户端回"下单成功"，大概率废单或留存到节后开盘成交）。桥是所有委托的
必经之地，在这里 fail-closed 一层，策略侧各自的日历缺口就不再是资金风险。
周末走本地判定不查 xtdata；交易日查 xtdata.get_trading_dates（QMT 终端
本地日历）；查询失败 fail-closed，60s 后重试。
"""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pytest

from bigqmt_signal_trader import market_guard


@pytest.fixture(autouse=True)
def _fresh():
    market_guard.reset_cache()
    yield
    market_guard.reset_cache()


def test_weekend_rejected_without_xtdata(monkeypatch):
    def boom(d):
        raise AssertionError("周末不应查日历")

    monkeypatch.setattr(market_guard, "_trading_dates", boom)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 10, 9, 14))  # 周六
    assert ok is False
    assert reason.startswith("MARKET_CLOSED")


def test_holiday_rejected(monkeypatch):
    monkeypatch.setattr(market_guard, "_trading_dates", lambda d: ({"20261008"}, ""))
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 7, 9, 14))  # 国庆周三
    assert ok is False
    assert reason.startswith("MARKET_CLOSED")


def test_trading_day_allowed(monkeypatch):
    monkeypatch.setattr(market_guard, "_trading_dates", lambda d: ({"20261008", "20261009"}, ""))
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is True and reason == ""


def test_calendar_unavailable_fail_closed(monkeypatch):
    monkeypatch.setattr(market_guard, "_trading_dates", lambda d: (None, "boom"))
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is False
    assert reason.startswith("MARKET_CALENDAR_UNAVAILABLE")


def test_guard_disabled_allows(monkeypatch):
    import types
    cfg = types.ModuleType("bigqmt_signal_trader_local_config")
    cfg.BIGQMT_MARKET_GUARD = False

    def boom(d):
        raise AssertionError("闸门关闭时不应查日历")

    monkeypatch.setattr(market_guard, "_trading_dates", boom)
    monkeypatch.setitem(__import__("sys").modules,
                        "bigqmt_signal_trader_local_config", cfg)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 7, 9, 14))
    assert ok is True and reason == ""


def test_fail_result_retried_after_ttl(monkeypatch):
    monkeypatch.setattr(market_guard, "_trading_dates", lambda d: (None, "boom"))
    ok, _ = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is False
    fail_ts = market_guard._fail[0]
    # 60s 内: 复用失败结果
    monkeypatch.setattr(market_guard._time, "monotonic", lambda: fail_ts + 10)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is False and reason.startswith("MARKET_CALENDAR_UNAVAILABLE")
    # 超过 60s: 重新查询并成功
    monkeypatch.setattr(market_guard._time, "monotonic", lambda: fail_ts + 61)
    monkeypatch.setattr(market_guard, "_trading_dates", lambda d: ({"20261008"}, ""))
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is True and reason == ""
