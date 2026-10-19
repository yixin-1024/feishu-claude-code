"""外部触发 API 的动态群（dynamic_chat）/ 续同一话题（thread_id）/ 群登记（/chats）测试。

背景：合作方进件每个合作方一个 Lark 话题群，群由后端运行时建、配置里写不死；
补件要续在原话题里。这里守住几条边界：
  · 未登记的群派不进去（403），登记要 manage_chats + bot 真在群里
  · 登记 = 白名单 + 群 workspace 钉成 route 的 cwd，且不许借它改道已有的群
  · 只能续本 route + 本群派出过的话题（409）
  · 登记簿落盘、重启后仍在；老 route 行为一字不改
"""

import asyncio
import json
import os
import stat
import sys
import urllib.error
import urllib.request

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import external_api
import group_allowlist
import http_server


SECRET = "0123456789abcdef0123456789abcdef"
OTHER_SECRET = "fedcba9876543210fedcba9876543210"
PARTNER_CHAT = "oc_partner01"
PARTNER_CHAT_2 = "oc_partner02"


class _Store:
    def find_primary_user(self):
        return "ou_primary"


class _Bot:
    def __init__(self, name="main", groups=("oc_legacy",), chat_cwd=None):
        self.profile = type("Profile", (), {
            "name": name,
            "app_id": f"app_{name}",
            "app_secret": f"secret_{name}",
            "verification_token": "",
            "runner": "claude",
            "allowed_open_ids": {"ou_primary"},
            "allowed_group_chat_ids": set(groups),
            "chat_default_cwd": dict(chat_cwd or {}),
        })()
        self.store = _Store()


class _Deps:
    def __init__(self, env_path):
        self.env_path = env_path
        self.dispatch_calls = []
        self.continue_calls = []
        self.membership_calls = []
        self.add_calls = []
        self.in_chat = {}            # chat_id -> (bool, detail)；缺省 (True, "")
        self.membership_error = None
        self._n = 0

    async def dispatch(self, bot, **kwargs):
        self._n += 1
        self.dispatch_calls.append((bot, kwargs))
        return {"ok": True, "thread_id": f"omt_t{self._n}", "anchor_message_id": f"om_a{self._n}",
                "model": "", "effort": "", "agent": bot.profile.name}

    async def continue_thread(self, bot, **kwargs):
        self.continue_calls.append((bot, kwargs))
        return {"ok": True, "thread_id": kwargs["thread_id"], "anchor_message_id": "om_reply",
                "model": "", "effort": "", "agent": bot.profile.name, "queued": False}

    async def read_thread(self, bot, **kwargs):
        return {"ok": True, "count": 0, "transcript": ""}

    async def check_membership(self, bot, chat_id):
        self.membership_calls.append(chat_id)
        if self.membership_error is not None:
            raise self.membership_error
        return self.in_chat.get(chat_id, (True, ""))

    async def add_group(self, bot, chat_id, cwd):
        self.add_calls.append((chat_id, cwd))
        # 真走共享实现（内存 + .env），只把 .env 换成临时文件
        return group_allowlist.add_chat_to_profile(bot.profile, chat_id, cwd, self.env_path)

    def run_coro(self, coro, timeout):
        return asyncio.new_event_loop().run_until_complete(coro)


def _write_cfg(tmp_path, workdir, legacy_dir, *, manage=True, extra_route="") -> str:
    path = tmp_path / "external_triggers.yaml"
    path.write_text(
        "clients:\n"
        "  - id: backend\n"
        "    secret_env: TEST_API_SECRET\n"
        "    routes: [partner-intake, doc-extract]\n"
        f"    manage_chats: {'true' if manage else 'false'}\n"
        "  - id: other\n"
        "    secret_env: TEST_OTHER_SECRET\n"
        "    routes: [doc-extract]\n"
        "  - id: ops\n"
        "    secret_env: TEST_OTHER_SECRET\n"
        "    routes: [doc-extract]\n"
        "    manage_chats: true\n"
        "routes:\n"
        "  partner-intake:\n"
        "    profile: main\n"
        "    dynamic_chat: true\n"
        f"    cwd: {workdir}\n"
        "    workspace_label: partner-onboarding\n"
        "    topic_title: 合作方进件\n"
        "    required_vars: [case_id, end_user_id, round, kind, env]\n"
        "    optional_vars: [partner_name, currency, request_id]\n"
        "    allow_free_prompt: false\n"
        "    prompt_template: |\n"
        "      处理进件 case={{vars.case_id}} round={{vars.round}} kind={{vars.kind}}\n"
        "  doc-extract:\n"
        "    profile: main\n"
        "    chat_id: oc_legacy\n"
        f"    cwd: {legacy_dir}\n"
        "    instruction: 按 runbook 抽字段\n"
        + extra_route,
        encoding="utf-8",
    )
    return str(path)


