# coding: utf-8
"""下单前置交易日闸门（A 股）。

submit_* 与原生 passorder 直通在触达 passorder 前先过本闸门：非交易日
（周末/节假日）一律拒绝；cancel_* 与查询类方法不拦。

日历查询双路（2026-10-12，上游 issue #160 同款）：
1. 原生 xtdata SDK —— 大 QMT 进程里可能连不上行情服务（报「无法连接行情
   服务」，且不会自愈），失败按模块记 600s 不再重拨，省掉每次 ~2s 的拨号
   超时；
2. 回落 ContextInfo.get_trading_dates（进程内直连；第一参数是证券代码而
   非市场码，SH 用代表指数 000001.SH 代替——与上游 market_bigqmt 同款）。
两条路都失败才 fail-closed：错杀一个交易日的代价已知且有限，休市日下单
的代价未知（废单，或订单留存到下一交易日按当时价格成交，策略本意已失效；
2026-10-06 休市日 hope/ors 曾各提交真实卖单）。日历失败结果缓存 60s 后
重试，成功判定按日缓存。
返回值归一为 YYYYMMDD 串，兼容整数日期、13 位毫秒时间戳与 QMT 14 位柱戳。

可用部署配置关闭（bigqmt_signal_trader_local_config.py）：
    BIGQMT_MARKET_GUARD = False   # 默认 True
"""
import datetime as _dt
import time as _time

SUBMIT_METHODS = ("submit_order", "submit_orders_batch", "passorder")
_FAIL_RETRY_SECONDS = 60.0
_NATIVE_FAILURE_CACHE_SECONDS = 600.0  # issue #160: SDK 失败后 600s 内不重拨

_cached_day = ""        # "YYYYMMDD"，当日判定已缓存
_cached = (True, "")    # (allowed, reason)
_fail = (0.0, "")       # (monotonic, reason) 最近一次日历不可用
_native_dead_at = 0.0   # (monotonic) 最近一次 SDK 日历失败，600s 内跳过 SDK

_context_info = None    # 测试可注入；运行时未注入则从策略单例懒取


def set_context_info(ci):
    """显式注入 ContextInfo（测试用）；生产走运行时单例懒取。"""
    global _context_info
    _context_info = ci


def _context_info_from_module(m):
    """-> module 持有的 ContextInfo 或 None; 任意一环异常都吞掉。"""
    try:
        svc = getattr(m, "_rpc_service", None)
        md = getattr(getattr(svc, "handlers", None), "market_data", None)
        return getattr(md, "context_info", None)
    except Exception:
        return None


def _runtime_context_info():
    """从策略运行时单例懒取 ContextInfo。

    与 redis_rpc 内部取法同款：service.handlers.market_data.context_info。
    先按模块名找运行时（两种部署名都试），再退化为全 sys.modules 扫描；
    任何一环缺失返回 None（此时仅剩 SDK 一条路，与旧行为一致）。"""
    import sys as _sys

    for name in ("bigqmt_signal_trader_strategy",
                 "bigqmt_signal_trader_redis_rpc_runtime"):
        ci = _context_info_from_module(_sys.modules.get(name))
        if ci is not None:
            return ci
    for m in list(_sys.modules.values()):
        ci = _context_info_from_module(m)
        if ci is not None:
            return ci
    return None


def _norm_dates(raw):
    """日历返回值归一为 {'YYYYMMDD'}。

    兼容日期串/整数、13 位毫秒时间戳与 QMT 14 位 yyyymmddHHMMSS 柱戳；
    解析不了的条目跳过（宁缺勿错：缺日子的方向是 fail-closed）。"""
    out = set()
    for d in raw or []:
        try:
            s = str(d).strip()
            if s.isdigit() and len(s) > 8:
                if len(s) == 14 and s.startswith("20"):
                    s = s[:8]  # QMT yyyymmddHHMMSS 柱戳
                else:
                    n = int(s)
                    s = (_dt.datetime.fromtimestamp(n / 1000.0).strftime("%Y%m%d")
                         if n > 10 ** 12 else s[:8])
            if len(s) == 8 and s.isdigit():
                out.add(s)
        except Exception:
            continue
    return out


def _guard_enabled() -> bool:
    try:
        import bigqmt_signal_trader_local_config as _cfg
    except Exception:
        return True
    return bool(getattr(_cfg, "BIGQMT_MARKET_GUARD", True))


def _sdk_trading_dates(start, end):
    """原生 xtdata SDK。-> (set|None, err)；失败记 600s（模块级）。"""
    global _native_dead_at
    now_mono = _time.monotonic()
    if _native_dead_at and now_mono - _native_dead_at < _NATIVE_FAILURE_CACHE_SECONDS:
        return None, ("native xtdata known-dead, cached %ds" %
                      int(_NATIVE_FAILURE_CACHE_SECONDS))
    try:
        from xtquant import xtdata
    except Exception as exc:
        _native_dead_at = now_mono
        return None, "xtquant unavailable: %s" % exc
    try:
        dates = _norm_dates(xtdata.get_trading_dates(market="SH",
                                                     start_time=start,
                                                     end_time=end))
    except Exception as exc:
        _native_dead_at = now_mono
        return None, "get_trading_dates failed: %s" % exc
    if not dates:
        return None, "get_trading_dates returned empty"
    return dates, ""


def _context_trading_dates(start, end):
    """ContextInfo 回落（进程内，不依赖那颗行情服务）。-> (set|None, err)。"""
    ci = _context_info if _context_info is not None else _runtime_context_info()
    if ci is None:
        return None, "ContextInfo 不可达(运行时单例缺失)"
    try:
        # ContextInfo 第一参数是证券代码而非市场码（SH 用代表指数，上游同款）；
        # 返回值可能是日期串/整数/时间戳，_norm_dates 统一归一。
        raw = ci.get_trading_dates("000001.SH", start, end, -1)
    except Exception as exc:
        return None, "ContextInfo fallback failed: %s" % exc
    dates = _norm_dates(raw)
    if not dates:
        return None, "ContextInfo returned empty"
    return dates, ""


def _trading_dates(today: _dt.date):
    """-> (set('YYYYMMDD')|None, err)。SDK 先行，失败回落 ContextInfo；
    两条路都失败 -> (None, err)，由调用方 fail-closed。"""
    start = (today - _dt.timedelta(days=20)).strftime("%Y%m%d")
    end = (today + _dt.timedelta(days=10)).strftime("%Y%m%d")
    dates, sdk_err = _sdk_trading_dates(start, end)
    if dates:
        return dates, ""
    dates, ci_err = _context_trading_dates(start, end)
    if dates:
        return dates, ""
    return None, "SDK: %s | ContextInfo: %s" % (sdk_err, ci_err)


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
    """测试钩子：清空当日判定/失败缓存/SDK 死标记/注入的 ContextInfo。"""
    global _cached_day, _cached, _fail, _native_dead_at, _context_info
    _cached_day, _cached, _fail = "", (True, ""), (0.0, "")
    _native_dead_at = 0.0
    _context_info = None
