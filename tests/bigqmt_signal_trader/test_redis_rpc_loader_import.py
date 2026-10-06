# coding: utf-8
"""QMT 加载器语义回归：`from . import X` 静默不绑定，必须 `from .x import y`。

QMT 沙箱加载器对 fromlist 一律返回子模块本身（BIGQMT_REDIS_DRYRUN._local_import
的 `if fromlist: return module`），IMPORT_FROM 在返回的子模块上取 `子模块.子模块名`
属性失败后既不抛错也不绑定（本地 3.13 复现：EXEC_OK 且不绑定）。2026-10-06 生产
桥重启后 ping 全线 `NameError: name 'market_guard' is not defined`，即此坑。
本测试用同一语义 exec redis_rpc 源码，锁死导入形式。
"""
import builtins
import importlib
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

SRC = os.path.join(ROOT, "src", "bigqmt_signal_trader", "redis_rpc.py")


def _exec_like_qmt_loader():
    mod = types.ModuleType("bigqmt_signal_trader.redis_rpc")
    mod.__package__ = "bigqmt_signal_trader"

    def fake_local_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
        if level:
            pkg = (module_globals or {}).get("__package__") or (module_globals or {}).get("__name__", "")
            absolute = pkg + ("." + name if name else "")
        else:
            absolute = name
        return importlib.import_module(absolute)  # loader 语义: fromlist 也返回子模块

    mod.__dict__["__builtins__"] = dict(builtins.__dict__, __import__=fake_local_import)
    with open(SRC, "rb") as f:
        source = f.read()
    exec(compile(source, SRC, "exec"), mod.__dict__)
    return mod


def test_check_order_allowed_is_bound_under_loader_semantics():
    mod = _exec_like_qmt_loader()
    assert "check_order_allowed" in mod.__dict__
    assert "SUBMIT_METHODS" in mod.__dict__


def test_no_from_dot_import_self_module():
    import re
    with open(SRC, "r", encoding="utf-8") as f:
        source = f.read()
    # 只匹配行首真实导入语句；注释里的文字不算
    assert re.search(r"^from \. import ", source, re.M) is None  # 该形式在 QMT 加载器下静默不绑定