VARS = {"case_id": "c1", "end_user_id": "u1", "round": "1", "kind": "PERSONAL", "env": "staging"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    monkeypatch.setenv("TEST_OTHER_SECRET", OTHER_SECRET)
    workdir = tmp_path / "partner-onboarding"
    workdir.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text("MAIN_APP_ID=x\nMAIN_ALLOWED_GROUP_CHAT_IDS=oc_legacy\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    deps = _Deps(str(env_path))
    bot = _Bot()
    holder = {"bot": bot}

    def _setup(path=None, *, bot=None):
        if bot is not None:
            holder["bot"] = bot
        cfg_path = path or _write_cfg(tmp_path, workdir, legacy)
        cfg = external_api.load_config(cfg_path)
        external_api.configure(
            config=cfg,
            deps=external_api.ExternalApiDeps(
                bots={"main": holder["bot"]},
                dispatch=deps.dispatch,
                read_thread=deps.read_thread,
                run_coro=deps.run_coro,
                continue_thread=deps.continue_thread,
                check_membership=deps.check_membership,
                add_group=deps.add_group,
            ),
            state_path=str(tmp_path / "data" / "state.json"),
            config_path=cfg_path,
        )
        return cfg

    _setup()
    external_api._rate_hits.clear()
    yield type("Api", (), {"deps": deps, "workdir": str(workdir), "legacy": str(legacy),
                           "env_path": str(env_path), "setup": staticmethod(_setup),
                           "tmp": tmp_path, "holder": holder})
    external_api._config = external_api.ApiConfig()
    external_api._deps = None
    external_api._chats = {"routes": {}, "threads": {}}
    external_api._rate_hits.clear()


def _hdr(token=f"backend:{SECRET}", extra=None):
    h = {"Authorization": f"Bearer {token}"}
    h.update(extra or {})
    return h


def _task(payload, *, token=f"backend:{SECRET}", extra=None):
    return external_api.handle("POST", "/api/v1/agent-tasks", _hdr(token, extra),
                               json.dumps(payload).encode(), "127.0.0.1")


def _register(chat_id=PARTNER_CHAT, route="partner-intake", *, token=f"backend:{SECRET}", **extra):
    body = {"route": route, "chat_id": chat_id, **extra}
    return external_api.handle("POST", "/api/v1/chats", _hdr(token), json.dumps(body).encode(),
                               "127.0.0.1")


def _list(query="", token=f"backend:{SECRET}"):
    return external_api.handle("GET", "/api/v1/chats" + query, _hdr(token), b"", "127.0.0.1")


# ── 配置 ──────────────────────────────────────────────────────

def test_dynamic_route_loads_without_chat_id_and_client_flag(api):
    route = external_api._config.routes["partner-intake"]
    assert route.dynamic_chat and route.continue_threads and route.chat_id == ""
    legacy = external_api._config.routes["doc-extract"]
    assert not legacy.dynamic_chat and not legacy.continue_threads
    assert external_api._config.clients["backend"].manage_chats is True
    assert external_api._config.clients["other"].manage_chats is False


def test_non_dynamic_route_still_requires_chat_id(api, tmp_path):
    extra = ("  broken:\n    profile: main\n"
             f"    cwd: {api.legacy}\n    instruction: x\n")
    cfg = external_api.load_config(_write_cfg(tmp_path, api.workdir, api.legacy, extra_route=extra))
    assert "broken" not in cfg.routes


# ── 群登记 /chats ─────────────────────────────────────────────

def test_register_adds_allowlist_cwd_env_and_registry(api):
    code, payload = _register(name="SPX × Acme")
    assert code == 201, payload
    assert payload["created"] is True and payload["chat_id"] == PARTNER_CHAT
    profile = api.holder["bot"].profile
    # 实时生效：内存白名单 + 群 workspace
    assert PARTNER_CHAT in profile.allowed_group_chat_ids
    assert profile.chat_default_cwd[PARTNER_CHAT] == api.workdir
    # 和 /group add 同一份 .env 落盘，权限保持 0600
    env = open(api.env_path, encoding="utf-8").read()
    assert f"MAIN_ALLOWED_GROUP_CHAT_IDS=oc_legacy,{PARTNER_CHAT}" in env
    assert f"MAIN_CHAT_CWD_{PARTNER_CHAT}={api.workdir}" in env
    assert stat.S_IMODE(os.stat(api.env_path).st_mode) == 0o600
    assert api.deps.membership_calls == [PARTNER_CHAT]
    code, listed = _list("?route=partner-intake")
    assert code == 200
    assert [(c["chat_id"], c["name"]) for c in listed["chats"]] == [(PARTNER_CHAT, "SPX × Acme")]


def test_register_is_idempotent(api):
    assert _register()[0] == 201
    code, payload = _register()
    assert code == 200 and payload["created"] is False
    env = open(api.env_path, encoding="utf-8").read()
    assert env.count(PARTNER_CHAT) == 2          # 白名单一次 + cwd 一次，不重复追加
    assert len(_list()[1]["chats"]) == 1


def test_register_requires_manage_chats(api, tmp_path):
    api.setup(_write_cfg(tmp_path, api.workdir, api.legacy, manage=False))
    code, payload = _register()
    assert code == 403 and "manage chats" in payload["error"]
    assert api.deps.membership_calls == [] and api.deps.add_calls == []
    assert PARTNER_CHAT not in api.holder["bot"].profile.allowed_group_chat_ids


def test_register_rejects_ungranted_route_non_dynamic_route_and_bad_ids(api):
    # other 没有 manage_chats
    assert _register(token=f"other:{OTHER_SECRET}")[0] == 403
    # ops 有 manage_chats，但没被授权用 partner-intake
    code, payload = _register(token=f"ops:{OTHER_SECRET}")
    assert code == 403 and "may not use route" in payload["error"]
    code, payload = _register(route="doc-extract")
    assert code == 400 and "not dynamic_chat" in payload["error"]
    assert _register(route="nope")[0] == 404
    for bad in ("", "oc_", "chat123", "oc_abc\nMAIN_DEFAULT_CWD=/", "oc_a=b", "oc_*"):
        code, _ = _register(chat_id=bad)
        assert code == 400, bad
    assert api.deps.add_calls == []


def test_register_membership_failure_is_refused(api):
    api.deps.in_chat[PARTNER_CHAT] = (False, "bot is not a member of this chat")
    code, payload = _register()
    assert code == 409 and "not a member" in payload["error"]
    assert api.deps.add_calls == []
    assert PARTNER_CHAT not in api.holder["bot"].profile.allowed_group_chat_ids
    assert _list()[1]["chats"] == []

    api.deps.membership_error = RuntimeError("network down")
    code, payload = _register()
    assert code == 502 and "membership check failed" in payload["error"]
    assert api.deps.add_calls == []


def test_register_fails_when_env_cannot_be_persisted(api):
    os.unlink(api.env_path)
    code, payload = _register()
    assert code == 500 and "not persisted" in payload["error"]
    # 没落盘就不进登记簿：后端会重试，不能留下"登记了但重启就丢"的半成品
    assert _list()[1]["chats"] == []
    code, _ = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS})
    assert code == 403


