"""工作域路由：把活派到**对的群**，而不是永远派回调用方所在的那个群。

背景（真实故障）：dispatch_task 的目标群一直只来自 CC_LARK_CHAT_ID，于是在 cc-lark
群里（或打电话时）说「去 KYT 那边查个东西」，活被派回 cc-lark 群、在 cc-lark 的目录
里跑。群 ↔ 工作目录本来就是一一对应的（.env 的 <PROFILE>_CHAT_CWD_<chat_id>），
所以「派到哪个群」就是「在哪个项目里干活」。

这份用例钉三件事：
  ① 传了 workspace → 子会话开在那个工作域的群、钉在那个目录；
  ② 省了 workspace → 目标群与从前逐字一致（向后兼容，绝不能悄悄改道）；
  ③ 名字打错 / bot 不在目标群 → 清晰报错，绝不静默派到别处。
"""

import asyncio
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import cc_mcp_server
import dispatcher
import http_server
import workspaces

CALLER_CHAT = "oc_cclark"          # 调用方（主 agent）所在的群
KYT_CHAT = "oc_kyt"                # 另一个工作域的群
PARENT_THREAD = "omt_parent"
PARENT_ANCHOR = "om_parent"
NEW_THREAD = "omt_child"


@pytest.fixture
def ws_file(tmp_path, monkeypatch):
    """一份临时路由表：绝不读本机真实的 workspaces.json（那份是机主的真 chat_id）。"""
    kyt_dir = tmp_path / "regtank" / "kyt"
    cclark_dir = tmp_path / "tools" / "feishu-claude-code"
    for d in (kyt_dir, cclark_dir):
        d.mkdir(parents=True)
    path = tmp_path / "workspaces.json"
    path.write_text(json.dumps({"workspaces": [
        {"name": "kyt", "aliases": ["链上", "KYT-bot"], "chat_id": KYT_CHAT,
         "chat_name": "kyt", "cwd": str(kyt_dir), "desc": "链上风控"},
        {"name": "cc-lark", "aliases": ["机器人"], "chat_id": CALLER_CHAT,
         "chat_name": "cc-lark", "cwd": str(cclark_dir), "desc": "机器人底座"},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(path))
    workspaces._CACHE.clear()
    yield NS(path=str(path), kyt_dir=str(kyt_dir), cclark_dir=str(cclark_dir))
    workspaces._CACHE.clear()


# ── 路由表本身 ────────────────────────────────────────────────

def test_resolve_by_name_is_case_and_separator_insensitive(ws_file):
    # 用户是**用嘴说**这些名字的，"CC Lark" / "cc-lark" / "cclark" 必须是同一个
    for spec in ("cc-lark", "CC-Lark", "cclark", "CC lark", " cc_lark "):
        ws, err = workspaces.resolve(spec)
        assert err == "" and ws.chat_id == CALLER_CHAT, spec


def test_resolve_by_alias_including_chinese(ws_file):
    for spec in ("链上", "KYT", "kyt-bot"):
        ws, err = workspaces.resolve(spec)
        assert err == "" and ws.chat_id == KYT_CHAT, spec


def test_resolve_by_chat_id_and_directory_name(ws_file):
    assert workspaces.resolve(KYT_CHAT)[0].name == "kyt"
    assert workspaces.resolve(ws_file.kyt_dir)[0].name == "kyt"
    assert workspaces.resolve("kyt")[0].cwd == ws_file.kyt_dir


def test_resolve_unknown_names_the_options(ws_file):
    ws, err = workspaces.resolve("payments-v2")
    assert ws is None
    assert "payments-v2" in err and "kyt" in err and "cc-lark" in err
    assert "当前群" in err          # 告诉模型省略参数就是原行为


def test_load_never_raises_on_missing_or_broken_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(tmp_path / "nope.json"))
    workspaces._CACHE.clear()
    assert workspaces.load() == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(broken))
    workspaces._CACHE.clear()
    assert workspaces.load() == [] and workspaces.catalog_doc() == ""
    ws, err = workspaces.resolve("kyt")
    assert ws is None and "workspaces.json" in err


def test_load_skips_half_written_entries(tmp_path, monkeypatch):
    """缺名字/缺群/重名的条目直接丢掉——半条配置制造的歧义比没有配置更糟。"""
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"workspaces": [
        {"name": "kyt", "chat_id": "oc_1"},
        {"name": "KYT", "chat_id": "oc_2"},      # 重名（归一化后同名）→ 丢
        {"name": "no-chat"},                     # 缺 chat_id → 丢
        {"chat_id": "oc_3"},                     # 缺 name → 丢
    ]}), encoding="utf-8")
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(p))
    workspaces._CACHE.clear()
    assert [w.name for w in workspaces.load()] == ["kyt"]
    assert workspaces.resolve("kyt")[0].chat_id == "oc_1"


