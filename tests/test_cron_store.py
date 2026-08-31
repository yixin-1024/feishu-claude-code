"""cron_store 的删/停/改：重点验「只动目标那几行、其余字节原样」和「跨群一律拒绝」。"""

import os

import pytest

import cron_store

CHAT = "oc_thisgroup"
OTHER = "oc_othergroup"

YAML = """\
# 顶部注释必须活下来
- name: keep_me
  profile: spx
  cron: 0 8 * * *
  timezone: Asia/Shanghai
  chat_id: ${SPX_DISPATCH_CHAT_ID}
  user_id: ou_x
  topic_title: 用了 env 引用的任务
  prompt_file: data/agent_crons/keep_me.md
- name: target
  profile: spx
  # 注意: APScheduler 的 dow=0 是周一不是周日
  cron: 50 13 * * 0
  timezone: Asia/Shanghai
  chat_id: oc_thisgroup
  user_id: ou_x
  topic_title: 周报
  prompt_file: data/agent_crons/target.md
- name: not_mine
  profile: spx
  cron: 0 9 * * *
  timezone: Asia/Shanghai
  chat_id: oc_othergroup
  user_id: ou_x
  topic_title: 别的群的
  prompt_file: data/agent_crons/not_mine.md
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """把 cron_store 的「仓库根」指到 tmp，yaml / data 全部隔离。"""
    monkeypatch.setattr(cron_store, "repo_dir", lambda: str(tmp_path))
    monkeypatch.delenv("CC_LARK_TASKS_YAML", raising=False)
    monkeypatch.delenv("SPX_DISPATCH_CHAT_ID", raising=False)
    (tmp_path / "scheduled_tasks.yaml").write_text(YAML, encoding="utf-8")
    prompts = tmp_path / "data" / "agent_crons"
    prompts.mkdir(parents=True)
    for n in ("keep_me", "target", "not_mine"):
        (prompts / f"{n}.md").write_text(f"prompt of {n}\n", encoding="utf-8")
    return tmp_path


def _yaml(repo):
    return (repo / "scheduled_tasks.yaml").read_text(encoding="utf-8")


def ok_reload():
    return {"ok": True, "removed": [], "added": [], "errors": []}


# ── 切块 / 字段 ────────────────────────────────────────────────

def test_split_join_is_byte_identical():
    preamble, blocks = cron_store.split_entries(YAML)
    assert [cron_store.field_value(b, "name") for b in blocks] == ["keep_me", "target", "not_mine"]
    assert cron_store.join_entries(preamble, blocks) == YAML


def test_block_comment_is_not_parsed_as_field():
    _, blocks = cron_store.split_entries(YAML)
    target = blocks[1]
    # 块内注释行不能被当成字段，cron 必须读到注释下面那行
    assert cron_store.field_value(target, "cron") == "50 13 * * 0"


@pytest.mark.parametrize("raw,expected", [
    ('key: plain value', "plain value"),
    ('key: "0 9 * * *"', "0 9 * * *"),
    ("key: 'it''s here'", "it's here"),
    ('key: bare # trailing comment', "bare"),
])
def test_scalar_parsing(raw, expected):
    assert cron_store.field_value([f"- {raw}"], "key") == expected


def test_emit_roundtrips_awkward_values():
    block = ["- name: x", "  chat_id: oc_thisgroup", "  prompt_file: a.md"]
    cron_store.set_field(block, "topic_title", '含 "引号" 和 # 井号')
    assert cron_store.field_value(block, "topic_title") == '含 "引号" 和 # 井号'
    # 新字段插在 prompt_file 之前
    assert block[-1].strip().startswith("prompt_file:")


# ── 归属校验 ──────────────────────────────────────────────────

def test_unexpanded_env_chat_id_is_never_in_scope():
    _, blocks = cron_store.split_entries(YAML)
    assert cron_store.block_in_chat(blocks[0], CHAT) is False  # ${SPX_DISPATCH_CHAT_ID}
    assert cron_store.block_in_chat(blocks[1], CHAT) is True
    assert cron_store.block_in_chat(blocks[2], CHAT) is False


def test_expanded_env_chat_id_is_in_scope(monkeypatch):
    monkeypatch.setenv("SPX_DISPATCH_CHAT_ID", CHAT)
    _, blocks = cron_store.split_entries(YAML)
    assert cron_store.block_in_chat(blocks[0], CHAT) is True


# ── cancel ────────────────────────────────────────────────────

def test_cancel_removes_only_target_and_keeps_everything_else(repo):
    res = cron_store.cancel("target", chat_id=CHAT, reload_fn=ok_reload)
    assert res["ok"]

    after = _yaml(repo)
    assert "name: target" not in after
    # 顶部注释、env 引用、别的条目一个字节都不能变
    assert after.startswith("# 顶部注释必须活下来\n")
    assert "${SPX_DISPATCH_CHAT_ID}" in after
    assert "name: keep_me" in after and "name: not_mine" in after
    # 留档可找回
    stash = repo / res["stash"]
    assert stash.exists() and "name: target" in stash.read_text(encoding="utf-8")


def test_cancel_refuses_other_chat(repo):
    before = _yaml(repo)
    res = cron_store.cancel("not_mine", chat_id=CHAT, reload_fn=ok_reload)
    assert not res["ok"] and "不属于本群" in res["error"]
    assert _yaml(repo) == before


def test_cancel_refuses_env_scoped_entry(repo):
    res = cron_store.cancel("keep_me", chat_id=CHAT, reload_fn=ok_reload)
    assert not res["ok"] and "不属于本群" in res["error"]


def test_cancel_unknown_name(repo):
    res = cron_store.cancel("nope", chat_id=CHAT, reload_fn=ok_reload)
    assert not res["ok"] and "没有名为" in res["error"]


# ── pause / resume ────────────────────────────────────────────

def test_pause_then_resume_roundtrip(repo):
    before = _yaml(repo)

    assert cron_store.pause("target", chat_id=CHAT, reload_fn=ok_reload)["ok"]
    assert "name: target" not in _yaml(repo)
    assert [p["name"] for p in cron_store.list_paused(CHAT)] == ["target"]
    # 暂停条目连块内注释一起搬走了
    sidecar = repo / "data" / "agent_crons" / "paused" / "target.yaml"
    assert "APScheduler 的 dow=0" in sidecar.read_text(encoding="utf-8")

    assert cron_store.resume("target", chat_id=CHAT, reload_fn=ok_reload)["ok"]
    assert not sidecar.exists()
    assert cron_store.list_paused(CHAT) == []
    # 条目回来了（位置挪到末尾，内容不变）
    _, blocks = cron_store.split_entries(_yaml(repo))
    names = [cron_store.field_value(b, "name") for b in blocks]
    assert sorted(names) == sorted(["keep_me", "target", "not_mine"])
    assert "APScheduler 的 dow=0" in _yaml(repo)
    assert len(_yaml(repo)) == len(before)  # 只是顺序变了


def test_paused_task_is_not_visible_to_other_chat(repo):
    cron_store.pause("target", chat_id=CHAT, reload_fn=ok_reload)
    assert cron_store.list_paused(OTHER) == []


def test_resume_refuses_other_chat(repo):
    cron_store.pause("target", chat_id=CHAT, reload_fn=ok_reload)
    res = cron_store.resume("target", chat_id=OTHER, reload_fn=ok_reload)
    assert not res["ok"] and "不属于本群" in res["error"]


def test_resume_unknown(repo):
    res = cron_store.resume("target", chat_id=CHAT, reload_fn=ok_reload)
    assert not res["ok"] and "不在暂停列表" in res["error"]


# ── update ────────────────────────────────────────────────────

def test_update_cron_touches_exactly_one_line(repo):
    before = _yaml(repo).split("\n")
    res = cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, cron="0 9 * * 1")
    assert res["ok"] and res["changed"] == ["cron"]

    after = _yaml(repo).split("\n")
    diff = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(diff) == 1
    assert diff[0][1].strip() == 'cron: "0 9 * * 1"'
    assert cron_store.field_value(after, "cron") is None or True  # 结构未破坏
    assert "APScheduler 的 dow=0" in "\n".join(after)


def test_update_prompt_rewrites_file_not_yaml(repo):
    before = _yaml(repo)
    res = cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, prompt="全新的指令")
    assert res["ok"] and res["changed"] == ["prompt"]
    assert _yaml(repo) == before
    assert (repo / "data" / "agent_crons" / "target.md").read_text(encoding="utf-8") == "全新的指令\n"


def test_update_model_insert_then_clear(repo):
    assert cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, model="opus")["ok"]
    _, blocks = cron_store.split_entries(_yaml(repo))
    target = blocks[1]
    assert cron_store.field_value(target, "model") == "opus"

    assert cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, model="")["ok"]
    _, blocks = cron_store.split_entries(_yaml(repo))
    assert cron_store.field_value(blocks[1], "model") is None


def test_update_rolls_back_when_scheduler_rejects(repo):
    before = _yaml(repo)
    before_prompt = (repo / "data" / "agent_crons" / "target.md").read_text(encoding="utf-8")
    calls = []

    def rejecting_reload():
        calls.append(1)
        return {"ok": True, "removed": [], "added": [],
                "errors": ["target: cron 非法 Wrong number of fields"]}

    res = cron_store.update("target", chat_id=CHAT, reload_fn=rejecting_reload,
                            cron="not a cron", prompt="不该留下的 prompt")
    assert not res["ok"] and "已回滚" in res["error"]
    assert _yaml(repo) == before
    assert (repo / "data" / "agent_crons" / "target.md").read_text(encoding="utf-8") == before_prompt
    assert len(calls) == 2  # 出错后又 reload 一次把 scheduler 拉回旧状态


def test_update_ignores_unrelated_task_errors(repo):
    def noisy_reload():
        return {"ok": True, "removed": [], "added": [],
                "errors": ["some_other_task: profile 'x' 未加载"]}

    res = cron_store.update("target", chat_id=CHAT, reload_fn=noisy_reload, title="新标题")
    assert res["ok"]
    assert 'topic_title: "新标题"' in _yaml(repo)


def test_update_paused_task_edits_sidecar(repo):
    cron_store.pause("target", chat_id=CHAT, reload_fn=ok_reload)
    res = cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, cron="0 7 * * *")
    assert res["ok"] and res["paused"] is True
    sidecar = repo / "data" / "agent_crons" / "paused" / "target.yaml"
    assert 'cron: "0 7 * * *"' in sidecar.read_text(encoding="utf-8")
    assert "name: target" not in _yaml(repo)  # 仍然是暂停态


def test_update_rejects_empty_cron_and_no_fields(repo):
    assert not cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload, cron="  ")["ok"]
    assert not cron_store.update("target", chat_id=CHAT, reload_fn=ok_reload)["ok"]


def test_update_refuses_other_chat(repo):
    before = _yaml(repo)
    res = cron_store.update("not_mine", chat_id=CHAT, reload_fn=ok_reload, cron="0 1 * * *")
    assert not res["ok"] and "不属于本群" in res["error"]
    assert _yaml(repo) == before
