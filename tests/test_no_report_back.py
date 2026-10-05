"""语音场景：派出去的活不回灌发起方话题。

真实故障：用户在电话里派活到 spx 工作域，活确实跑去了 spx 群，
但子任务回报 + 批次完成唤醒把一大段汇总全发回了发起通话的 cc-lark 话题。
根因是 dispatch 带了父上下文（bot 侧 `if parent_thread and parent_anchor` 就登记回报闭环）。
"""
import importlib
import os

import pytest


def _reload(monkeypatch, **env):
    for k in ("CC_LARK_NO_REPORT_BACK", "CC_LARK_THREAD_ID", "CC_LARK_ANCHOR",
              "CC_LARK_MESSAGE_ID", "CC_LARK_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import cc_mcp_server
    return importlib.reload(cc_mcp_server)


BASE = {
    "CC_LARK_CHAT_ID": "oc_test",
    "CC_LARK_THREAD_ID": "omt_parent",
    "CC_LARK_ANCHOR": "om_parent",
}


def test_default_keeps_report_back(monkeypatch):
    """默认行为不变：带父上下文 → bot 侧照常登记回报闭环。"""
    m = _reload(monkeypatch, **BASE)
    assert m._NO_REPORT_BACK is False


@pytest.mark.parametrize("val", ["1", "true", "yes"])
def test_switch_strips_parent_context(monkeypatch, val):
    """开关打开 → 不传父上下文，于是既不回报也不唤醒发起方。"""
    m = _reload(monkeypatch, **BASE, CC_LARK_NO_REPORT_BACK=val)
    assert m._NO_REPORT_BACK is True


def test_switch_off_values(monkeypatch):
    """空值 / 0 / 乱填都视为关闭，不能误伤默认回报。"""
    for val in ("", "0", "no", "off"):
        m = _reload(monkeypatch, **BASE, CC_LARK_NO_REPORT_BACK=val)
        assert m._NO_REPORT_BACK is False, f"{val!r} 不该打开开关"


def teardown_module(_m):
    """把模块还原成不带开关的状态，免得污染同进程里的其它测试。"""
    for k in ("CC_LARK_NO_REPORT_BACK",):
        os.environ.pop(k, None)
    import cc_mcp_server
    importlib.reload(cc_mcp_server)