def test_catalog_doc_lists_every_workspace_with_its_purpose(ws_file):
    doc = workspaces.catalog_doc()
    assert "kyt" in doc and "链上风控" in doc and "群「kyt」" in doc
    assert "cc-lark" in doc and "机器人底座" in doc


def test_dispatch_tool_schema_exposes_workspace_param():
    """工具说明里必须真的有 workspace 参数 + 工作域清单，否则模型根本不知道能路由。

    注意：description 是 import 时按**本机** workspaces.json 渲染的；没配置表的机器
    上该参数整个不出现（此时跳过，不把 CI 钉死在机主的本地配置上）。
    """
    props = cc_mcp_server.DISPATCH_TASK_TOOL["inputSchema"]["properties"]
    if not cc_mcp_server._WS_CATALOG:
        pytest.skip("本机没有 workspaces.json —— 该参数按设计不暴露")
    assert "workspace" in props
    assert "workspace" in cc_mcp_server.SCHEDULE_CRON_TOOL["inputSchema"]["properties"]
    desc = cc_mcp_server.DISPATCH_TASK_TOOL["description"]
    assert "WORKSPACE ROUTING" in desc
    assert cc_mcp_server._WS_CATALOG in desc
    for name in workspaces.names():
        assert name in desc


# ── MCP 工具层：解析 + payload ────────────────────────────────

@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "control-secret")
    monkeypatch.setenv("CC_LARK_PROFILE", "spx")
    monkeypatch.setenv("CC_LARK_CHAT_ID", CALLER_CHAT)
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_me")
    monkeypatch.setenv("CC_LARK_THREAD_ID", PARENT_THREAD)
    monkeypatch.setenv("CC_LARK_ANCHOR", PARENT_ANCHOR)


def _fake_http(monkeypatch, response: dict) -> dict:
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(response).encode()

    def _urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", _urlopen)
    return captured


_OK = {"ok": True, "thread_id": NEW_THREAD, "active_after": 1, "cap": 7}


def test_omitting_workspace_keeps_the_current_group(monkeypatch, mcp_env, ws_file):
    """向后兼容的硬约束：不传 workspace 时目标群仍是调用方所在的群。"""
    captured = _fake_http(monkeypatch, _OK)

    result = cc_mcp_server._tool_dispatch_task({"prompt": "去干活"})

    assert result["isError"] is False
    assert captured["body"]["chat_id"] == CALLER_CHAT
    assert captured["body"]["workspace"] == ""
    assert "workspace" not in result["content"][0]["text"]


def test_workspace_name_is_sent_to_the_bot(monkeypatch, mcp_env, ws_file):
    """客户端只传名字：chat_id / cwd 的权威解析在 bot 侧（见 http_server）。"""
    captured = _fake_http(monkeypatch, {**_OK, "workspace": "kyt", "cwd": ws_file.kyt_dir})

    result = cc_mcp_server._tool_dispatch_task({"prompt": "查一下", "workspace": "链上"})

    assert result["isError"] is False
    assert captured["body"]["workspace"] == "kyt"        # 别名已归一成规范名
    assert captured["body"]["chat_id"] == CALLER_CHAT    # 调用方自己的群照旧带上（父上下文）
    text = result["content"][0]["text"]
    assert "kyt" in text and ws_file.kyt_dir in text     # 回执要说清派到哪儿了


def test_unknown_workspace_fails_before_any_dispatch(monkeypatch, mcp_env, ws_file):
    captured = _fake_http(monkeypatch, _OK)

    result = cc_mcp_server._tool_dispatch_task({"prompt": "干活", "workspace": "spx"})

    assert result["isError"] is True
    assert "spx" in result["content"][0]["text"]
    assert "kyt" in result["content"][0]["text"]         # 把可选项摆出来
    assert captured == {}                                # 一个请求都没发出去


def test_schedule_cron_also_routes(monkeypatch, mcp_env, ws_file):
    captured = _fake_http(monkeypatch, {"ok": True, "name": "agent_cron_1", "cron": "0 9 * * *",
                                        "next_run": "2026-09-17 09:00"})

    result = cc_mcp_server._tool_schedule_cron(
        {"cron": "0 9 * * *", "prompt": "体检", "workspace": "KYT"})

    assert result["isError"] is False
    assert captured["body"]["workspace"] == "kyt"
    assert "kyt" in result["content"][0]["text"]


