"""Docs rot checks for README.md, CONTRIBUTING.md and the repo skills.

Why: the README is the only thing a first-time user has, and it rots in two ways
this suite catches. Relative links break when files move, and commands drift when
a lane adds or renames a subcommand without touching the docs. Run from the
repository root (no project needed):

    uv run --with pytest pytest docs/tests -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
SKILLS = sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
REFERENCES = sorted((ROOT / ".agents" / "skills").glob("*/references/*.md"))
DOC_FILES = [README, CONTRIBUTING, ROOT / "docs" / "checks.md", *SKILLS, *REFERENCES]

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# Any language tag (bash, json, text, …): an unmatched opener would mis-pair every fence after it.
_FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _relative_links(md: Path) -> list[str]:
    return [t for t in _LINK.findall(md.read_text(encoding="utf-8")) if not t.startswith(("http://", "https://", "mailto:"))]


def _fenced_lines(md: Path) -> list[str]:
    out: list[str] = []
    for block in _FENCE.findall(md.read_text(encoding="utf-8")):
        out += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return out


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc: Path) -> None:
    missing = []
    for target in _relative_links(doc):
        path = target.split("#", 1)[0]
        if path and not (doc.parent / path).exists():
            missing.append(target)
    assert not missing, f"{doc.relative_to(ROOT)}: broken links {missing}"


def test_readme_uv_commands_name_real_projects_and_scripts() -> None:
    projects = set()
    scripts = set()
    for line in _fenced_lines(README):
        m = re.search(r"uv run (?:--locked )?--project (\S+)", line)
        if m:
            projects.add(m.group(1))
        scripts.update(re.findall(r"scripts/\S+\.py", line))
    assert projects == {"bot", "render"}
    for p in projects:
        assert (ROOT / p / "pyproject.toml").exists(), p
    assert scripts, "README should show the gate and the ffmpeg installer"
    for s in scripts:
        assert (ROOT / s).exists(), s


def test_readme_documents_every_clipbot_subcommand() -> None:
    """When a lane adds a subcommand (reel, outline, …) the README must show it."""
    cli = (ROOT / "bot" / "clipbot" / "cli.py").read_text(encoding="utf-8")
    subcommands = set(re.findall(r'add_parser\(\s*"([a-z]+)"', cli))
    assert subcommands >= {"plan", "summarize"}
    readme = README.read_text(encoding="utf-8")
    undocumented = sorted(s for s in subcommands if f"clipbot {s}" not in readme)
    assert not undocumented, f"README does not mention: {undocumented}"


def test_readme_is_about_the_tool_and_contributing_holds_the_process() -> None:
    """Kyle's ask: the README is for using the app; beads onboarding lives in CONTRIBUTING."""
    readme = README.read_text(encoding="utf-8")
    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    assert "bd dolt" not in readme and "bd bootstrap" not in readme
    assert "clipbot reel" in readme and "cliprender" in readme
    assert "CONTRIBUTING.md" in readme
    for needle in ("bd bootstrap", "lane:bot", "lane:render", "HANDOFF", "APPROVE", "MERGED"):
        assert needle in contributing, needle
    assert len(readme.splitlines()) <= 200, "keep the README tight; details go to docs/ and CONTRIBUTING.md"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    front = text.split("---", 2)[1]
    name = re.search(r"^name:\s*(\S+)", front, re.M)
    assert name and name.group(1) == skill.parent.name, "frontmatter name must match the directory"
    assert re.search(r"^description:\s*\S", front, re.M), "frontmatter needs a description"
    agents = skill.parent / "agents" / "openai.yaml"
    assert agents.exists(), "each skill ships agents/openai.yaml like the existing ones"
