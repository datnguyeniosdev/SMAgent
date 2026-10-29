"""Projects with stable ids. Every session, message and task names its project by `project_id`; the
display name can change (README rename) and old names stay as aliases, so state keyed by the id
survives a rename. `routing_mode` says who places a project's work: its pinned main conversation
("main", today's behaviour) or the gateway's own rules with the shared Gateway Agent as fallback
("central"), so projects move one at a time and can move back."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import new_id, now

RoutingMode = Literal["main", "central"]
# product: a game/app repo, one worker at a time on its code (lane dev), deploy and QA test in lanes
# of their own; tool: an agent project (UIUX, PO, QA, research) whose requests run in parallel
ProjectKind = Literal["product", "tool"]


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())


class Project(BaseModel):
    schema_version: int = 1
    project_id: str = Field(default_factory=lambda: new_id("prj"))
    name: str
    aliases: list[str] = []  # earlier names
    routing_mode: RoutingMode = "main"
    kind: ProjectKind = "product"
    root: str = ""  # the project folder (from its sessions): lanes are copied from here
    created_at: datetime = Field(default_factory=now)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


class ProjectRegistry:
    def __init__(self, store):
        self.store = store

    def all(self) -> list[Project]:
        return [Project.model_validate(b) for b in self.store.list("project")]

    def _save(self, p: Project) -> Project:
        self.store.put("project", p.project_id, p.model_dump(mode="json"), state=p.routing_mode)
        return p

    def get(self, project_id: str) -> Project | None:
        body = self.store.get("project", project_id)
        return Project.model_validate(body) if body else None

    def resolve(self, ref: str) -> Project | None:
        """By id, current name or an earlier name (exact, normalized)."""
        if ref.startswith("prj_") and (p := self.get(ref)):
            return p
        key = norm(ref)
        return next((p for p in self.all() if key and key in {norm(n) for n in p.names()}), None)

    def ensure(self, name: str) -> Project:
        """The project of that name (or alias), registered on first sight."""
        return self.resolve(name) or self._save(Project(name=name.strip()))

    def rename(self, ref: str, new_name: str) -> Project:
        p = self.resolve(ref)
        if p is None:
            raise KeyError(ref)
        if other := self.resolve(new_name):
            if other.project_id != p.project_id:
                raise ValueError(f"{new_name!r} already names {other.project_id}")
        if norm(new_name) != norm(p.name):
            p.aliases = [a for a in [*p.aliases, p.name] if norm(a) != norm(new_name)]
            p.name = new_name.strip()
        return self._save(p)

    def note_root(self, name: str, cwd: str) -> None:
        """A session of the project opened in `cwd`: its project folder is remembered (a lane copy or
        an app worktree points back to the project, not to itself)."""
        if not cwd or "/.agw-children/" in cwd + "/":
            return
        root = cwd.split("/.claude/worktrees/")[0]
        p = self.ensure(name)
        if not p.root or (len(root) < len(p.root) and p.root.startswith(root + "/")):
            p.root = root
            self._save(p)

    def set_kind(self, ref: str, kind: ProjectKind) -> Project:
        p = self.resolve(ref)
        if p is None:
            raise KeyError(ref)
        p.kind = kind
        return self._save(p)

    def set_routing_mode(self, ref: str, mode: RoutingMode) -> Project:
        p = self.resolve(ref)
        if p is None:
            raise KeyError(ref)
        p.routing_mode = mode
        return self._save(p)