# ── HTTP 层：权威解析 + 群成员校验 ────────────────────────────

class _Bot:
    def __init__(self, name="spx", runner="claude", groups=(CALLER_CHAT, KYT_CHAT)):
        self.profile = NS(name=name, runner=runner, app_id=f"app_{name}",
                          dispatch_model="", default_model="",
                          allowed_group_chat_ids=set(groups))
        self.store = NS(find_primary_user=lambda: f"ou_{name}")
        self.feishu = NS(
            reply_text=AsyncMock(return_value="om_note"),
            send_post_to_chat=AsyncMock(return_value="om_root"),
            get_message_thread_id=AsyncMock(return_value=NEW_THREAD),
        )


def _drive_resolve(payload, monkeypatch, bots, endpoint="dispatch"):
    """不起服务器，直接驱动 /dispatch 的入口校验，拿回它解析出的目标。"""
    monkeypatch.setattr(http_server, "_bots", bots)
    responses = []
    fake_self = NS(
        client_address=("127.0.0.1", 12345),
        _respond=lambda code, data: responses.append((code, data)),
        _mcp_respond=lambda ep, prof, result: responses.append((400, result)),
    )
    fake_self._resolve_bot_by_profile = (
        lambda name: http_server._CardCallbackHandler._resolve_bot_by_profile(fake_self, name)
    )
    p, bot, target = http_server._CardCallbackHandler._resolve_dispatch_family(
        fake_self, endpoint, json.dumps(payload).encode(),
    )
    return p, bot, target, responses


def test_http_resolves_workspace_to_its_chat_and_cwd(monkeypatch, ws_file):
    p, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "链上", "prompt": "x"},
        monkeypatch, {"spx": _Bot()},
    )

    assert responses == []
    assert p["chat_id"] == KYT_CHAT             # 目标群被改写成工作域的群
    assert p["cwd"] == ws_file.kyt_dir          # 工作目录由路由表钉死
    assert p["workspace"] == "kyt"
    assert p["_caller_chat_id"] == CALLER_CHAT  # 父上下文仍指向调用方自己的群


def test_http_ignores_a_client_supplied_cwd(monkeypatch, ws_file):
    """cwd 以路由表为准：客户端传什么都不作数（权威解析只有一处）。"""
    p, *_ = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt",
         "cwd": "/etc", "prompt": "x"},
        monkeypatch, {"spx": _Bot()},
    )
    assert p["cwd"] == ws_file.kyt_dir


def test_http_without_workspace_changes_nothing(monkeypatch, ws_file):
    p, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "prompt": "x"},
        monkeypatch, {"spx": _Bot()},
    )
    assert responses == []
    assert p["chat_id"] == CALLER_CHAT and p["cwd"] == "" and p["workspace"] == ""


def test_http_rejects_unknown_workspace(monkeypatch, ws_file):
    p, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "nope", "prompt": "x"},
        monkeypatch, {"spx": _Bot()},
    )
    assert p is None
    assert responses and "未知工作域" in responses[0][1]["error"]


def test_http_rejects_when_the_bot_is_not_in_the_target_group(monkeypatch, ws_file):
    """bot 不在目标群就当场拒绝，别等 Lark 回 230002、更别静默派到别处。"""
    p, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt", "prompt": "x"},
        monkeypatch, {"spx": _Bot(groups=(CALLER_CHAT,))},
    )
    assert p is None
    err = responses[0][1]["error"]
    assert "不在" in err and KYT_CHAT in err and "SPX_ALLOWED_GROUP_CHAT_IDS" in err


def test_http_rejects_when_the_cross_agent_bot_is_not_in_the_target_group(monkeypatch, ws_file):
    """跨 agent + 跨群：校验的是**真正去建话题的那个 bot**，不是调用方。"""
    bots = {"spx": _Bot(), "gpt": _Bot("gpt", "codex", groups=(CALLER_CHAT,))}
    p, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt",
         "agent": "gpt", "prompt": "x"},
        monkeypatch, bots,
    )
    assert p is None
    assert "gpt" in responses[0][1]["error"]


def test_http_rejects_workspace_whose_directory_is_gone(monkeypatch, tmp_path, ws_file):
    p = tmp_path / "w2.json"
    p.write_text(json.dumps({"workspaces": [
        {"name": "kyt", "chat_id": KYT_CHAT, "cwd": str(tmp_path / "deleted")},
    ]}), encoding="utf-8")
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(p))
    workspaces._CACHE.clear()

    payload, bot, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt", "prompt": "x"},
        monkeypatch, {"spx": _Bot()},
    )
    assert payload is None
    assert "目录不存在" in responses[0][1]["error"]


