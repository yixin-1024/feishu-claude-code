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
