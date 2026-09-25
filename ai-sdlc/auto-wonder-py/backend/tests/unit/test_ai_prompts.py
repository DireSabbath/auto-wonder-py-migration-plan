"""AI 场景提示和克隆命令与 Java 适配器一致。"""

from pathlib import Path

from autowonder.ai.prompts import allowed_tools, user_prompt, with_api_suffix
from autowonder.ai.repo_prep import build_clone_command, repo_dir_name


def test_scene_prompts_follow_the_java_defaults() -> None:
    """空白输入走各场景默认句，SDLC 不开放工具。"""
    assert user_prompt("MEMORY_IMPORT", None) == "请提炼以下内容为记忆条目。"
    assert user_prompt("CLARIFICATION", "  ") == "请分析并澄清该需求。"
    repo = user_prompt("REPO_SCAN", "/tmp/repo")
    assert repo.startswith("请扫描本地仓库: /tmp/repo")
    sdlc = user_prompt("SDLC_GEN", "直接生成")
    assert "SDLC workflow JSON" in sdlc
    assert sdlc.endswith("直接生成")
    assert allowed_tools("SDLC_GEN") == ""
    assert allowed_tools("AGENT_CONFIG_GEN") == ""
    assert allowed_tools("REPO_SCAN") is None
    assert with_api_suffix("正文").endswith("直接用文字提问和回复。")


def test_clone_command_adds_a_single_branch() -> None:
    """有默认分支时浅克隆只取该分支。"""
    command = build_clone_command("git", "file:///repo", "main", Path("/tmp/checkout"))
    assert command == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "main",
        "--single-branch",
        "file:///repo",
        "/tmp/checkout",
    ]
    plain = build_clone_command("git", "file:///repo", "  ", Path("/tmp/checkout"))
    assert "--branch" not in plain
    assert repo_dir_name("my repo", 3) == "my_repo"
    assert repo_dir_name(None, 3) == "3"
