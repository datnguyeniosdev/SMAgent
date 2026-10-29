"""Skill catalog for routing: a project's own skills live in its folder (`.claude/skills/<name>/SKILL.md`),
shared ones in `~/.claude/skills`. The Gateway Agent names the skills a request needs when it
dispatches; a lane that lacks one (a skill of another project) gets it imported into its own folder,
and the message records name, source and version (sha256 of SKILL.md) so the run is reproducible.
Only skills that exist are offered or accepted: an unknown name is refused, never substituted."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

GLOBAL = Path.home() / ".claude" / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    source: str  # "global" or the project name
    path: Path  # the skill folder (holds SKILL.md)
    version: str  # first 12 hex of sha256(SKILL.md)

    def ref(self) -> dict:
        return {"name": self.name, "source": self.source, "version": self.version}


def _read(folder: Path, source: str) -> list[Skill]:
    out = []
    if not folder.is_dir():
        return out
    for d in sorted(folder.iterdir()):
        md = d / "SKILL.md"
        if d.is_dir() and not d.is_symlink() and md.is_file():
            out.append(Skill(d.name, source, d, hashlib.sha256(md.read_bytes()).hexdigest()[:12]))
    return out


def project_skills(name: str, root: str | Path) -> list[Skill]:
    return _read(Path(root) / ".claude" / "skills", name) if root else []


def global_skills(home: Path | None = None) -> list[Skill]:
    return _read(home or GLOBAL, "global")


def resolve(names: list[str], project: str, projects: list[tuple[str, str]],
            home: Path | None = None) -> list[Skill]:  # fmt: skip
    """`names` for a request of `project`: its own skill first, then a global one, then a skill of
    another project (`other project/skill`, or a bare name found in exactly one project).
    projects: (name, root) of every known project. Raises ValueError for unknown or ambiguous."""
    own = {s.name: s for p, r in projects if p == project for s in project_skills(p, r)}
    shared = {s.name: s for s in global_skills(home)}
    out, seen = [], set()
    for raw in names:
        raw = raw.strip()
        if not raw:
            continue
        if "/" in raw:
            pname, sname = raw.rsplit("/", 1)
            key = re.sub(r"[^0-9a-z]+", "", pname.lower())
            hits = [s for p, r in projects if re.sub(r"[^0-9a-z]+", "", p.lower()) == key
                    for s in project_skills(p, r) if s.name == sname]  # fmt: skip
        elif raw in own:
            hits = [own[raw]]
        elif raw in shared:
            hits = [shared[raw]]
        else:
            hits = [
                s
                for p, r in projects
                if p != project
                for s in project_skills(p, r)
                if s.name == raw
            ]
        if not hits:
            raise ValueError(f"unknown skill {raw!r}: name one of the listed skills")
        if len({h.source for h in hits}) > 1:
            raise ValueError(f"skill {raw!r} exists in {', '.join(sorted({h.source for h in hits}))}: "
                             "name it as <project>/<skill>")  # fmt: skip
        if hits[0].name not in seen:  # the same skill twice is injected once
            seen.add(hits[0].name)
            out.append(hits[0])
    return out


def import_into(skill: Skill, folder: str | Path) -> bool:
    """Make `skill` available in a lane folder: copied to <folder>/.claude/skills/<name> when it is
    missing there or differs (a project's own skill already in place is left alone)."""
    if skill.source == "global":
        return False  # every session sees ~/.claude/skills
    dst = Path(folder) / ".claude" / "skills" / skill.name
    if dst.resolve() == skill.path.resolve():
        return False
    md = dst / "SKILL.md"
    if md.is_file() and hashlib.sha256(md.read_bytes()).hexdigest()[:12] == skill.version:
        return False
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(skill.path, dst, symlinks=False)
    return True


def note(skills: list[Skill]) -> str:
    if not skills:
        return ""
    names = ", ".join(f"`{s.name}` ({s.source}@{s.version})" for s in skills)
    return (f"\n\nSkills for this request (chosen by the Gateway Agent): load {names} before anything "
            "else (Claude Code: the Skill tool; other agent tools: read .claude/skills/<name>/SKILL.md "
            "in this folder) and follow them.")  # fmt: skip
