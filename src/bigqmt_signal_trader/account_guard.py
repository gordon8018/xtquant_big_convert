# coding: utf-8
"""账户级聚合风控（下单前置闸门）。

在交易日闸门（market_guard）同一钩子内执行，submit_* 与原生 passorder 直通
必经。检查顺序与语义：

1. **kill 熔断**（Redis `bigqmt:risk:kill:<account>`）→ 拒绝一切提交（买+卖）。
   人工全局急停，优先级最高，不受 BIGQMT_GUARD 开关影响——闸门可以关，急停不能。
2. BIGQMT_GUARD=False → 其余检查全部跳过（kill 仍生效）。
3. **卖出永不拦**（降风险方向），直接放行。
4. **停买标志**（`bigqmt:risk:stopbuy:<account>`，值 `YYYYMMDD|原因`，隔日自动失效）
   → 拒买。可由日亏熔断自动写入，也可由 risk_ctl 人工设置。
5. **日亏熔断**：当日总资产回撤 ≥ daily_loss_stop_pct → 写停买标志并拒买。
   基线 = 当日首次资产快照，存 Redis（策略重启不丢，跨日自动重建）。
6. **总仓位占比**：持仓市值 + 本单金额 > max_total_ratio × 总资产 → 拒买。
7. **单票占比**：该票持仓市值 + 本单金额 > max_single_ratio × 总资产 → 拒买该单。

数据源：桥内 position_provider（QMT 原生 get_trade_detail_data，零漂移）。
查询/Redis 失败 fail-closed（60s 重试），与 market_guard 同一哲学：
错杀一天代价已知，失控下单代价未知。

限值默认是"跑偏保护轨"，不是策略仓位政策——策略自身的 SOP 限额照旧：
    max_total_ratio = 0.98   总仓位市值 / 总资产
    max_single_ratio = 0.40  单票市值 / 总资产
    daily_loss_stop_pct = 0.10 当日总资产回撤
部署配置可覆盖（bigqmt_signal_trader_local_config.py）：
    BIGQMT_GUARD = False                       # 关闭除 kill 外的全部检查（默认 True）
    BIGQMT_GUARD_MAX_TOTAL_RATIO / _MAX_SINGLE_RATIO / _DAILY_LOSS_STOP_PCT
运行期覆盖（Mac 侧 deploy/risk_ctl.py 写 Redis `bigqmt:risk:cfg:<account>`）。

已知近似：买入金额 = volume × price（LATEST_PRICE 用行情最新价估算）；
持仓快照有 5s 缓存，连续快速下单的第二笔可能看到略旧的仓位——这是
"跑偏保护轨"级别的近似，不追求逐笔精确。
"""
import datetime as _dt
import json as _json
import logging as _logging
import time as _time

log = _logging.getLogger("bigqmt.account_guard")

_FAIL_RETRY_SECONDS = 60.0
_SNAPSHOT_TTL_SECONDS = 5.0

_redis_client = None
_fail_state = (0.0, "")              # (monotonic, reason) 最近一次"数据不可用"拒绝
_asset_cache = (0.0, 0.0)            # (monotonic, total)
_pos_cache = (0.0, None)             # (monotonic, {"by_code": {...}, "total": float})


def reset_cache() -> None:
    """测试钩子：清空全部进程内缓存。"""
    global _fail_state, _asset_cache, _pos_cache
    _fail_state = (0.0, "")
    _asset_cache = (0.0, 0.0)
    _pos_cache = (0.0, None)


def set_redis(client) -> None:
    """测试钩子：注入 Redis 客户端。"""
    global _redis_client
    _redis_client = client


def _cfg(name, default):
    try:
        import bigqmt_signal_trader_local_config as _cfg_mod
    except Exception:
        return default
    return getattr(_cfg_mod, name, default)


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis as _redis_mod
        cfg = dict(_cfg("BIGQMT_REDIS_CONFIG", {}) or {})
        # 只取连接参数：配置里还有 transport/rpc_* 等桥自用键，塞给
        # redis.Redis 会 TypeError（2026-10-06 生产实证，fail-closed 兜住）。
        conn = {k: cfg[k] for k in ("host", "port", "db", "username", "password")
                if k in cfg}
        conn.setdefault("decode_responses", True)
        _redis_client = _redis_mod.Redis(**conn)
    return _redis_client


