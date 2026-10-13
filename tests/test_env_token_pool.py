"""env token 池：凭证被 CLAUDE_CODE_OAUTH_TOKEN 钉死时，在几个 setup-token 之间切换。

用户 2026-10-13 的要求：/usage 卡片点按钮切换不同账户、能看到其他账户的用量、
能登记多个 token。钉死模式下账户池不生效，所以切的是"钉的那份"本身：
池文件（ENV_TOKENS_FILE）选中谁，就把谁写进本进程 os.environ，之后 spawn 的
claude 子进程继承它。
"""

import os
import stat
import sys
import time
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_switcher as accs  # noqa: E402
import commands  # noqa: E402
import dispatcher  # noqa: E402

TOK_A = "sk-ant-oat01-aaaa"
TOK_B = "sk-ant-oat01-bbbb"
KEY = "CLAUDE_CODE_OAUTH_TOKEN"


@pytest.fixture(autouse=True)
def _restore_pinned_env(monkeypatch):
    """sync_env_token_from_pool 直接写 os.environ：先 setenv 一次让 monkeypatch 记下
    原值（没有也记），收尾时还原，别把假 token 漏给后面的用例。"""
    monkeypatch.setenv(KEY, "sk-ant-oat01-placeholder")
    monkeypatch.delenv(KEY)


def _pool_ab():
    assert accs.add_env_token("a", TOK_A)[0]
    assert accs.add_env_token("b", TOK_B)[0]


def _fake_probe(usage: dict):
    """按 token 填用量：usage = {token: (u5h, u7d)}；缺的 token 当 403。"""
    def probe(acc, is_current=False, *, refreshable=True):
        assert refreshable is False, "env token 没有 refresh_token，不许走刷新"
        now = int(time.time())
        if acc.access_token not in usage:
            acc.probe_error = "auth 403 — token 已失效或被吊销"
            return acc
        acc.u5h, acc.u7d = usage[acc.access_token]
        acc.r5h, acc.r7d = now + 3600, now + 86400
        acc.s5h = acc.s7d = "allowed"
        return acc
    return probe


# ── 存储 / 选中 / 同步 env ─────────────────────────────────────────

def test_empty_pool_keeps_env_token_untouched(monkeypatch):
    monkeypatch.setenv(KEY, "sk-ant-oat01-from-dotenv")
    assert accs.env_token_names() == []
    assert accs.pinned_env_credential() == (KEY, "sk-ant-oat01-from-dotenv")


def test_add_first_token_selects_it_and_file_is_private(monkeypatch):
    monkeypatch.setenv(KEY, "sk-ant-oat01-from-dotenv")
    ok, msg = accs.add_env_token("a", TOK_A + "\n")
    assert ok, msg
    assert accs.active_env_token_name() == "a"
    assert stat.S_IMODE(os.stat(accs.ENV_TOKENS_FILE).st_mode) == 0o600
    # 池里选中的那份压过 .env 那份
    assert accs.pinned_env_credential() == (KEY, TOK_A)
    assert os.environ[KEY] == TOK_A


def test_add_rejects_bad_input_and_duplicates():
    assert not accs.add_env_token("a b", TOK_A)[0], "名字有空格"
    assert not accs.add_env_token("a", "not-a-token")[0]
    assert accs.add_env_token("a", TOK_A)[0]
    ok, msg = accs.add_env_token("b", TOK_A)
    assert not ok and "`a`" in msg


