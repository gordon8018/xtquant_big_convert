#!/usr/bin/env python3
# coding: utf-8
"""账户风控操作台（Mac 侧）——kill / 停买 / 限值 / 状态，走 Redis 标志立即生效。

与桥内 account_guard 配套（xtquant_big_convert 0.3.58+）。kill = 全局急停：
拒绝该账户一切新提交（买+卖）+ 撤销全部可撤委托 + 推企微。桥在每个 submit
前读标志，无需重启策略。

用法（建议 hope venv 运行，自带 redis 依赖；需 PYTHONPATH 含 ~/qmt_client）:
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py status
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py kill --reason "人工熔断"
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py resume
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py stopbuy --reason "..."
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py stopbuy-clear
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py set-limits --max-total 0.9 --max-single 0.3 --daily-loss 0.05
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py clear-limits
  PYTHONPATH=~/qmt_client python deploy/risk_ctl.py status --account 8890323587

账户默认取 bigqmt_signal_trader_client_config.BIGQMT_ACCOUNT_ID，--account 覆盖。
企微通知: WECOM_WEBHOOK_URL 环境变量或 bigqmt_dashboard/config/notify.json 的
wecom_webhook；都没有则只打日志不发送。
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qmt_cli  # noqa: E402  复用 redis 配置与交易客户端

RISK = "bigqmt:risk"


def redis():
    return qmt_cli.raw_redis()


def account(args):
    value = str(getattr(args, "account", "") or "").strip()
    if not value:
        value = str(getattr(qmt_cli.cfg, "BIGQMT_ACCOUNT_ID", "") or "").strip()
    if not value:
        sys.exit("account id 为空: 用 --account 或配置 client config")
    return value


def _wecom_webhook():
    url = os.environ.get("WECOM_WEBHOOK_URL", "")
    if url:
        return url
    path = os.path.expanduser(
        "~/.zcode/workspace/default/bigqmt_dashboard/config/notify.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("wecom_webhook") or ""
    except (OSError, ValueError):
        return ""


def notify(title, content):
    url = _wecom_webhook()
    text = "**%s**\n%s" % (title, content)
    if not url:
        print("[notify] 未配置企微 webhook, 仅日志: %s" % text)
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps(
                {"msgtype": "markdown", "markdown": {"content": text}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("[notify] 企微已推送")
    except Exception as exc:
        print("[notify] 企微推送失败: %s" % exc)


def _cancel_all(acc_id):
    trader = qmt_cli.make_trader(acc_id)
    stock_account = qmt_cli.as_stock_account(acc_id)
    orders = trader.query_stock_orders(stock_account, cancelable_only=True) or []
    done = failed = 0
    for od in orders:
        try:
            if trader.cancel_order_stock(stock_account, od.order_id) == 0:
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return len(orders), done, failed


def cmd_kill(args):
    acc = account(args)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    redis().set("%s:kill:%s" % (RISK, acc), "%s|%s" % (stamp, args.reason or "manual"))
    print("kill 已设置（桥每个 submit 实时读取，无需重启）")
    total, done, failed = _cancel_all(acc)
    print("可撤委托 %d 笔: 撤销成功 %d, 失败 %d" % (total, done, failed))
    notify("风险急停 %s" % acc, "reason: %s\n可撤 %d 笔, 撤销 %d, 失败 %d"
           % (args.reason or "manual", total, done, failed))


def cmd_resume(args):
    acc = account(args)
    redis().delete("%s:kill:%s" % (RISK, acc))
    print("kill 已解除: %s" % acc)


def cmd_stopbuy(args):
    acc = account(args)
    val = "%s|%s" % (datetime.now().strftime("%Y%m%d"), args.reason or "manual")
    redis().set("%s:stopbuy:%s" % (RISK, acc), val)
    print("stopbuy 已设置（隔日自动失效）: %s" % val)


def cmd_stopbuy_clear(args):
    acc = account(args)
    redis().delete("%s:stopbuy:%s" % (RISK, acc))
    print("stopbuy 已清除: %s" % acc)


def cmd_set_limits(args):
    acc = account(args)
    over = {}
    for key, val in (("max_total_ratio", args.max_total),
                     ("max_single_ratio", args.max_single),
                     ("daily_loss_stop_pct", args.daily_loss)):
        if val is not None:
            over[key] = val
    if not over:
        sys.exit("没有可设置的限值: --max-total / --max-single / --daily-loss")
    redis().set("%s:cfg:%s" % (RISK, acc), json.dumps(over))
    print("限值覆盖已写入: %s" % over)


def cmd_clear_limits(args):
    acc = account(args)
    redis().delete("%s:cfg:%s" % (RISK, acc))
    print("限值覆盖已清除（回到配置文件/默认值）: %s" % acc)


def _dec(v):
    return v.decode("utf-8") if isinstance(v, bytes) else (v or "")


def cmd_status(args):
    acc = account(args)
    r = redis()
    print("account: %s" % acc)
    print("kill: %s" % (_dec(r.get("%s:kill:%s" % (RISK, acc))) or "(无)"))
    print("stopbuy: %s" % (_dec(r.get("%s:stopbuy:%s" % (RISK, acc))) or "(无)"))
    print("baseline: %s" % (_dec(r.get("%s:baseline:%s" % (RISK, acc))) or "(未建立)"))
    print("limits override: %s" % (_dec(r.get("%s:cfg:%s" % (RISK, acc))) or "(默认)"))
    try:
        trader = qmt_cli.make_trader(acc)
        asset = trader.query_stock_asset(qmt_cli.as_stock_account(acc))
        if asset is not None:
            print("live total_asset: %s" % getattr(asset, "total_asset", None))
    except Exception as exc:
        print("live asset: 查询失败 %s" % exc)


def main():
    ap = argparse.ArgumentParser(description="账户风控操作台 (kill/停买/限值/状态)")
    ap.add_argument("--account", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("kill")
    p.add_argument("--reason", default="")
    sub.add_parser("resume")
    p = sub.add_parser("stopbuy")
    p.add_argument("--reason", default="")
    sub.add_parser("stopbuy-clear")
    p = sub.add_parser("set-limits")
    p.add_argument("--max-total", type=float, dest="max_total")
    p.add_argument("--max-single", type=float, dest="max_single")
    p.add_argument("--daily-loss", type=float, dest="daily_loss")
    sub.add_parser("clear-limits")
    args = ap.parse_args()
    handlers = {
        "status": cmd_status, "kill": cmd_kill, "resume": cmd_resume,
        "stopbuy": cmd_stopbuy, "stopbuy-clear": cmd_stopbuy_clear,
        "set-limits": cmd_set_limits, "clear-limits": cmd_clear_limits,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