def _text(v):
    return v.decode("utf-8") if isinstance(v, bytes) else (v or "")


def _limits(account):
    lim = {
        "max_total_ratio": float(_cfg("BIGQMT_GUARD_MAX_TOTAL_RATIO", 0.98)),
        "max_single_ratio": float(_cfg("BIGQMT_GUARD_MAX_SINGLE_RATIO", 0.40)),
        "daily_loss_stop_pct": float(_cfg("BIGQMT_GUARD_DAILY_LOSS_STOP_PCT", 0.10)),
    }
    try:
        raw = _redis().get("bigqmt:risk:cfg:%s" % account)
        if raw:
            over = _json.loads(_text(raw))
            for key in lim:
                if over.get(key) is not None:
                    lim[key] = float(over[key])
    except Exception as exc:
        log.warning("account_guard %s: 限值覆盖读取失败(用默认): %s", account, exc)
    return lim


def _flags(account, today):
    r = _redis()
    kill, stop = r.mget("bigqmt:risk:kill:%s" % account,
                        "bigqmt:risk:stopbuy:%s" % account)
    stop = _text(stop)
    if stop and stop.split("|", 1)[0] != today:
        stop = ""  # 停买标志隔日自动失效(惰性清理)
    return {"kill": _text(kill), "stopbuy": stop}


def _fail(reason):
    global _fail_state
    ts = _time.monotonic()
    kind = reason.split(":", 1)[0]
    if _fail_state[1] and ts - _fail_state[0] < _FAIL_RETRY_SECONDS \
            and _fail_state[1].split(":", 1)[0] == kind:
        return (False, _fail_state[1])
    _fail_state = (ts, reason)
    log.warning("account_guard: %s", reason)
    return (False, reason)


def _asset_total(handler, account):
    global _asset_cache
    now = _time.monotonic()
    if _asset_cache[1] > 0 and now - _asset_cache[0] < _SNAPSHOT_TTL_SECONDS:
        return _asset_cache[1], ""
    try:
        asset = handler.position_provider.get_asset(account)
    except Exception as exc:
        return 0.0, "ASSET_UNAVAILABLE:get_asset failed: %s" % exc
    total = 0.0
    for attr in ("total_asset", "m_dBalance", "balance", "total"):
        v = getattr(asset, attr, None)
        if v:
            total = float(v)
            break
    if total <= 0:
        return 0.0, "ASSET_UNAVAILABLE:total<=0"
    _asset_cache = (now, total)
    return total, ""


def _positions(handler, account):
    global _pos_cache
    now = _time.monotonic()
    if _pos_cache[1] is not None and now - _pos_cache[0] < _SNAPSHOT_TTL_SECONDS:
        return _pos_cache[1], ""
    try:
        rows = handler.position_provider.get_positions(account) or []
    except Exception as exc:
        return None, "POSITIONS_UNAVAILABLE:get_positions failed: %s" % exc
    by_code, total = {}, 0.0
    for row in rows:
        code = str(getattr(row, "stock_code", "") or "")
        volume = float(getattr(row, "volume", 0) or 0)
        if not code or volume <= 0:
            continue
        mv = getattr(row, "market_value", None)
        if mv is None:
            mv = getattr(row, "m_dInstrumentValue", 0) or 0
        by_code[code] = by_code.get(code, 0.0) + float(mv or 0)
        total += float(mv or 0)
    snap = {"by_code": by_code, "total": total}
    _pos_cache = (now, snap)
    return snap, ""


def _passorder_is_sell(params):
    for key in ("op_type", "opType", "optype"):
        if params.get(key) is not None:
            try:
                return int(params[key]) == 1  # passorder: 0=买入, 1=卖出
            except (TypeError, ValueError):
                return False
    return False


