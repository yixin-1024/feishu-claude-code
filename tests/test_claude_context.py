"""claude_context：把 Claude Code 的规则 / 记忆 / skill 接给 qoder。"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_context
import qoder_runner
from claude_context import (
    build_claude_context_brief,
    claude_memory_dir,
    claude_project_slug,
    link_claude_skills,
)


def _skill(root, name, desc="does things"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")
    return d


def _fake_home(tmp_path, monkeypatch):
    claude = tmp_path / "home" / ".claude"
    agents = tmp_path / "home" / ".agents" / "skills"
    (claude / "skills").mkdir(parents=True)
    agents.mkdir(parents=True)
    monkeypatch.setattr(claude_context, "CLAUDE_HOME", str(claude))
    monkeypatch.setattr(claude_context, "CLAUDE_SKILLS_DIR", str(claude / "skills"))
    monkeypatch.setattr(claude_context, "AGENTS_SKILLS_DIR", str(agents))
    return claude, agents


def test_project_slug_matches_claude_code():
    assert claude_project_slug("/Users/me/Desktop/workspace/payment/spx") == \
        "-Users-me-Desktop-workspace-payment-spx"
    assert claude_project_slug("/home/yixin/feishu-claude-code") == "-home-yixin-feishu-claude-code"
    assert claude_project_slug("/home/a/.b_c") == "-home-a--b-c"


def test_link_claude_skills_only_adds_what_qoder_cannot_see(tmp_path, monkeypatch):
    claude, agents = _fake_home(tmp_path, monkeypatch)
    _skill(claude / "skills", "spxpay-config")
    _skill(claude / "skills", "bg-job")
    _skill(agents, "lark-im")
    (claude / "skills" / "lark-im").symlink_to(agents / "lark-im")  # 已在 ~/.agents/skills
    (claude / "skills" / "_shared").mkdir()                          # 公共目录，不是 skill
    (claude / "skills" / "notes").mkdir()                            # 没有 SKILL.md
    qroot = tmp_path / "qoder" / "skills"
    _skill(qroot, "bg-job")                                          # 用户自己放的同名 qoder skill

    created = link_claude_skills(str(qroot))

    assert created == ["spxpay-config"]
    assert os.path.realpath(qroot / "spxpay-config") == os.path.realpath(claude / "skills" / "spxpay-config")
    assert not (qroot / "bg-job").is_symlink()   # 不覆盖用户自己的
    assert not (qroot / "lark-im").exists()
    assert not (qroot / "_shared").exists()
    # 幂等
    assert link_claude_skills(str(qroot)) == []


def test_brief_has_global_rules_project_rules_memory_and_project_skills(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    (claude / "CLAUDE.md").write_text("不许改 ClashX 的节点。")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = home / "work" / "spx"
    sub = proj / "svc"
    sub.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("spx rules")
    (sub / "CLAUDE.md").write_text("svc rules")
    (sub / "AGENTS.md").symlink_to(sub / "CLAUDE.md")   # qoder 会自己加载，不重复提示
    _skill(sub / ".claude" / "skills", "fullstack-feature-delivery", "端到端交付")
    mem = tmp_path / "home" / ".claude" / "projects" / claude_project_slug(str(sub)) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# Memory Index\n- [部署](project_deploy.md) — 走 systemd\n")

    brief = build_claude_context_brief(str(sub), skills_note="SKILLS NOTE")

    assert "不许改 ClashX 的节点。" in brief
    assert str(proj / "CLAUDE.md") in brief
    assert str(sub / "CLAUDE.md") not in brief
    assert "[部署](project_deploy.md)" in brief
    assert str(mem) in brief and "只读" in brief
    assert "SKILLS NOTE" in brief
    assert "fullstack-feature-delivery：端到端交付" in brief
    assert claude_memory_dir(str(sub)) == str(mem)


def test_memory_index_is_cut_at_claude_code_limit(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd = tmp_path / "home" / "p"
    cwd.mkdir()
    mem = claude / "projects" / claude_project_slug(str(cwd)) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("\n".join(f"- line {i}" for i in range(400)))

    brief = build_claude_context_brief(str(cwd))

    assert "- line 199" in brief and "- line 200" not in brief
    assert "超过 200 行" in brief


def test_empty_machine_gives_empty_brief(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "p").mkdir()
    assert build_claude_context_brief(str(tmp_path / "home" / "p")) == ""


def test_qoder_runner_appends_brief_and_links_skills(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    (claude / "CLAUDE.md").write_text("GLOBAL RULE X")
    _skill(claude / "skills", "spxpay-config")
    monkeypatch.setenv("CC_LARK_QODER_CLAUDE_CONTEXT", "1")
    qhome = tmp_path / "qhome"

    captured = {}

    class _Out:
        async def readline(self):
            return b""

    class _Err:
        async def read(self):
            return b""

    class _In:
        def write(self, d): pass
        async def drain(self): pass
        def close(self): pass

    class _Proc:
        stdin, stdout, stderr, pid, returncode = _In(), _Out(), _Err(), 1, None

        async def wait(self):
            self.returncode = 0
            return 0

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(qoder_runner.run_qoder(
        message="hi", cwd=str(tmp_path), append_system_prompt="LARK RULES",
        config_dir=str(qhome),
    ))

    cmd = captured["cmd"]
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert prompt.startswith("LARK RULES\n\n【本机 Claude Code 的规则、记忆和 skill】")
    assert "GLOBAL RULE X" in prompt
    assert str(qhome / "skills") in prompt
    assert (qhome / "skills" / "spxpay-config").is_symlink()


# ── codex / agy 接 Claude 记忆（2026-11-20）──────────────────────────


def _memory(home, cwd, text="# Memory Index\n- [部署](project_deploy.md) — 走 systemd\n"):
    mem = home / "projects" / claude_project_slug(str(cwd)) / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text(text)
    return mem


def test_global_rules_expand_import_lines(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    gm = claude / "global-memory"
    gm.mkdir()
    (gm / "MEMORY.md").write_text("- [称呼](feedback/call_user.md) — 叫 yixin")
    (claude / "CLAUDE.md").write_text(
        f"# 全局\n规则A\n\n@{gm / 'MEMORY.md'}\n\n```\n@{gm / 'MEMORY.md'}\n```\n"
    )
    cwd = tmp_path / "home" / "p"
    cwd.mkdir()

    brief = build_claude_context_brief(str(cwd))

    assert "规则A" in brief
    assert "叫 yixin" in brief
    assert f"相对链接都相对 {gm}" in brief
    assert brief.count("叫 yixin") == 1   # 代码块里的 @ 行不展开


def test_imports_mode_skips_rules_body_but_keeps_global_memory(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (claude / "global.md").write_text("全局记忆条目")
    (claude / "CLAUDE.md").write_text("规则正文\n@global.md\n")
    cwd = tmp_path / "home" / "p"
    cwd.mkdir()

    brief = build_claude_context_brief(str(cwd), global_rules="imports")

    assert "规则正文" not in brief
    assert "全局记忆条目" in brief and "■ 全局记忆" in brief


def test_claude_home_picks_the_other_config_dir(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    research = tmp_path / "home" / ".claude-research"
    research.mkdir()
    (research / "CLAUDE.md").write_text("研究规矩")
    cwd = tmp_path / "data" / "research"
    cwd.mkdir(parents=True)
    _memory(claude, cwd, "SPX 那边的记忆")
    mem = _memory(research, cwd, "研究记忆索引")

    brief = build_claude_context_brief(str(cwd), claude_home=str(research))

    assert "研究规矩" in brief and "研究记忆索引" in brief
    assert "SPX 那边的记忆" not in brief
    assert str(mem) in brief
    assert claude_memory_dir(str(cwd), str(research)) == str(mem)


def test_compact_brief_gives_paths_not_contents(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (claude / "CLAUDE.md").write_text("规则正文")
    proj = tmp_path / "home" / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("项目规矩")
    mem = _memory(claude, proj, "# Memory Index\n- 很长的索引")

    brief = build_claude_context_brief(str(proj), full=False)

    assert "规则正文" not in brief and "很长的索引" not in brief
    assert f"{claude}/CLAUDE.md" in brief
    assert str(proj / "CLAUDE.md") in brief
    assert f"{mem}/MEMORY.md" in brief


def test_skip_rule_files_drops_ones_backend_already_loads(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "home" / "proj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("项目规矩")

    assert str(proj / "CLAUDE.md") in build_claude_context_brief(str(proj))
    assert build_claude_context_brief(str(proj), skip_rule_files=[str(proj / "CLAUDE.md")]) == ""


def test_codex_loaded_claude_mds_follows_codex_project_doc_rules(tmp_path, monkeypatch):
    import codex_runner

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"
    sub = repo / "svc"
    other = sub / "agents"
    other.mkdir(parents=True)
    (repo / ".git").mkdir()
    (tmp_path / "CLAUDE.md").write_text("仓库外面的")    # git 根以上，codex 不读
    (repo / "CLAUDE.md").write_text("root")
    (sub / "CLAUDE.md").write_text("svc")
    (other / "CLAUDE.md").write_text("x")
    (other / "AGENTS.md").write_text("y")               # 有 AGENTS.md 时 codex 读它，不读 CLAUDE.md

    assert codex_runner.codex_loaded_claude_mds(str(other)) == []   # 没配 fallback

    (codex_home / "config.toml").write_text('project_doc_fallback_filenames = ["CLAUDE.md"]\n')
    loaded = codex_runner.codex_loaded_claude_mds(str(other))
    assert sorted(loaded) == sorted([str(repo / "CLAUDE.md"), str(sub / "CLAUDE.md")])

    plain = tmp_path / "plain" / "deep"
    plain.mkdir(parents=True)
    (plain / "CLAUDE.md").write_text("z")
    (plain.parent / "CLAUDE.md").write_text("w")
    # 不在 git 仓库里：只看 cwd
    assert codex_runner.codex_loaded_claude_mds(str(plain)) == [str(plain / "CLAUDE.md")]


def _agent_profile(name, runner):
    from bot_config import Profile
    return Profile(
        name=name, app_id="a", app_secret="b", platform="lark",
        domain="https://open.larksuite.com", default_cwd="/tmp", runner=runner,
        default_model="gpt-6.2-astra" if runner == "codex" else "gemini-4-neon",
    )


def _research_setup(tmp_path, monkeypatch):
    claude, _ = _fake_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (claude / "CLAUDE.md").write_text("SPX 全局规则")
    research = tmp_path / "home" / ".claude-research"
    research.mkdir()
    (research / "CLAUDE.md").write_text("研究全局规矩")
    cwd = tmp_path / "data" / "research"
    cwd.mkdir(parents=True)
    _memory(research, cwd, "研究记忆索引")
    return claude, research, cwd


def test_agent_runner_gives_agy_full_brief_only_on_new_session(tmp_path, monkeypatch):
    import agent_runner

    _, research, cwd = _research_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("CC_LARK_CLAUDE_CONTEXT", raising=False)
    monkeypatch.setenv("CTXTESTGEMINI_CLAUDE_CONTEXT_DIR", str(research))
    monkeypatch.setenv("CTXTESTGEMINI_CLAUDE_CONTEXT", "memory")
    seen = []

    async def fake_agy(**kw):
        seen.append(kw["append_system_prompt"])
        return "ok", "conv", False

    monkeypatch.setattr(agent_runner, "run_agy", fake_agy)
    prof = _agent_profile("ctxtestgemini", "agy")
    for sid in (None, "conv"):
        asyncio.run(agent_runner.run_agent(
            profile=prof, runner="agy", message="hi", session_id=sid, cwd=str(cwd),
            append_system_prompt="LARK PROMPT",
        ))

    first, later = seen
    assert first.startswith("LARK PROMPT") and "研究记忆索引" in first
    assert "研究全局规矩" not in first          # memory 模式不贴全局规则正文
    assert "研究记忆索引" not in later and str(research) in later


def test_agent_runner_codex_uses_claude_env_file_dir_and_full_on_app_server(tmp_path, monkeypatch):
    import agent_runner

    _, research, cwd = _research_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("CC_LARK_CLAUDE_CONTEXT", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nocodex"))
    env_file = tmp_path / ".env.research"
    env_file.write_text(f"CLAUDE_CONFIG_DIR={research}\n")
    seen = []

    async def fake_codex(**kw):
        seen.append(kw["append_system_prompt"])
        return "ok", "t1", False

    monkeypatch.setattr(agent_runner, "run_codex", fake_codex)
    prof = _agent_profile("ctxtestcodex", "codex")
    prof.claude_env_file = str(env_file)

    monkeypatch.setenv("CTXTESTCODEX_CODEX_TRANSPORT", "app-server")
    asyncio.run(agent_runner.run_agent(
        profile=prof, runner="codex", message="hi", session_id="t1", cwd=str(cwd),
    ))
    monkeypatch.setenv("CTXTESTCODEX_CODEX_TRANSPORT", "exec")
    asyncio.run(agent_runner.run_agent(
        profile=prof, runner="codex", message="hi", session_id="t1", cwd=str(cwd),
    ))
    monkeypatch.setenv("CTXTESTCODEX_CLAUDE_CONTEXT", "off")
    asyncio.run(agent_runner.run_agent(
        profile=prof, runner="codex", message="hi", session_id=None, cwd=str(cwd),
    ))

    app_server, exec_resume, off = seen
    assert "研究全局规矩" in app_server and "研究记忆索引" in app_server
    assert "SPX 全局规则" not in app_server
    assert "研究记忆索引" not in exec_resume and str(research) in exec_resume
    assert off is None