def test_workspace_default_agent_applies_only_when_caller_is_silent(monkeypatch, tmp_path):
    p = tmp_path / "w3.json"
    (tmp_path / "kyt").mkdir()
    p.write_text(json.dumps({"workspaces": [
        {"name": "kyt", "chat_id": KYT_CHAT, "cwd": str(tmp_path / "kyt"), "agent": "gpt"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("CC_LARK_WORKSPACES_FILE", str(p))
    workspaces._CACHE.clear()
    bots = {"spx": _Bot(), "gpt": _Bot("gpt", "codex")}

    _, _, target, responses = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt", "prompt": "x"},
        monkeypatch, bots)
    assert responses == [] and target.profile.name == "gpt"

    # 调用方点名了 agent → 以调用方为准，工作域默认值不许覆盖
    _, _, target2, responses2 = _drive_resolve(
        {"profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt",
         "agent": "claude", "prompt": "x"},
        monkeypatch, bots)
    assert responses2 == [] and target2.profile.runner == "claude"


def test_handover_refuses_cross_workspace(monkeypatch, ws_file):
    """移交要收尾原话题，跨群那套收尾没实现 —— 宁可明说也不半吊子地交出去。"""
    monkeypatch.setattr(http_server, "_bots", {"spx": _Bot()})
    monkeypatch.setattr(http_server, "_handlers", NS(handover_task=AsyncMock()))
    responses = []
    fake_self = NS(
        client_address=("127.0.0.1", 12345),
        _respond=lambda code, data: responses.append((code, data)),
        _mcp_respond=lambda ep, prof, result: responses.append((400, result)),
    )
    for attr in ("_resolve_bot_by_profile", "_resolve_dispatch_family", "_handle_handover_task"):
        setattr(fake_self, attr, getattr(http_server._CardCallbackHandler, attr).__get__(fake_self))

    fake_self._handle_handover_task(json.dumps({
        "profile": "spx", "chat_id": CALLER_CHAT, "workspace": "kyt",
        "brief": {"goal": "g", "completed": "c", "remaining": "r"},
    }).encode())

    assert responses and "不支持跨工作域" in responses[0][1]["error"]


# ── 整链路：MCP 工具 → HTTP control → dispatcher ──────────────

@pytest.fixture
def fake_dispatch_world(monkeypatch):
    """真跑 dispatcher.dispatch_task，只把子会话与"批次收口唤醒父 agent"换成假的。"""
    spawns = []

    async def _fake_spawn(_bot, **kwargs):
        spawns.append({"bot": _bot, **kwargs})
        return (True, "done")

    monkeypatch.setattr(dispatcher, "handle_spawn", _fake_spawn)
    # 批次 debounce 归零 + 收口唤醒打桩，否则每个用例都要白等 6 秒
    monkeypatch.setattr(dispatcher, "WAVE_DEBOUNCE_SEC", 0)
    monkeypatch.setattr(dispatcher, "_dispatch_wake_parent", AsyncMock())
    monkeypatch.setattr(dispatcher, "_DISPATCH_TASKS", set())
    monkeypatch.setattr(dispatcher, "_DISPATCH_CHILDREN", {})
    monkeypatch.setattr(dispatcher, "_DISPATCH_PARENTS", {})
    yield NS(spawns=spawns)
    dispatcher._DISPATCH_CHILDREN.clear()
    dispatcher._DISPATCH_PARENTS.clear()


async def _drain():
    for _ in range(6):
        await asyncio.sleep(0)
    pending = [t for t in dispatcher._DISPATCH_TASKS if not t.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 2)


async def test_end_to_end_dispatch_lands_in_the_other_groups_workspace(
        monkeypatch, mcp_env, ws_file, fake_dispatch_world):
    bot = _Bot()
    monkeypatch.setattr(http_server, "_bots", {"spx": bot})
    monkeypatch.setattr(http_server, "_bot_loop", asyncio.get_running_loop())
    monkeypatch.setattr(http_server, "_handlers", NS(dispatch_task=dispatcher.dispatch_task))
    monkeypatch.setattr(http_server, "_control_token", "test-ws-token")
    server = http_server.start_control_server(0)
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", str(server.server_address[1]))
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "test-ws-token")
    try:
        result = await asyncio.to_thread(
            cc_mcp_server._tool_dispatch_task,
            {"prompt": "查一笔链上交易", "workspace": "链上", "title": "KYT 排查"},
        )
        await _drain()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()

    assert result["isError"] is False, result["content"][0]["text"]
    # 话题建在 KYT 群
    assert bot.feishu.send_post_to_chat.await_args.kwargs["chat_id"] == KYT_CHAT
    # 子会话跑在 KYT 群、钉在 KYT 目录
    spawn = fake_dispatch_world.spawns[0]
    assert spawn["chat_id_raw"] == KYT_CHAT
    assert spawn["cwd"] == ws_file.kyt_dir and spawn["workspace"] == "kyt"
    # 并发闸门按**目标群**计数（别把配额记到调用方那个群头上）
    assert set(dispatcher._DISPATCH_CHILDREN) <= {KYT_CHAT}


async def test_parent_batch_is_tracked_in_the_callers_group_not_the_targets(
        monkeypatch, ws_file, fake_dispatch_world):
    """跨群派发时父 thread 仍在调用方的群里：批次收口要 resume 的是父 session，
    chat 取成子会话那个群就会拼出一个不存在的 chat_key，父 agent 永远醒不过来。"""
    bot = _Bot()
    result = await dispatcher.dispatch_task(
        bot, user_id="ou_me", group_chat_id=KYT_CHAT, title="t", prompt="p",
        parent_thread=PARENT_THREAD, parent_anchor=PARENT_ANCHOR,
        parent_chat=CALLER_CHAT, cwd=os.getcwd(), workspace="kyt",
    )
    assert result["ok"] is True
    # 批次登记在返回时就已落下（子会话还没跑完，条目还在）
    grp = dispatcher._DISPATCH_PARENTS[PARENT_THREAD]
    assert grp["chat"] == CALLER_CHAT
    assert grp["thread"] == PARENT_THREAD
    await _drain()


async def test_parent_chat_defaults_to_the_target_group(monkeypatch, fake_dispatch_world):
    """不传 parent_chat（同群派发 / 老调用方）时行为与从前逐字一致。"""
    bot = _Bot()
    await dispatcher.dispatch_task(
        bot, user_id="ou_me", group_chat_id=CALLER_CHAT, title="t", prompt="p",
        parent_thread=PARENT_THREAD, parent_anchor=PARENT_ANCHOR,
    )
    assert dispatcher._DISPATCH_PARENTS[PARENT_THREAD]["chat"] == CALLER_CHAT
    await _drain()


def test_telegram_session_cannot_route_to_a_lark_workspace(monkeypatch, ws_file):
    """工作域是 Lark 群（oc_…）的概念：Telegram bot 往那儿建话题根本发不出去。"""
    tg = _Bot("tg", groups=("*",))
    tg.profile.is_telegram = True
    p, bot, target, responses = _drive_resolve(
        {"profile": "tg", "chat_id": "-100123", "workspace": "kyt", "prompt": "x"},
        monkeypatch, {"tg": tg},
    )
    assert p is None
    assert "Telegram" in responses[0][1]["error"]


def test_telegram_prompt_never_mentions_workspaces(monkeypatch):
    """system prompt 同理：别告诉 Telegram 侧一个它用不了的参数。"""
    import lark_prompts
    lark = NS(is_telegram=False)
    tg = NS(is_telegram=True)
    assert lark_prompts._build_workspace_routing(tg) == ""
    if workspaces.load():                       # 本机配了表才有东西可比
        assert "workspace" in lark_prompts._build_workspace_routing(lark)


def test_stale_bot_that_ignores_workspace_is_called_out(monkeypatch, mcp_env, ws_file):
    """老 bot（没 /restart）会把 workspace 整个吞掉 —— 那时活其实落在当前群。

    它不回显 workspace，据此识破并在回执里顶一行警告；但**不报 isError**：
    子会话真的已经派出去了，报错只会引诱模型再派一遍。
    """
    _fake_http(monkeypatch, _OK)                     # 老响应里没有 workspace 字段

    result = cc_mcp_server._tool_dispatch_task({"prompt": "查一下", "workspace": "kyt"})

    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert text.startswith("⚠️ 路由未生效") and "/restart" in text


def test_no_stale_warning_when_the_bot_echoes_the_workspace(monkeypatch, mcp_env, ws_file):
    _fake_http(monkeypatch, {**_OK, "workspace": "kyt", "cwd": ws_file.kyt_dir})
    result = cc_mcp_server._tool_dispatch_task({"prompt": "查一下", "workspace": "kyt"})
    assert "路由未生效" not in result["content"][0]["text"]
    # 没要求路由时当然也不该警告
    _fake_http(monkeypatch, _OK)
    assert "路由未生效" not in cc_mcp_server._tool_dispatch_task(
        {"prompt": "x"})["content"][0]["text"]
