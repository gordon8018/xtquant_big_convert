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


# ---- 2026-10-12: SDK 连不上行情服务时回落 ContextInfo(上游 issue #160 同款) ----

class _FakeContext:
    """ContextInfo 替身: 第一参数是证券代码, 返回 YYYYMMDD 串列表。"""

    def __init__(self, dates):
        self.dates = list(dates)
        self.calls = []

    def get_trading_dates(self, stockcode, start_date, end_date, count):
        self.calls.append((stockcode, start_date, end_date, count))
        return list(self.dates)


def _install_dead_sdk(monkeypatch):
    """让 SDK 路径处于已知死亡状态(大 QMT 里「无法连接行情服务」的常态)。"""
    market_guard._native_dead_at = market_guard._time.monotonic()


def test_sdk_dead_contextinfo_fallback_allows(monkeypatch):
    _install_dead_sdk(monkeypatch)
    ctx = _FakeContext(["20261008", "20261009"])
    market_guard.set_context_info(ctx)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is True and reason == ""
    # 回落走 ContextInfo: 第一参数是证券代码(000001.SH), 不是市场码
    assert ctx.calls and ctx.calls[0][0] == "000001.SH"


def test_sdk_dead_no_context_still_fail_closed(monkeypatch):
    _install_dead_sdk(monkeypatch)
    market_guard.set_context_info(None)
    monkeypatch.setattr(market_guard, "_runtime_context_info", lambda: None)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is False
    assert reason.startswith("MARKET_CALENDAR_UNAVAILABLE")
    assert "ContextInfo" in reason and "SDK" in reason  # 两路原因都留痕


def test_sdk_dead_contextinfo_empty_still_fail_closed(monkeypatch):
    _install_dead_sdk(monkeypatch)
    market_guard.set_context_info(_FakeContext([]))
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is False
    assert "returned empty" in reason


def test_sdk_dead_cache_avoids_redial(monkeypatch):
    # SDK 死亡标记生效时, 不应再触发 SDK 导入/拨号(用必然失败的假 xtquant 验证)
    import sys, types
    _install_dead_sdk(monkeypatch)
    calls = {"n": 0}

    class _Boom:
        def __getattr__(self, name):
            calls["n"] += 1
            raise RuntimeError("无法连接行情服务")

    monkeypatch.setitem(sys.modules, "xtquant", types.ModuleType("xtquant"))
    market_guard.set_context_info(_FakeContext(["20261008"]))
    ok, _ = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is True
    assert calls["n"] == 0  # 600s 窗口内未重拨 SDK


def test_sdk_ok_wins_over_context(monkeypatch):
    import sys, types
    xtquant = types.ModuleType("xtquant")

    class _Sdk:
        def get_trading_dates(self, market, start_time, end_time):
            return ["20261008"]

    xtquant.xtdata = _Sdk()
    monkeypatch.setitem(sys.modules, "xtquant", xtquant)
    ctx = _FakeContext([])
    market_guard.set_context_info(ctx)
    ok, reason = market_guard.check_order_allowed(dt.datetime(2026, 10, 8, 9, 14))
    assert ok is True and reason == ""
    assert ctx.calls == []  # SDK 成功时不回落


def test_norm_dates_shapes():
    assert market_guard._norm_dates(["20261012", 20261012, "20261013"]) == {
        "20261012", "20261013"}
    # 13 位毫秒时间戳 -> 当地日期
    ms = int(dt.datetime(2026, 10, 12, 0, 0).timestamp() * 1000)
    assert market_guard._norm_dates([ms]) == {"20261012"}
    # QMT 14 位柱戳
    assert market_guard._norm_dates(["20261012093000"]) == {"20261012"}
    # 垃圾条目跳过
    assert market_guard._norm_dates(["x", None, ""]) == set()
