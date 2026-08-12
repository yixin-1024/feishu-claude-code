"""外部事件触发 API 的鉴权 / 配置 / 提示词组装 / HTTP 暴露面测试。

重点覆盖三类回归风险：
  · fail-closed —— 没配 client、密钥太短、route 未授权时必须拒绝
  · 边界不可越 —— 请求体拿不到 cwd/profile；control 端点不因新增前缀而暴露
  · 幂等 —— 后端重试不该在群里刷出第二条话题
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import external_api
import http_server


SECRET = "0123456789abcdef0123456789abcdef"


class _Store:
    def find_primary_user(self):
        return "ou_primary"


class _Bot:
    def __init__(self, name="spx", groups=("oc_group",), chat_cwd=None):
        self.profile = type("Profile", (), {
            "name": name,
            "app_id": f"app_{name}",
            "app_secret": f"secret_{name}",
            "verification_token": "",
            "runner": "claude",
            "allowed_open_ids": {"ou_primary"},
            "allowed_group_chat_ids": set(groups),
            # 群自己的 workspace 映射（.env 里的 <PROFILE>_CHAT_CWD_<chat_id>）
            "chat_default_cwd": dict(chat_cwd or {}),
        })()
        self.store = _Store()


def _write_cfg(tmp_path, *, routes: str, clients: str) -> str:
    path = tmp_path / "external_triggers.yaml"
    path.write_text(f"clients:\n{clients}\nroutes:\n{routes}\n", encoding="utf-8")
    return str(path)


def _basic_cfg(tmp_path, workdir, **route_extra) -> str:
    extra = "".join(f"    {k}: {v}\n" for k, v in route_extra.items())
    return _write_cfg(
        tmp_path,
        routes=(
            "  doc-extract:\n"
            "    profile: spx\n"
            "    chat_id: oc_group\n"
            "    user_id: ou_primary\n"
            f"    cwd: {workdir}\n"
            "    workspace_label: spx-backend\n"
            "    model: opus\n"
            "    topic_title: 外部事件 · 抽取\n"
            "    instruction: 按 runbook 抽字段\n"
            + extra
        ),
        clients=(
            "  - id: backend\n"
            "    secret_env: TEST_API_SECRET\n"
            "    routes: [doc-extract]\n"
        ),
    )


class _Deps:
    """记录派发调用的假 dispatch/read_thread。"""

    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "thread_id": "omt_new", "anchor_message_id": "om_anchor",
                       "model": "opus", "effort": "", "agent": "spx"}
        self.transcript = {"ok": True, "count": 2, "transcript": "[1] bot: 干完了"}

    async def dispatch(self, bot, **kwargs):
        self.calls.append((bot, kwargs))
        return self.result

    async def read_thread(self, bot, **kwargs):
        return self.transcript

    def run_coro(self, coro, timeout):
        # 测试里没有 bot_loop：直接把 coroutine 跑完即可
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    deps_obj = _Deps()
    cfg_path = _basic_cfg(tmp_path, workdir)

    def _setup(path=None):
        cfg = external_api.load_config(path or cfg_path)
        external_api.configure(
            config=cfg,
            deps=external_api.ExternalApiDeps(
                bots={"spx": _Bot()},
                dispatch=deps_obj.dispatch,
                read_thread=deps_obj.read_thread,
                run_coro=deps_obj.run_coro,
            ),
            state_path=str(tmp_path / "state.json"),
            config_path=path or cfg_path,
        )
        return cfg

    _setup()
    external_api._rate_hits.clear()
    yield type("Api", (), {"deps": deps_obj, "cfg_path": cfg_path,
                           "workdir": str(workdir), "setup": staticmethod(_setup),
                           "tmp": tmp_path})
    external_api._config = external_api.ApiConfig()
    external_api._deps = None
    external_api._rate_hits.clear()


def _headers(extra=None, *, token=f"backend:{SECRET}"):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(extra or {})
    return h


def _post(payload, headers=None, *, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    return external_api.handle("POST", "/api/v1/agent-tasks",
                               _headers(headers), body, "127.0.0.1")


# ── 鉴权 ──────────────────────────────────────────────────────

def test_disabled_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_API_SECRET", raising=False)
    cfg = external_api.load_config(str(tmp_path / "nope.yaml"))
    assert not cfg.enabled
    external_api.configure(config=cfg, deps=None, state_path=str(tmp_path / "s.json"))
    code, payload = external_api.handle("POST", "/api/v1/agent-tasks", {}, b"{}", "1.2.3.4")
    assert code == 503 and payload["ok"] is False


def test_short_secret_disables_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", "tooshort")
    cfg = external_api.load_config(_basic_cfg(tmp_path, tmp_path))
    assert cfg.clients == {} and not cfg.enabled


def test_missing_and_wrong_credentials_rejected(api):
    code, _ = external_api.handle("POST", "/api/v1/agent-tasks", {}, b"{}", "1.2.3.4")
    assert code == 401
    code, _ = external_api.handle(
        "POST", "/api/v1/agent-tasks",
        {"Authorization": "Bearer backend:wrong-secret-aaaaaaaaaaaaaaaa"}, b"{}", "1.2.3.4")
    assert code == 401
    assert api.deps.calls == []


def test_client_header_form_is_accepted(api):
    code, payload = external_api.handle(
        "POST", "/api/v1/agent-tasks",
        {"Authorization": f"Bearer {SECRET}", "X-CC-Lark-Client": "backend"},
        json.dumps({"route": "doc-extract", "prompt": "hi"}).encode(), "127.0.0.1")
    assert code == 202, payload


def test_route_not_granted_is_403(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=(
            "  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {api.workdir}\n    instruction: x\n"
            "  other:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {api.workdir}\n    instruction: y\n"
        ),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: [doc-extract]\n",
    )
    api.setup(path)
    code, payload = _post({"route": "other", "prompt": "x"})
    assert code == 403 and "may not use" in payload["error"]
    assert api.deps.calls == []


def test_signature_required_mode(tmp_path, monkeypatch, api):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients=("  - id: backend\n    secret_env: TEST_API_SECRET\n"
                 "    routes: ['*']\n    require_signature: true\n"),
    )
    api.setup(path)
    body = json.dumps({"route": "doc-extract", "prompt": "hi"}).encode()

    code, payload = _post(None, raw=body)
    assert code == 401 and "Signature" in payload["error"] or "signature" in payload["error"]

    import hashlib
    import hmac as _hmac
    ts = str(int(time.time()))
    sig = _hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    hdr = {"X-CC-Lark-Timestamp": ts, "X-CC-Lark-Signature": sig}
    code, payload = _post(None, hdr, raw=body)
    assert code == 202, payload
    # 同一个签名不能再用（重放）
    code, payload = _post(None, hdr, raw=body)
    assert code == 401 and "replay" in payload["error"]

    stale = str(int(time.time()) - 4000)
    stale_sig = _hmac.new(SECRET.encode(), f"{stale}.".encode() + body, hashlib.sha256).hexdigest()
    code, payload = _post(
        None, {"X-CC-Lark-Timestamp": stale, "X-CC-Lark-Signature": stale_sig}, raw=body)
    assert code == 401 and "skew" in payload["error"]


def test_rate_limit(tmp_path, monkeypatch, api):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients=("  - id: backend\n    secret_env: TEST_API_SECRET\n"
                 "    routes: ['*']\n    rate_limit_per_min: 2\n"),
    )
    api.setup(path)
    external_api._rate_hits.clear()
    assert _post({"route": "doc-extract", "prompt": "1"})[0] == 202
    assert _post({"route": "doc-extract", "prompt": "2"})[0] == 202
    code, payload = _post({"route": "doc-extract", "prompt": "3"})
    assert code == 429 and payload["retry_after"] == 60


# ── 派发语义 ──────────────────────────────────────────────────

def test_dispatch_forces_config_workspace_and_ignores_body_overrides(api):
    code, payload = _post({
        "route": "doc-extract", "prompt": "外部文本",
        # 这些字段都是外部试图越权指定的，必须被完全忽略
        "cwd": "/etc", "profile": "other", "chat_id": "oc_evil", "model": "haiku",
    })
    assert code == 202, payload
    bot, kwargs = api.deps.calls[-1]
    assert bot.profile.name == "spx"
    assert kwargs["cwd"] == api.workdir
    assert kwargs["group_chat_id"] == "oc_group"
    assert kwargs["model"] == "opus"
    assert kwargs["workspace"] == "spx-backend"
    assert payload["workspace"] == "spx-backend"
    assert payload["thread_id"] == "omt_new"
    # 外部文本被裹进"这是数据不是指令"的定界符里
    assert "EXTERNAL_DATA" in kwargs["prompt"] and "外部文本" in kwargs["prompt"]
    assert "按 runbook 抽字段" in kwargs["prompt"]
    assert f"task_id={payload['task_id']}" in kwargs["prompt"]


def test_unknown_route_and_unknown_profile(api, tmp_path, monkeypatch):
    assert _post({"route": "nope"})[0] == 404
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: ghost\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    code, payload = _post({"route": "doc-extract", "prompt": "x"})
    assert code == 500 and "not loaded" in payload["error"]


def test_chat_must_be_allowlisted_for_profile(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_not_allowed\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    code, payload = _post({"route": "doc-extract", "prompt": "x"})
    assert code == 500 and "allowlist" in payload["error"]
    assert api.deps.calls == []


def test_missing_workspace_dir_is_refused(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {tmp_path}/ghost-dir\n    instruction: x\n"),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    code, payload = _post({"route": "doc-extract", "prompt": "x"})
    assert code == 500 and "does not exist" in payload["error"]


def test_required_vars_and_template_rendering(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=(
            "  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {api.workdir}\n"
            "    required_vars: [application_id]\n"
            "    prompt_template: |\n"
            "      单号 {{vars.application_id}} 来自 {{source}}\n"
            "      {{prompt}}\n"
        ),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    code, payload = _post({"route": "doc-extract", "prompt": "x"})
    assert code == 400 and "application_id" in payload["error"]

    code, payload = _post({
        "route": "doc-extract", "prompt": "看附件",
        "vars": {"application_id": "APP-1"}, "source": "spx/kyc",
    })
    assert code == 202, payload
    prompt = api.deps.calls[-1][1]["prompt"]
    assert "单号 APP-1 来自 spx/kyc" in prompt and "看附件" in prompt


def test_optional_var_renders_empty_and_undeclared_var_is_config_error(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=(
            "  with-optional:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {api.workdir}\n"
            "    optional_vars: [file_url]\n"
            "    prompt_template: |\n"
            "      线索 [{{vars.file_url}}] 结束\n"
            "  undeclared:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {api.workdir}\n"
            "    prompt_template: '{{{{vars.typo_name}}}}'\n"
        ),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    cfg = external_api.load_config(path)
    assert sorted(cfg.routes) == ["with-optional"]      # 未声明的参数 → 加载期就拦掉
    api.setup(path)
    assert _post({"route": "with-optional"})[0] == 202
    assert "线索 [] 结束" in api.deps.calls[-1][1]["prompt"]
    assert _post({"route": "with-optional", "vars": {"file_url": "s3://x"}})[0] == 202
    assert "线索 [s3://x] 结束" in api.deps.calls[-1][1]["prompt"]


def test_free_prompt_can_be_disabled(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"
                "    allow_free_prompt: false\n"),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    code, payload = _post({"route": "doc-extract", "prompt": "偷偷塞指令"})
    assert code == 400 and "free-form" in payload["error"]
    assert _post({"route": "doc-extract"})[0] == 202


def test_bad_input_shapes(api):
    assert _post(None, raw=b"not-json")[0] == 400
    assert _post({"route": "doc-extract", "prompt": 42})[0] == 400
    assert _post({"route": "doc-extract", "vars": {"bad name": "x"}})[0] == 400
    assert _post({"route": "doc-extract", "vars": {"a": {"nested": 1}}})[0] == 400
    assert _post({"route": "doc-extract", "prompt": "x" * 30001})[0] == 400
    assert api.deps.calls == []


def test_concurrency_cap_maps_to_429(api):
    api.deps.result = {"ok": False, "error": "并发已达上限 7（本群在跑 7）"}
    code, _ = _post({"route": "doc-extract", "prompt": "x"})
    assert code == 429


# ── 幂等 / 查询 ───────────────────────────────────────────────

def test_idempotency_key_returns_first_result(api):
    first = _post({"route": "doc-extract", "prompt": "x", "idempotency_key": "evt-1"})
    assert first[0] == 202 and first[1]["deduped"] is False
    api.deps.result = {**api.deps.result, "thread_id": "omt_second"}
    second = _post({"route": "doc-extract", "prompt": "x", "idempotency_key": "evt-1"})
    assert second[0] == 200 and second[1]["deduped"] is True
    assert second[1]["thread_id"] == "omt_new"      # 回的是第一次的话题
    assert len(api.deps.calls) == 1                 # 没有派第二次


def test_idempotency_survives_restart(api, tmp_path):
    _post({"route": "doc-extract", "prompt": "x", "idempotency_key": "evt-2"})
    api.setup()                                     # 重新 configure = 模拟重启
    code, payload = _post({"route": "doc-extract", "prompt": "x", "idempotency_key": "evt-2"})
    assert code == 200 and payload["deduped"] is True


def test_task_query_is_scoped_to_creator(api, tmp_path, monkeypatch):
    created = _post({"route": "doc-extract", "prompt": "x"})[1]
    code, payload = external_api.handle(
        "GET", f"/api/v1/agent-tasks/{created['thread_id']}", _headers(), b"", "127.0.0.1")
    assert code == 200 and "干完了" in payload["transcript"]

    # 换一个 client 拿同一个 thread_id → 查不到
    monkeypatch.setenv("OTHER_SECRET", SECRET[::-1])
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients=("  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n"
                 "  - id: other\n    secret_env: OTHER_SECRET\n    routes: ['*']\n"),
    )
    api.setup(path)
    code, _ = external_api.handle(
        "GET", f"/api/v1/agent-tasks/{created['thread_id']}",
        {"Authorization": f"Bearer other:{SECRET[::-1]}"}, b"", "127.0.0.1")
    assert code == 404


def test_routes_listing_only_shows_granted(api):
    code, payload = external_api.handle("GET", "/api/v1/routes", _headers(), b"", "127.0.0.1")
    assert code == 200
    assert [r["route"] for r in payload["routes"]] == ["doc-extract"]
    assert payload["routes"][0]["workspace"] == "spx-backend"


def test_unknown_api_path_is_404(api):
    assert external_api.handle("GET", "/api/v1/nope", _headers(), b"", "127.0.0.1")[0] == 404
    assert external_api.handle("POST", "/api/v1/", _headers(), b"{}", "127.0.0.1")[0] == 404


def test_ip_allowlist_trusts_xff_only_from_proxy(tmp_path, monkeypatch, api):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients=("  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n"
                 "    allow_ips: [203.0.113.9]\n"),
    )
    monkeypatch.setenv("CC_LARK_API_TRUSTED_PROXY_IPS", "10.0.0.1")
    api.setup(path)
    body = json.dumps({"route": "doc-extract", "prompt": "x"}).encode()

    # 直连且 IP 不在白名单 → 403
    assert external_api.handle("POST", "/api/v1/agent-tasks", _headers(), body, "8.8.8.8")[0] == 403
    # 伪造 XFF 但 peer 不是可信反代 → 仍然 403
    assert external_api.handle(
        "POST", "/api/v1/agent-tasks", _headers({"X-Forwarded-For": "203.0.113.9"}),
        body, "8.8.8.8")[0] == 403
    # 经可信反代带 XFF → 放行
    assert external_api.handle(
        "POST", "/api/v1/agent-tasks", _headers({"X-Forwarded-For": "203.0.113.9"}),
        body, "10.0.0.1")[0] == 202


def test_bad_route_config_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=(
            "  relative-cwd:\n    profile: spx\n    chat_id: oc_group\n"
            "    cwd: ./nope\n    instruction: x\n"
            "  no-instruction:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {tmp_path}\n"
            "  bad-placeholder:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {tmp_path}\n    prompt_template: '{{{{nope}}}}'\n"
            "  good:\n    profile: spx\n    chat_id: oc_group\n"
            f"    cwd: {tmp_path}\n    instruction: ok\n"
        ),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    cfg = external_api.load_config(path)
    assert sorted(cfg.routes) == ["good"]


def test_audit_flags_route_whose_group_workspace_disagrees(tmp_path, monkeypatch):
    """群 ↔ workspace 对不上（把 spx 的活派进 cc-lark 群）必须在加载期就喊出来。"""
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    cclark = tmp_path / "cc-lark"
    spx = tmp_path / "spx"
    for d in (cclark, spx):
        d.mkdir()
    path = _write_cfg(
        tmp_path,
        routes=(
            # 配对的：cc-lark 群 + cc-lark 目录
            "  ok-route:\n    profile: spx\n    chat_id: oc_cclark\n"
            f"    cwd: {cclark}\n    instruction: x\n"
            # 配错的：cc-lark 群却挂 spx 目录
            "  wrong-group:\n    profile: spx\n    chat_id: oc_cclark\n"
            f"    cwd: {spx}\n    instruction: x\n"
            # 群没有 .env 映射 → 无从核对，不该误报
            "  unmapped:\n    profile: spx\n    chat_id: oc_other\n"
            f"    cwd: {spx}\n    instruction: x\n"
        ),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    cfg = external_api.load_config(path)
    bot = _Bot(groups=("oc_cclark", "oc_other"), chat_cwd={"oc_cclark": str(cclark)})
    external_api.configure(
        config=cfg,
        deps=external_api.ExternalApiDeps(bots={"spx": bot}, dispatch=None,
                                          read_thread=None, run_coro=None),
        state_path=str(tmp_path / "s.json"), config_path=path)
    findings = external_api.audit_route_groups()
    assert len(findings) == 1, findings
    assert "wrong-group" in findings[0]
    assert str(spx) in findings[0] and str(cclark) in findings[0]
    # 只 warn 不拦：配错的 route 仍然可用（专用监控群承载多 workspace 是合法用法）
    assert "wrong-group" in cfg.routes
    assert external_api.reload()["warnings"] == findings


def test_reload_picks_up_new_route(api, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    path = _write_cfg(
        tmp_path,
        routes=("  doc-extract:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: x\n"),
        clients="  - id: backend\n    secret_env: TEST_API_SECRET\n    routes: ['*']\n",
    )
    api.setup(path)
    assert _post({"route": "extra", "prompt": "x"})[0] == 404

    with open(path, "a", encoding="utf-8") as f:
        f.write("  extra:\n    profile: spx\n    chat_id: oc_group\n"
                f"    cwd: {api.workdir}\n    instruction: 新加的\n")
    result = external_api.reload()
    assert result["ok"] and "extra" in result["routes"]
    assert _post({"route": "extra", "prompt": "x"})[0] == 202


# ── HTTP 暴露面 ───────────────────────────────────────────────

def _http(base, path, *, method="GET", payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def servers(api, monkeypatch):
    """真起 callback + control 两个 listener，验证前缀只落在公网侧。"""
    http_server.configure(bot_loop=None, bots={"spx": _Bot()},
                          handlers=http_server.HttpHandlers(**{
                              f: (lambda *a, **k: None) for f in
                              http_server.HttpHandlers.__dataclass_fields__
                          }),
                          control_token="tok-" + SECRET)
    cb = http_server.start_callback_server(0)
    ctrl = http_server.start_control_server(0)
    yield (f"http://127.0.0.1:{cb.server_address[1]}",
           f"http://127.0.0.1:{ctrl.server_address[1]}")
    for s in (cb, ctrl):
        s.shutdown()
        s.server_close()


def test_public_listener_serves_api_and_hides_control(servers, api):
    public, control = servers
    code, payload = _http(public, "/api/v1/agent-tasks", method="POST",
                          payload={"route": "doc-extract", "prompt": "从 HTTP 来的"},
                          headers={"Authorization": f"Bearer backend:{SECRET}",
                                   "Content-Type": "application/json"})
    assert code == 202, payload
    assert payload["thread_id"] == "omt_new"
    assert api.deps.calls[-1][1]["cwd"] == api.workdir

    # 公网侧没有 API key 也进不去
    assert _http(public, "/api/v1/agent-tasks", method="POST", payload={})[0] == 401
    # 公网侧仍然看不到 control 端点
    assert _http(public, "/spawn", method="POST", payload={})[0] == 404
    # control 侧不提供外部 API（它是给本机 MCP/脚本用的）
    assert _http(control, "/api/v1/routes",
                 headers={"Authorization": f"Bearer tok-{SECRET}"})[0] == 404


def test_public_callback_still_works_with_api_prefix_added(servers):
    public, _ = servers
    code, payload = _http(public, "/callback", method="POST",
                          payload={"type": "url_verification", "challenge": "abc"},
                          headers={"Content-Type": "application/json"})
    assert code == 200 and payload["challenge"] == "abc"