def test_register_refuses_to_repoint_existing_groups(api):
    # 已映射到别的 workspace 的群（比如开发群）不能被改道成合作方目录
    bot = _Bot(groups=("oc_legacy", "oc_devgroup", "oc_manual"),
               chat_cwd={"oc_devgroup": "/home/yixin/workspace/spxpay"})
    api.setup(bot=bot)
    code, payload = _register(chat_id="oc_devgroup")
    assert code == 409 and "another workspace" in payload["error"]
    # 手工加白、没有 workspace 映射的群同样不碰
    code, payload = _register(chat_id="oc_manual")
    assert code == 409 and "outside this API" in payload["error"]
    assert bot.profile.chat_default_cwd == {"oc_devgroup": "/home/yixin/workspace/spxpay"}
    assert api.deps.add_calls == [] and api.deps.membership_calls == []
    # 已经手工 /group add 到同一个 cwd 的群可以被收编
    bot.profile.allowed_group_chat_ids.add("oc_adopt")
    bot.profile.chat_default_cwd["oc_adopt"] = api.workdir
    assert _register(chat_id="oc_adopt")[0] == 201


def test_register_refuses_chat_owned_by_another_dynamic_route(api, tmp_path):
    other_dir = tmp_path / "other-ws"
    other_dir.mkdir()
    extra = ("  second-intake:\n    profile: main\n    dynamic_chat: true\n"
             f"    cwd: {other_dir}\n    instruction: x\n")
    path = _write_cfg(tmp_path, api.workdir, api.legacy, extra_route=extra)
    text = open(path, encoding="utf-8").read().replace(
        "routes: [partner-intake, doc-extract]", "routes: [partner-intake, doc-extract, second-intake]")
    open(path, "w", encoding="utf-8").write(text)
    api.setup(path)
    assert _register()[0] == 201
    code, payload = _register(route="second-intake")
    assert code == 409 and "already registered for route 'partner-intake'" in payload["error"]


