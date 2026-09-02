"""system prompt 在同一话题内必须逐轮字节一致（prompt cache 前缀命中）。

线上 7 天统计：216 次续轮只有 2 次首调命中缓存，其余每轮把整段历史（300k~750k
tokens）重写一遍——根因是 --append-system-prompt 里嵌了本轮消息 id。对照实验：
只改一个 id 就从全命中退回全重写。修复后本轮 id 走用户消息开头的【本轮】行 +
CC_LARK_MESSAGE_ID env，system prompt 里不再有任何每轮都变的字段。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
from bot_config import Profile
from lark_prompts import render_lark_prompt


def _profile(**kw):
    base = dict(
        name="spx", app_id="app", app_secret="secret", platform="lark",
        domain="https://open.larksuite.com", default_cwd="/tmp",
        lark_cli_profile="spx",
    )
    base.update(kw)
    return Profile(**base)


def _render(msg_id, asker, runner="claude", thread="omt_1"):
    return render_lark_prompt(
        profile=_profile(), raw_chat_id="oc_1", thread_id=thread,
        user_message_id=msg_id, is_group=True, asker_open_id=asker, runner=runner,
    )


def test_system_prompt_is_identical_across_turns_in_same_thread():
    a = _render("om_turn_A", "ou_alice")
    b = _render("om_turn_B", "ou_bob")
    assert a == b, "同一话题两轮的 system prompt 必须字节一致，否则 prompt cache 全失效"
    assert "om_turn_A" not in a and "ou_alice" not in a
    assert "$CC_LARK_MESSAGE_ID" in a, "回复锚点应改走 env 变量"
    assert "【本轮" in a, "应告诉 agent 本轮 id 在用户消息开头的【本轮】行"


def test_system_prompt_also_stable_for_codex_and_private_chat():
    a = render_lark_prompt(profile=_profile(runner="codex"), raw_chat_id="ou_u", thread_id="",
                           user_message_id="om_1", is_group=False, asker_open_id="ou_u", runner="codex")
    b = render_lark_prompt(profile=_profile(runner="codex"), raw_chat_id="ou_u", thread_id="",
                           user_message_id="om_2", is_group=False, asker_open_id="ou_u", runner="codex")
    assert a == b
    assert "om_1" not in a


def test_create_doc_uses_current_lark_cli_flags():
    out = _render("om_1", "ou_1")
    assert "--doc-format markdown --content @" in out
    assert '--markdown "' not in out, "v1 的 --markdown 已被 lark-cli 下线，命令模板里不能再出现"


def test_timeout_rules_are_rendered_from_config(monkeypatch):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "0")
    monkeypatch.delenv("SPX_CLAUDE_WALL_CLOCK_LIMIT_SEC", raising=False)
    out = _render("om_1", "ou_1")
    assert "未设单轮 wall-clock 上限" in out
    assert "60 分钟 wall-clock" not in out and "15/60min" not in out
    assert "15 分钟" in out  # STUCK_CHILD_TIMEOUT=900 渲染出来的

    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "7200")
    out2 = _render("om_1", "ou_1")
    assert "单轮 2 小时 wall-clock → 强杀" in out2


def test_turn_header_carries_message_id_and_asker():
    h = dispatcher._turn_header("om_x", "ou_y")
    assert h.startswith("【本轮") and "om_x" in h and "ou_y" in h and h.endswith("\n\n")
    assert dispatcher._turn_header("", "") == ""
