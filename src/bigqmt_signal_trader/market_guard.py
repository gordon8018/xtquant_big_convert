# coding: utf-8
"""下单前置交易日闸门（A 股）。

submit_* 与原生 passorder 直通在触达 passorder 前先过本闸门：非交易日
（周末/节假日）一律拒绝；cancel_* 与查询类方法不拦。日历取 QMT 终端本地
xtdata.get_trading_dates（终端自带，权威且无外部依赖）。

fail-closed：xtquant 不可用（单测/非 QMT 环境）或日历查询失败时同样拒绝——
错杀一个交易日的代价已知且有限，休市日下单的代价未知（废单，或订单留存到
下一交易日按当时价格成交，策略本意已失效；2026-10-06 休市日 hope/ors 曾各
提交真实卖单）。日历失败结果缓存 60s 后重试，成功判定按日缓存。

可用部署配置关闭（bigqmt_signal_trader_local_config.py）：
    BIGQMT_MARKET_GUARD = False   # 默认 True
"""
import datetime as _dt
import time as _time

SUBMIT_METHODS = ("submit_order", "submit_orders_batch", "passorder")
_FAIL_RETRY_SECONDS = 60.0

_cached_day = ""        # "YYYYMMDD"，当日判定已缓存
_cached = (True, "")    # (allowed, reason)
_fail = (0.0, "")       # (monotonic, reason) 最近一次日历不可用


def _guard_enabled() -> bool:
    try:
        import bigqmt_signal_trader_local_config as _cfg
    except Exception:
        return True
    return bool(getattr(_cfg, "BIGQMT_MARKET_GUARD", True))


def _trading_dates(today: _dt.date):
    """-> (set("YYYYMMDD") | None, err)。None 表示日历不可用。"""
    try:
        from xtquant import xtdata
    except Exception as exc:
        return None, "xtquant unavailable: %s" % exc
    start = (today - _dt.timedelta(days=20)).strftime("%Y%m%d")
    end = (today + _dt.timedelta(days=10)).strftime("%Y%m%d")
    try:
        dates = xtdata.get_trading_dates(market="SH", start_time=start, end_time=end)
    except Exception as exc:
        return None, "get_trading_dates failed: %s" % exc
    return {str(d) for d in dates or []}, ""


def check_order_allowed(now=None):
    """-> (allowed, reason)。reason 随 PermissionError 上抛，供客户端留痕。"""
    global _cached_day, _cached, _fail
    now = now or _dt.datetime.now()
    today = now.strftime("%Y%m%d")
    if today == _cached_day:
        return _cached
    if not _guard_enabled():
        _cached_day, _cached = today, (True, "")
        return _cached
    if now.weekday() >= 5:
        _cached_day, _cached = today, (False, "MARKET_CLOSED:%s weekend" % today)
        return _cached
    ts = _time.monotonic()
    if _fail[0] and ts - _fail[0] < _FAIL_RETRY_SECONDS:
        return False, _fail[1]
    dates, err = _trading_dates(now.date())
    if dates is None or not dates:
        reason = "MARKET_CALENDAR_UNAVAILABLE:%s %s" % (today, err or "empty calendar")
        _fail = (ts, reason)
        return False, reason
    _fail = (0.0, "")
    if today in dates:
        _cached_day, _cached = today, (True, "")
        return _cached
    _cached_day, _cached = today, (False, "MARKET_CLOSED:%s 非交易日" % today)
    return _cached


def reset_cache() -> None:
    """测试钩子：清空当日判定与失败缓存。"""
    global _cached_day, _cached, _fail
    _cached_day, _cached, _fail = "", (True, ""), (0.0, "")