def test_list_chats_scoping(api):
    assert _register()[0] == 201
    code, payload = _list("?route=doc-extract")
    assert code == 200 and payload["chats"] == []
    assert _list("?route=nope")[0] == 404
    # other 不能用 partner-intake
    assert _list("?route=partner-intake", token=f"other:{OTHER_SECRET}")[0] == 403
    assert _list(token=f"other:{OTHER_SECRET}")[1]["chats"] == []


def test_registry_persists_and_reloads_after_restart(api):
    assert _register(name="SPX × Acme")[0] == 201
    path = external_api._chats_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not [f for f in os.listdir(os.path.dirname(path)) if ".tmp." in f]
    # 模拟重启：内存登记簿清空、重新 configure，应从磁盘读回
    external_api._chats = {"routes": {}, "threads": {}}
    api.setup()
    assert external_api.registered_chats("partner-intake") == [PARTNER_CHAT]
    assert _list()[1]["chats"][0]["name"] == "SPX × Acme"
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS})
    assert code == 202, payload


def test_corrupt_registry_is_set_aside_not_silently_overwritten(api, tmp_path):
    path = external_api._chats_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("{not json")
    api.setup()
    assert external_api.registered_chats("partner-intake") == []
    assert [f for f in os.listdir(os.path.dirname(path)) if ".broken." in f]


# ── 动态群派发 ────────────────────────────────────────────────

def test_dispatch_into_registered_chat(api):
    assert _register()[0] == 201
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS})
    assert code == 202, payload
    _, kwargs = api.deps.dispatch_calls[-1]
    assert kwargs["group_chat_id"] == PARTNER_CHAT
    assert kwargs["cwd"] == api.workdir and kwargs["workspace"] == "partner-onboarding"
    assert payload["ok"] is True and payload["chat_id"] == PARTNER_CHAT
    assert payload["thread_id"] == "omt_t1" and payload["task_id"]
    assert payload["continued"] is False


def test_dispatch_into_unregistered_chat_is_403(api):
    code, payload = _task({"route": "partner-intake", "chat_id": "oc_stranger", "vars": VARS})
    assert code == 403 and "not registered" in payload["error"]
    # 已在 profile 白名单里但没登记到本 route 的群也不行
    code, payload = _task({"route": "partner-intake", "chat_id": "oc_legacy", "vars": VARS})
    assert code == 403
    assert api.deps.dispatch_calls == []


def test_dynamic_route_without_any_chat_is_400(api):
    code, payload = _task({"route": "partner-intake", "vars": VARS})
    assert code == 400 and "requires chat_id" in payload["error"]


def test_chat_must_still_be_allowlisted_when_registry_says_yes(api):
    assert _register()[0] == 201
    api.holder["bot"].profile.allowed_group_chat_ids.discard(PARTNER_CHAT)
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS})
    assert code == 500 and "not allowlisted" in payload["error"]


# ── 续同一话题 ────────────────────────────────────────────────

def _first_round(api):
    assert _register()[0] == 201
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS})
    assert code == 202, payload
    return payload


def test_continue_thread_created_by_same_route_and_chat(api):
    first = _first_round(api)
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": first["thread_id"], "vars": {**VARS, "round": "2"}})
    assert code == 202, payload
    assert len(api.deps.dispatch_calls) == 1          # 没有新开话题
    bot, kwargs = api.deps.continue_calls[-1]
    assert kwargs["thread_id"] == first["thread_id"]
    assert kwargs["group_chat_id"] == PARTNER_CHAT
    assert kwargs["cwd"] == api.workdir
    assert "round=2" in kwargs["prompt"]
    assert {k: payload[k] for k in ("ok", "thread_id", "chat_id")} == {
        "ok": True, "thread_id": first["thread_id"], "chat_id": PARTNER_CHAT}
    assert payload["task_id"] and payload["task_id"] != first["task_id"]
    assert payload["continued"] is True
    rec = external_api._thread_record(first["thread_id"])
    assert rec["rounds"] == 2 and rec["first_task_id"] == first["task_id"]
    assert rec["last_task_id"] == payload["task_id"]
    # 话题根消息 id（om_）也能当 thread_id 用
    code, _ = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                     "thread_id": first["anchor_message_id"], "vars": VARS})
    assert code == 202
    assert api.deps.continue_calls[-1][1]["thread_id"] == first["thread_id"]


