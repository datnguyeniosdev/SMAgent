"""Game Intelligence entities (schema version 1). Stored as JSON records in the gateway Store (kinds
`game_*`), large content in the ArtifactStore. Facts and interpretations stay apart: an Observation is
what was directly seen (it cites evidence), a Claim is an interpretation whose status follows from its
evidence links (see `claim_transition`). There is no probability anywhere: `strength` reports counts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models import new_id, now

SCHEMA_VERSION = 1

EvidenceType = Literal[
    "manifest", "source", "config", "asset_metadata", "level_data", "log", "screenshot", "video",
    "runtime_event", "telemetry", "playtest_observation", "operator_note", "other",
]  # fmt: skip
ClaimStatus = Literal["hypothesis", "supported", "verified", "contradicted", "unresolved"]
Relation = Literal["supports", "contradicts"]


class Entity(BaseModel):
    schema_version: int = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=now)


class GameProject(Entity):
    project_id: str = Field(default_factory=lambda: new_id("gpr"))
    name: str
    root: str  # resolved, inside an approved projects root
    engine: str | None = None  # detected from evidence, never from the folder name
    engine_evidence: str | None = None  # the marker/evidence id the detection rests on


class GameBuild(Entity):
    build_id: str = Field(default_factory=lambda: new_id("gbd"))
    project_id: str
    kind: Literal["source", "build"]
    root: str
    fingerprint: str  # sha256 over the manifest (path, sha256) pairs
    revision: str | None = None  # git commit, version string or build number
    label: str = ""


class EvidenceArtifact(Entity):
    """Immutable once registered: a correction is a new version (`derived_from`, `version` + 1)."""

    evidence_id: str = Field(default_factory=lambda: new_id("gev"))
    project_id: str
    run_id: str  # the gateway task of the analysis run
    build_id: str | None = None
    evidence_type: EvidenceType
    method: str  # how it was collected: tool name, adapter op, "operator", ...
    adapter: str = "generic"
    adapter_version: str = ""
    artifact_id: str
    sha256: str
    size_bytes: int
    captured_at: datetime | None = None
    status: Literal["collected", "partial"] = "collected"
    limitations: list[str] = []
    provenance: dict = {}  # source path, revision, tool versions, permissions
    derived_from: str | None = None
    version: int = 1


class Observation(Entity):
    """What was directly observed. Never an interpretation; always cites evidence."""

    observation_id: str = Field(default_factory=lambda: new_id("gob"))
    project_id: str
    run_id: str
    statement: str = Field(min_length=3)
    scope: dict = {}  # e.g. {"level": "1-1", "entity": "player"}
    evidence_ids: list[str] = Field(min_length=1)


class ClaimEvidenceLink(Entity):
    link_id: str = Field(default_factory=lambda: new_id("gln"))
    claim_id: str
    relation: Relation
    evidence_id: str | None = None
    observation_id: str | None = None
    method: str = ""  # copied from the evidence: independence of methods is what "verified" needs
    note: str = ""

    @model_validator(mode="after")
    def _one_target(self):
        if (self.evidence_id is None) == (self.observation_id is None):
            raise ValueError("a link cites exactly one evidence or one observation")
        return self


class Claim(Entity):
    claim_id: str = Field(default_factory=lambda: new_id("gcl"))
    project_id: str
    statement: str = Field(min_length=3)
    scope: dict = {}
    status: ClaimStatus = "hypothesis"
    history: list[dict] = []  # {"from", "to", "reason", "at"}


class ValidationResult(Entity):
    validation_id: str = Field(default_factory=lambda: new_id("gvr"))
    project_id: str
    run_id: str
    subject: str  # what was validated: an id or an export name
    passed: bool
    problems: list[str] = []


def strength(links: list[ClaimEvidenceLink]) -> dict:
    """Plain counts, not a probability: how many links support or contradict, by how many methods."""
    sup = [x for x in links if x.relation == "supports"]
    con = [x for x in links if x.relation == "contradicts"]
    return {"supporting": len(sup), "contradicting": len(con),
            "supporting_methods": len({x.method for x in sup if x.method})}  # fmt: skip


def claim_transition(claim: Claim, to: ClaimStatus, links: list[ClaimEvidenceLink]) -> str | None:
    """None when `claim` may move to `to` given its links, else why not.
    hypothesis   -> supported     >= 1 supporting link, none contradicting
    supported    -> verified      supporting links from >= 2 distinct methods, none contradicting
    any          -> contradicted  >= 1 contradicting link
    any          -> unresolved    both supporting and contradicting links
    contradicted / unresolved -> hypothesis   reopened (the reason is required by the caller)"""
    s = strength(links)
    if to == claim.status:
        return f"already {to}"
    if to == "supported":
        if claim.status != "hypothesis":
            return "only a hypothesis becomes supported"
        if not s["supporting"] or s["contradicting"]:
            return "needs a supporting link and none contradicting"
    elif to == "verified":
        if claim.status != "supported":
            return "only a supported claim becomes verified"
        if s["contradicting"] or s["supporting_methods"] < 2:
            return "needs support from two independent methods and none contradicting"
    elif to == "contradicted":
        if not s["contradicting"]:
            return "needs a contradicting link"
    elif to == "unresolved":
        if not (s["supporting"] and s["contradicting"]):
            return "needs both supporting and contradicting links"
    elif to == "hypothesis" and claim.status not in ("contradicted", "unresolved"):
        return "only a contradicted or unresolved claim is reopened"
    return None
