"""agy（Antigravity）账号切换：快照 save / use / list + `/switch` 在 agy runner 下的分发。

凭证真源是 macOS keychain 的 gemini/antigravity 条目（go-keyring base64 格式），
这里把 keychain 读写替换成内存态，其余逻辑（编解码、email 解析、防串号、
写完读回校验）全部走真代码。
"""

import base64
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agy_account_switcher as aas
import commands


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": "1" * 20}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.sig"


def _blob(email: str, refresh: str, access: str = "ya29.aaa") -> dict:
    return {
        "token": {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": refresh,
            "expiry": "2026-09-12T21:14:56.123456789+08:00",
        },
        "auth_method": "consumer",
        "id_token": _id_token(email),
    }


@pytest.fixture
def kc(tmp_path, monkeypatch):
    """内存 keychain + 临时快照目录。返回可读写的 state。"""
    monkeypatch.setattr(aas, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    state = {"raw": None}

    monkeypatch.setattr(aas, "_read_keychain_raw", lambda: state["raw"])

    def fake_write(raw):
        state["raw"] = raw
        return True, ""

    monkeypatch.setattr(aas, "_write_keychain_raw", fake_write)

    def fake_delete():
        state["raw"] = None
        return True, ""

    monkeypatch.setattr(aas, "_delete_keychain", fake_delete)
    monkeypatch.setattr(aas, "keychain_supported", lambda: True)
    return state


def _login(kc, email, refresh, access="ya29.aaa"):
    kc["raw"] = aas.encode_raw(_blob(email, refresh, access))


# ── 编解码 / 元信息 ──────────────────────────────────────────────


def test_encode_decode_roundtrip_matches_go_keyring_format():
    blob = _blob("a@gmail.com", "1//refresh-a")
    raw = aas.encode_raw(blob)
    assert raw.startswith("go-keyring-base64:")
    assert aas.decode_raw(raw) == blob


def test_decode_accepts_plain_json():
    blob = _blob("a@gmail.com", "1//refresh-a")
    assert aas.decode_raw(json.dumps(blob)) == blob


def test_email_comes_from_id_token():
    assert aas.blob_email(_blob("who@gmail.com", "1//r")) == "who@gmail.com"


def test_email_falls_back_to_meta_when_id_token_broken():
    blob = _blob("who@gmail.com", "1//r")
    blob["id_token"] = "not-a-jwt"
    blob["_meta"] = {"email": "meta@gmail.com"}
    assert aas.blob_email(blob) == "meta@gmail.com"


def test_expiry_parses_go_nanosecond_timestamp():
    ts = aas.blob_expiry_ts(_blob("a@gmail.com", "1//r"))
    assert ts and 1789000000 < ts < 1800000000


# ── save / list / use ───────────────────────────────────────────


def test_save_defaults_name_to_email_local_part(kc):
    _login(kc, "cactrinh383@gmail.com", "1//refresh-a")
    ok, msg = aas.save_current_account()
    assert ok and "cactrinh383" in msg
    assert aas.list_account_files() == ["cactrinh383"]
    assert aas.current_account_name() == "cactrinh383"


def test_save_rejects_when_no_credentials(kc):
    ok, msg = aas.save_current_account("x")
    assert not ok and "没有 agy 凭证" in msg


def test_save_guards_against_overwriting_another_email(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    assert aas.save_current_account("main")[0]
    _login(kc, "b@gmail.com", "1//refresh-b")
    ok, msg = aas.save_current_account("main")
    assert not ok and "拒绝覆盖" in msg
    # --force 等价：guard_email=False
    assert aas.save_current_account("main", guard_email=False)[0]
    assert aas.blob_email(aas.load_account("main")) == "b@gmail.com"


def test_snapshot_is_written_0600_without_meta_leaking_into_keychain(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    path = os.path.join(aas.ACCOUNTS_DIR, "a.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert aas.load_account("a")["_meta"]["email"] == "a@gmail.com"

    _login(kc, "b@gmail.com", "1//refresh-b")
    aas.save_current_account("b")
    assert aas.use_account("a")[0]
    assert "_meta" not in aas.decode_raw(kc["raw"])


def test_use_switches_keychain_and_resyncs_previous_account(kc):
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.old")
    aas.save_current_account("a")
    _login(kc, "b@gmail.com", "1//refresh-b")
    aas.save_current_account("b")

    # agy 自己刷新了 a 的 access_token（keychain 变了，快照还是旧的）
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.rotated")
    ok, msg = aas.use_account("b")

    assert ok, msg
    assert aas.blob_email(aas.decode_raw(kc["raw"])) == "b@gmail.com"
    assert aas.load_account("a")["token"]["access_token"] == "ya29.rotated"
    assert "回收" in msg


def test_use_same_account_is_noop_but_still_resyncs(kc):
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.old")
    aas.save_current_account("a")
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.rotated")

    ok, msg = aas.use_account("a")

    assert ok and msg.startswith("already using")
    assert aas.load_account("a")["token"]["access_token"] == "ya29.rotated"


def test_use_unknown_account_lists_available(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    ok, msg = aas.use_account("nope")
    assert not ok and "`a`" in msg


def test_use_detects_keychain_overwritten_by_running_agy(kc, monkeypatch):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    _login(kc, "b@gmail.com", "1//refresh-b")
    aas.save_current_account("b")
    _login(kc, "a@gmail.com", "1//refresh-a")

    # 写进去之后被别的 agy 进程覆盖回 a
    monkeypatch.setattr(aas, "_write_keychain_raw", lambda raw: (True, ""))
    ok, msg = aas.use_account("b")

    assert not ok and "读回不一致" in msg


def test_logout_refuses_when_current_account_not_saved(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    ok, msg = aas.logout()
    assert not ok and "还没保存过" in msg
    assert kc["raw"] is not None


def test_logout_resyncs_then_clears_keychain(kc):
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.old")
    aas.save_current_account("a")
    _login(kc, "a@gmail.com", "1//refresh-a", access="ya29.rotated")

    ok, msg = aas.logout()

    assert ok and kc["raw"] is None
    assert aas.load_account("a")["token"]["access_token"] == "ya29.rotated"
    assert "`a`" in msg
    # 登完新号还能切回来
    _login(kc, "b@gmail.com", "1//refresh-b")
    aas.save_current_account("b")
    assert aas.use_account("a")[0]
    assert aas.blob_email(aas.decode_raw(kc["raw"])) == "a@gmail.com"


def test_logout_force_drops_unsaved_account(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    ok, _ = aas.logout(require_saved=False)
    assert ok and kc["raw"] is None


def test_logout_on_empty_keychain_is_noop(kc):
    ok, msg = aas.logout()
    assert ok and "本来就没有" in msg


def test_remove_account(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    assert aas.remove_account("a")[0]
    assert aas.list_account_files() == []
    assert not aas.remove_account("a")[0]


def test_current_account_name_matches_by_email_when_token_rotated(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    # refresh_token 被换掉（极少见），靠 email 仍能认出来
    _login(kc, "a@gmail.com", "1//refresh-a-new")
    assert aas.current_account_name() == "a"


def test_render_accounts_text_marks_active(kc):
    _login(kc, "a@gmail.com", "1//refresh-a")
    aas.save_current_account("a")
    text = aas.render_accounts_text()
    assert "a@gmail.com" in text and "● `a`" in text


# ── /switch 分发（agy runner）────────────────────────────────────


class _Store:
    def __init__(self, runner="agy"):
        self.runner = runner

    async def get_current(self, _user_id, _chat_id):
        return SimpleNamespace(runner=self.runner)


@pytest.mark.asyncio
async def test_switch_with_name_switches_agy_account(monkeypatch):
    seen = []
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda name: seen.append(name) or "✅ ok")
    monkeypatch.setattr(commands, "_switch_claude_account",
                        lambda _n: pytest.fail("agy runner must not touch Claude creds"))

    reply = await commands.handle_command("switch", "alt", "ou", "oc", _Store())

    assert seen == ["alt"] and reply == "✅ ok"


@pytest.mark.asyncio
async def test_switch_save_subcommand_does_not_switch(monkeypatch):
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda _n: pytest.fail("`save` is a subcommand, not an account"))
    monkeypatch.setattr(commands, "_save_agy_account", lambda args: f"saved:{args}")

    reply = await commands.handle_command("switch", "save alt --force", "ou", "oc", _Store())

    assert reply == "saved:alt --force"


@pytest.mark.asyncio
async def test_switch_without_args_shows_agy_picker(monkeypatch):
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda _n: pytest.fail("no-arg /switch must not mutate credentials"))
    monkeypatch.setattr(commands, "_get_agy_switch_picker",
                        lambda chat_id: {"text": "agy-picker", "buttons": []})

    reply = await commands.handle_command("switch", "", "ou", "oc", _Store())

    assert reply["text"] == "agy-picker"


@pytest.mark.asyncio
async def test_accounts_routes_to_agy_under_agy_runner(monkeypatch):
    monkeypatch.setattr(commands, "_get_agy_accounts", lambda chat_id: f"agy-accounts:{chat_id}")
    monkeypatch.setattr(commands, "_get_accounts",
                        lambda: pytest.fail("agy runner must not probe Claude accounts"))

    reply = await commands.handle_command("accounts", "", "ou", "oc", _Store())

    assert reply == "agy-accounts:oc"


@pytest.mark.asyncio
async def test_switch_logout_subcommand_routes_to_logout(monkeypatch):
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda _n: pytest.fail("`logout` is a subcommand, not an account"))
    monkeypatch.setattr(commands, "_logout_agy_account", lambda args: f"logout:{args}")

    reply = await commands.handle_command("switch", "logout --force", "ou", "oc", _Store())

    assert reply == "logout:--force"


def test_remove_subcommand_requires_name(monkeypatch):
    reply = commands._handle_agy_switch("remove", "oc")
    assert "用法" in reply