def _order_amount(handler, method, params):
    """-> (code, amount|None, err)。LATEST_PRICE 用行情最新价估算。"""
    if method == "passorder":
        def pick(*names):
            for name in names:
                if params.get(name) is not None:
                    return params[name]
            return None
        code = pick("stock_code", "code", "instrument", "stockcode")
        volume = pick("volume", "order_volume", "amount")
        price = pick("price", "order_price")
        if code is None or volume is None or price is None:
            return "", None, "PASSORDER_UNPARSABLE:缺 code/volume/price"
        return str(code), float(volume) * float(price), None
    code = str(params.get("stock_code") or "")
    volume = float(params.get("volume") or params.get("order_volume") or 0)
    price = float(params.get("price") or 0)
    if not code or volume <= 0:
        return code, None, "ORDER_UNPARSABLE:%s" % (code or "empty")
    if price <= 0:
        try:
            ticks = handler.market_data.get_full_tick([code]) or {}
            price = float((ticks.get(code) or {}).get("lastPrice") or 0)
        except Exception:
            price = 0.0
        if price <= 0:
            return code, None, "PRICE_UNAVAILABLE:%s" % code
    return code, volume * price, None


def _check_buy(handler, account, method, params, today):
    lim = _limits(account)
    code, amount, err = _order_amount(handler, method, params)
    if err:
        return _fail(err)
    total, err = _asset_total(handler, account)
    if err:
        return _fail(err)
    try:
        r = _redis()
        base_key = "bigqmt:risk:baseline:%s" % account
        raw = r.get(base_key)
        base = _json.loads(_text(raw)) if raw else {}
        if not base.get("total") or base.get("date") != today:
            base = {"date": today, "total": total}
            r.set(base_key, _json.dumps(base))
        drop = (float(base["total"]) - total) / float(base["total"])
        if drop >= lim["daily_loss_stop_pct"]:
            reason = ("DAILY_LOSS_STOP:drawdown=%.2f%%>=%.0f%% "
                      "(base=%.0f now=%.0f)" % (
                          drop * 100, lim["daily_loss_stop_pct"] * 100,
                          float(base["total"]), total))
            r.set("bigqmt:risk:stopbuy:%s" % account, "%s|%s" % (today, reason))
            log.warning("account_guard %s: %s", account, reason)
            return (False, reason)
    except Exception as exc:
        return _fail("RISK_STATE_UNAVAILABLE:%s" % exc)
    pos, err = _positions(handler, account)
    if err:
        return _fail(err)
    if pos["total"] + amount > lim["max_total_ratio"] * total:
        return (False, "TOTAL_POSITION_CAP:%.0f+%.0f>%.0f%%x%.0f" % (
            pos["total"], amount, lim["max_total_ratio"] * 100, total))
    single_before = pos["by_code"].get(code, 0.0)
    if single_before + amount > lim["max_single_ratio"] * total:
        return (False, "SINGLE_STOCK_CAP:%s %.0f+%.0f>%.0f%%x%.0f" % (
            code, single_before, amount, lim["max_single_ratio"] * 100, total))
    return None


def check_submit_allowed(handler, method, params, now=None):
    """-> None=放行；(False, reason)=拒绝。仅对 submit 方法调用（钩子保证）。"""
    now = now or _dt.datetime.now()
    today = now.strftime("%Y%m%d")
    account = str(getattr(handler, "account_id", "") or "")
    try:
        flags = _flags(account, today)
    except Exception as exc:
        return _fail("RISK_FLAGS_UNAVAILABLE:%s" % exc)
    if flags.get("kill"):
        return (False, "KILL_SWITCH:%s" % flags["kill"])
    if not _cfg("BIGQMT_GUARD", True):
        return None
    if method == "passorder":
        is_sell = _passorder_is_sell(params)
    else:
        try:
            action = handler._order_action_from_params(params)
        except Exception:
            action = "BUY"  # 判不出按买入从严
        is_sell = str(action).upper() == "SELL"
    if is_sell:
        return None
    if flags.get("stopbuy"):
        return (False, "STOP_BUY:%s" % flags["stopbuy"])
    if method == "submit_orders_batch":
        for item in (params.get("orders") or []):
            try:
                if str(handler._order_action_from_params(item or {})).upper() == "SELL":
                    continue
            except Exception:
                pass
            block = _check_buy(handler, account, "submit_order", item or {}, today)
            if block is not None:
                return block
        return None
    return _check_buy(handler, account, method, params, today)