def test_continue_unknown_or_foreign_thread_is_409(api, tmp_path):
    first = _first_round(api)
    # 不存在的话题
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": "omt_unknown", "vars": VARS})
    assert code == 409
    # 别的群（同 route）派出的话题不能在这个群续
    assert _register(chat_id=PARTNER_CHAT_2)[0] == 201
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT_2,
                           "thread_id": first["thread_id"], "vars": VARS})
    assert code == 409 and "was not created by" in payload["error"]
    # 老 route 派出的话题（没进登记簿）也不能被动态 route 续
    code, legacy = _task({"route": "doc-extract", "prompt": "x"})
    assert code == 202
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": legacy["thread_id"], "vars": VARS})
    assert code == 409
    assert api.deps.continue_calls == []


def test_continue_thread_from_another_route_is_409(api, tmp_path):
    """同一个群里，别的 route 派出的话题也不能被本 route 续。"""
    extra = ("  second-intake:\n    profile: main\n    dynamic_chat: true\n"
             f"    chat_id: {PARTNER_CHAT}\n"
             f"    cwd: {api.workdir}\n    instruction: x\n")
    path = _write_cfg(tmp_path, api.workdir, api.legacy, extra_route=extra)
    text = open(path, encoding="utf-8").read().replace(
        "routes: [partner-intake, doc-extract]", "routes: [partner-intake, doc-extract, second-intake]")
    open(path, "w", encoding="utf-8").write(text)
    # 该群已手工 /group add 到同一个 workspace：partner-intake 可以收编登记
    bot = _Bot(groups=("oc_legacy", PARTNER_CHAT), chat_cwd={PARTNER_CHAT: api.workdir})
    api.setup(path, bot=bot)
    code, other = _task({"route": "second-intake", "prompt": "x"})
    assert code == 202, other
    assert other["chat_id"] == PARTNER_CHAT
    assert _register()[0] == 201
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": other["thread_id"], "vars": VARS})
    assert code == 409 and "was not created by route 'partner-intake'" in payload["error"]
    assert api.deps.continue_calls == []
    # 原 route 自己续是可以的
    code, payload = _task({"route": "second-intake", "prompt": "y", "thread_id": other["thread_id"]})
    assert code == 202 and payload["continued"] is True


def test_thread_registry_survives_restart(api):
    first = _first_round(api)
    external_api._chats = {"routes": {}, "threads": {}}
    external_api._state = {"idempotency": {}, "tasks": {}}
    api.setup()
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": first["thread_id"], "vars": VARS})
    assert code == 202, payload
    assert payload["continued"] is True


def test_idempotent_replays_for_create_and_continue(api):
    assert _register()[0] == 201
    hdr = {"X-Idempotency-Key": "intake-c1-1"}
    code, first = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS}, extra=hdr)
    assert code == 202
    code, again = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT, "vars": VARS}, extra=hdr)
    assert code == 200 and again["deduped"] is True
    assert again["thread_id"] == first["thread_id"] and again["task_id"] == first["task_id"]
    assert len(api.deps.dispatch_calls) == 1

    hdr2 = {"X-Idempotency-Key": "intake-c1-2"}
    body = {"route": "partner-intake", "chat_id": PARTNER_CHAT,
            "thread_id": first["thread_id"], "vars": {**VARS, "round": "2"}}
    code, cont = _task(body, extra=hdr2)
    assert code == 202 and cont["continued"] is True
    code, cont_again = _task(body, extra=hdr2)
    assert code == 200 and cont_again["deduped"] is True
    assert cont_again["task_id"] == cont["task_id"]
    assert len(api.deps.continue_calls) == 1
    assert external_api._thread_record(first["thread_id"])["rounds"] == 2