def test_switch_updates_file_and_env(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    _pool_ab()
    assert accs.switch_env_token("b") == (True, "switched")
    assert os.environ[KEY] == TOK_B
    assert accs.active_env_token_name() == "b"
    assert accs.switch_env_token("b") == (True, "already")
    ok, msg = accs.switch_env_token("zzz")
    assert not ok and "`a`" in msg and "`b`" in msg


def test_spawn_hook_syncs_even_without_switcher(monkeypatch):
    """终端里 token-use 改了文件 → 下一次 spawn 前就跟上（自动切换没开也一样）。"""
    _pool_ab()
    monkeypatch.setattr(accs, "_DEFAULT_SWITCHER", None)
    pool = accs._load_env_tokens()
    pool["active"] = "b"
    accs._save_env_tokens(pool)          # 模拟另一个进程改了选择
    monkeypatch.setenv(KEY, TOK_A)
    accs.maybe_switch_before_spawn()
    assert os.environ[KEY] == TOK_B


def test_remove_refuses_active_when_others_exist():
    _pool_ab()
    ok, msg = accs.remove_env_token("a")
    assert not ok and "正在用" in msg
    assert accs.remove_env_token("b")[0]
    assert accs.env_token_names() == ["a"]


def test_probe_one_not_refreshable_skips_refresh(monkeypatch):
    monkeypatch.setattr(accs, "_refresh_account_inplace",
                        mock.Mock(side_effect=AssertionError("不该按同名 slot 刷新")))
    monkeypatch.setattr(accs, "urlopen_with_retry", mock.Mock(side_effect=_http_error(403)))
    acc = accs._probe_one(accs.Account(name="a", access_token=TOK_A), refreshable=False)
    assert "403" in acc.probe_error and "setup-token" in acc.probe_error


def _http_error(code):
    import urllib.error

    def raise_(*a, **k):
        raise urllib.error.HTTPError("u", code, "x", {}, None)
    return raise_


# ── 撞墙自动切 ────────────────────────────────────────────────────

def test_emergency_switch_moves_to_token_with_headroom(monkeypatch):
    _pool_ab()
    monkeypatch.setattr(accs, "_probe_one", _fake_probe({TOK_A: (1.0, 1.0), TOK_B: (0.1, 0.2)}))
    monkeypatch.setattr(accs, "use_account", mock.Mock(side_effect=AssertionError("不该动账户池")))

    out = accs.emergency_switch_on_limit(notify=False)

    assert out["from"] == "a" and out["switched"] == "b"
    assert "7d 已用 20%" in out["reason"]
    assert os.environ[KEY] == TOK_B and accs.active_env_token_name() == "b"


def test_emergency_switch_stays_when_other_token_also_exhausted(monkeypatch):
    _pool_ab()
    monkeypatch.setattr(accs, "_probe_one", _fake_probe({TOK_A: (1.0, 0.5), TOK_B: (0.2, 0.99)}))

    out = accs.emergency_switch_on_limit(notify=False)

    assert out["switched"] is None
    assert out["current_reset_epoch"], "没切成也要给出当前 token 的恢复时刻"
    assert "b:" in out["reason"]
    assert os.environ[KEY] == TOK_A


def test_single_token_pool_keeps_old_pinned_behavior(monkeypatch):
    assert accs.add_env_token("a", TOK_A)[0]
    monkeypatch.setattr(accs, "probe_env_tokens", mock.Mock(side_effect=AssertionError("只有一个，不用探池")))
    monkeypatch.setattr(accs, "probe_pinned_env_credential", lambda: None)
    out = accs.emergency_switch_on_limit(notify=False)
    assert out["switched"] is None and "钉死" in out["reason"]


def test_dispatcher_knows_pool_is_switchable():
    assert dispatcher._env_token_pool_switchable() is False
    _pool_ab()
    assert dispatcher._env_token_pool_switchable() is True


# ── /usage 卡片 + 按钮 + /switch ──────────────────────────────────

def test_usage_card_shows_every_token_and_switch_buttons(monkeypatch):
    _pool_ab()
    monkeypatch.setattr(accs, "_probe_one", _fake_probe({TOK_A: (0.03, 0.94), TOK_B: (0.1, 0.2)}))

    out = commands._get_usage("oc_chat")

    text = out["text"]
    assert "当前 `a`" in text and "94.0%" in text
    assert "**其他 token：**" in text and "`b` · 5h `10%`" in text and "7d `20%`" in text
    switch = [b for b in out["buttons"] if b["value"]["action"] == "switch_usage"]
    assert [b["text"] for b in switch] == ["● a", "b"]
    assert switch[1]["value"]["name"] == "b"
    assert out["buttons"][-1]["value"] == {"action": "run_cmd", "cmd": "/usage", "cid": "oc_chat"}


def test_usage_card_reports_dead_token(monkeypatch):
    _pool_ab()
    monkeypatch.setattr(accs, "_probe_one", _fake_probe({TOK_A: (0.03, 0.5)}))
    text = commands._get_usage("oc_chat")["text"]
    assert "`b` — ⚠️ auth 403" in text


def test_switch_command_and_picker_use_tokens(monkeypatch):
    _pool_ab()
    picker = commands._get_account_switch_picker("oc_chat")
    assert "切换 Claude token" in picker["text"] and "当前 token：`a`" in picker["text"]
    assert [b["value"]["cmd"] for b in picker["buttons"]] == ["/switch a", "/switch b"]

    msg = commands._switch_claude_account("b")
    assert msg.startswith("✅ 已切换到 token `b`")
    assert os.environ[KEY] == TOK_B
    assert commands._switch_claude_account("b").startswith("✅ 当前已经是 token `b`")
    assert commands._switch_claude_account("nope").startswith("❌")


def test_accounts_matrix_uses_tokens(monkeypatch):
    _pool_ab()
    monkeypatch.setattr(accs, "probe_all", mock.Mock(side_effect=AssertionError("不该探账户池")))
    monkeypatch.setattr(accs, "_probe_one", _fake_probe({TOK_A: (0.03, 0.94), TOK_B: (0.1, 0.2)}))
    text = commands._get_accounts()
    assert "● **a**" in text and "**b**" in text


# ── CLI ──────────────────────────────────────────────────────────

def test_cli_token_add_reads_stdin_and_lists_fingerprint(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOK_A + "\n"))
    assert accs._cli_main(["token-add", "a"]) == 0
    assert accs._cli_main(["token-list"]) == 0
    out = capsys.readouterr().out
    assert "* a" in out and "sha256:" in out and TOK_A not in out