def test_continue_failure_is_reported_and_not_recorded(api):
    first = _first_round(api)

    async def _fail(bot, **kwargs):
        return {"ok": False, "error": "该话题没有派单归属记录，无法续跑"}

    external_api._deps.continue_thread = _fail
    code, payload = _task({"route": "partner-intake", "chat_id": PARTNER_CHAT,
                           "thread_id": first["thread_id"], "vars": VARS})
    assert code == 502 and payload["ok"] is False
    assert external_api._thread_record(first["thread_id"])["rounds"] == 1


# ── 老 route 一字不改 ─────────────────────────────────────────

def test_legacy_route_ignores_chat_and_thread_and_keeps_response_shape(api):
    code, first = _task({"route": "doc-extract", "prompt": "x"})
    assert code == 202
    code, payload = _task({"route": "doc-extract", "prompt": "y",
                           "chat_id": "oc_evil", "thread_id": first["thread_id"]})
    assert code == 202, payload
    assert api.deps.continue_calls == []
    assert len(api.deps.dispatch_calls) == 2
    assert api.deps.dispatch_calls[-1][1]["group_chat_id"] == "oc_legacy"
    assert payload["chat_id"] == "oc_legacy" and payload["thread_id"] != first["thread_id"]
    assert "continued" not in payload
    assert set(payload) == {"ok", "task_id", "route", "profile", "agent", "chat_id", "thread_id",
                            "anchor_message_id", "workspace", "model", "effort", "deduped"}
    # 老 route 的话题不进动态群登记簿
    assert external_api._chats.get("threads") == {}
    # 老 route 连字段类型都不看（以前就是整个忽略的）
    code, _ = _task({"route": "doc-extract", "prompt": "z", "chat_id": 123, "thread_id": ["x"]})
    assert code == 202
    # 动态 route 上类型不对才 400
    code, payload = _task({"route": "partner-intake", "chat_id": 123, "vars": VARS})
    assert code == 400 and "must be strings" in payload["error"]


def test_legacy_route_works_without_new_deps(api, tmp_path):
    """main.py 之外的调用方（老测试 / 老部署）不注入新依赖也要照常派发。"""
    deps = api.deps
    external_api.configure(
        config=external_api.load_config(_write_cfg(tmp_path, api.workdir, api.legacy)),
        deps=external_api.ExternalApiDeps(bots={"main": api.holder["bot"]}, dispatch=deps.dispatch,
                                          read_thread=deps.read_thread, run_coro=deps.run_coro),
        state_path=str(tmp_path / "data" / "state.json"),
    )
    assert _task({"route": "doc-extract", "prompt": "x"})[0] == 202
    assert _register()[0] == 501


def test_routes_listing_shows_dynamic_flags(api):
    code, payload = external_api.handle("GET", "/api/v1/routes", _hdr(), b"", "127.0.0.1")
    by_name = {r["route"]: r for r in payload["routes"]}
    assert by_name["partner-intake"]["dynamic_chat"] is True
    assert by_name["doc-extract"]["dynamic_chat"] is False


def test_audit_flags_registered_chat_repointed_elsewhere(api):
    assert _register()[0] == 201
    assert external_api.audit_route_groups() == []
    api.holder["bot"].profile.chat_default_cwd[PARTNER_CHAT] = "/somewhere/else"
    assert any(PARTNER_CHAT in line for line in external_api.audit_route_groups())


# ── HTTP 层把 query 交给 handle ───────────────────────────────

def test_http_listener_passes_query_string(api):
    http_server.configure(bot_loop=None, bots={"main": api.holder["bot"]},
                          handlers=http_server.HttpHandlers(**{
                              f: (lambda *a, **k: None) for f in
                              http_server.HttpHandlers.__dataclass_fields__
                          }),
                          control_token="tok-" + SECRET)
    cb = http_server.start_callback_server(0)
    try:
        base = f"http://127.0.0.1:{cb.server_address[1]}"
        req = urllib.request.Request(base + "/api/v1/chats", method="POST",
                                     data=json.dumps({"route": "partner-intake",
                                                      "chat_id": PARTNER_CHAT}).encode(),
                                     headers={**_hdr(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 201
        req = urllib.request.Request(base + "/api/v1/chats?route=doc-extract", headers=_hdr())
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert json.loads(resp.read())["chats"] == []
        req = urllib.request.Request(base + "/api/v1/chats?route=partner-intake", headers=_hdr())
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert [c["chat_id"] for c in json.loads(resp.read())["chats"]] == [PARTNER_CHAT]
    finally:
        cb.shutdown()
        cb.server_close()
