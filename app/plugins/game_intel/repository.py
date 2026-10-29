"""Game Intelligence storage on the gateway's own Store and ArtifactStore (no second database).
Record kinds: game_project, game_build, game_evidence, game_observation, game_claim, game_claim_link,
game_validation. Evidence content lives in the ArtifactStore (create-only, sha256-verified on read);
the record holds its metadata and provenance. Nothing here updates registered evidence in place."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.models import now

from .models import (
    Claim,
    ClaimEvidenceLink,
    ClaimStatus,
    EvidenceArtifact,
    GameBuild,
    GameProject,
    Observation,
    claim_transition,
)

KINDS = ("game_project", "game_build", "game_evidence", "game_observation", "game_claim",
         "game_claim_link", "game_validation")  # fmt: skip


class GameIntelError(ValueError):
    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


def manifest_fingerprint(files: list[dict]) -> str:
    """A build's identity: sha256 over its sorted (path, sha256) pairs (an inventory manifest)."""
    pairs = sorted((f["path"], f["sha256"]) for f in files)
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


class ProjectRegistry:
    """GameProjectService: projects and their source/build versions, confined to approved roots."""

    def __init__(self, store, allowed_roots: list[Path]):
        self.store = store
        self.roots = [Path(r).expanduser().resolve() for r in allowed_roots if r]

    def _confined(self, root: str | Path) -> Path:
        path = (
            Path(root).expanduser().resolve()
        )  # follows symlinks: an escape through a link is caught
        if not any(path == r or path.is_relative_to(r) for r in self.roots):
            raise GameIntelError(f"{path} is outside the approved project roots", "forbidden")
        if not path.is_dir():
            raise GameIntelError(f"{path} is not a folder")
        return path

    def register_project(self, name: str, root: str | Path) -> GameProject:
        path = str(self._confined(root))
        same = [GameProject.model_validate(b) for b in self.store.list("game_project")
                if b["root"] == path]  # fmt: skip
        if same:  # registering again is a no-op
            return same[0]
        p = GameProject(name=name.strip() or Path(path).name, root=path)
        self.store.put("game_project", p.project_id, p.model_dump(mode="json"))
        return p

    def project(self, project_id: str) -> GameProject:
        body = self.store.get("game_project", project_id)
        if body is None:
            raise GameIntelError(f"unknown project {project_id}", "not_found")
        return GameProject.model_validate(body)

    def set_engine(self, project_id: str, engine: str, evidence_id: str) -> GameProject:
        """Engine detection always cites the evidence it rests on (a marker file), never a name."""
        p = self.project(project_id)
        p.engine, p.engine_evidence = engine, evidence_id
        self.store.put("game_project", p.project_id, p.model_dump(mode="json"))
        return p

    def register_build(self, project_id: str, kind: str, root: str | Path, files: list[dict],
                       revision: str | None = None, label: str = "") -> GameBuild:  # fmt: skip
        self.project(project_id)
        path = str(self._confined(root))
        fp = manifest_fingerprint(files)
        for b in self.store.list("game_build"):
            if b["project_id"] == project_id and b["fingerprint"] == fp and b["kind"] == kind:
                return GameBuild.model_validate(b)  # the same content: the same build
        b = GameBuild(project_id=project_id, kind=kind, root=path, fingerprint=fp, revision=revision,
                      label=label)  # fmt: skip
        self.store.put("game_build", b.build_id, b.model_dump(mode="json"))
        return b


class EvidenceRepository:
    """Immutable evidence with provenance; observations and claims that cite it."""

    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    # ---- evidence ----

    def register(self, *, project_id: str, run_id: str, evidence_type: str, method: str,
                 filename: str, data: bytes | str, build_id: str | None = None,
                 adapter: str = "generic", adapter_version: str = "", captured_at=None,
                 status: str = "collected", limitations: list[str] = (), provenance: dict | None = None,
                 derived_from: str | None = None, version: int = 1) -> EvidenceArtifact:  # fmt: skip
        """Store content + metadata. Idempotent: the same content of the same type and method in the
        same run returns the record already there (a retried collection step adds nothing)."""
        raw = data.encode() if isinstance(data, str) else data
        digest = hashlib.sha256(raw).hexdigest()
        for body in self.store.list("game_evidence", task_id=run_id):
            if (
                body["sha256"],
                body["evidence_type"],
                body["method"],
                body.get("derived_from"),
            ) == (digest, evidence_type, method, derived_from):
                return EvidenceArtifact.model_validate(body)
        art = self.artifacts.add(
            run_id, f"evidence:{evidence_type}", filename, raw, producer=method
        )
        ev = EvidenceArtifact(
            project_id=project_id, run_id=run_id, build_id=build_id, evidence_type=evidence_type,
            method=method, adapter=adapter, adapter_version=adapter_version, artifact_id=art.artifact_id,
            sha256=art.sha256, size_bytes=art.size_bytes, captured_at=captured_at, status=status,
            limitations=list(limitations), provenance=provenance or {}, derived_from=derived_from,
            version=version,
        )  # fmt: skip
        self.store.put("game_evidence", ev.evidence_id, ev.model_dump(mode="json"), task_id=run_id,
                       run_id=run_id, state=ev.status)  # fmt: skip
        return ev

    def correct(self, evidence_id: str, data: bytes | str, reason: str, run_id: str | None = None,
                filename: str | None = None) -> EvidenceArtifact:  # fmt: skip
        """A correction or reprocessing: a new version derived from the old one, which stays as it was."""
        if not reason.strip():
            raise GameIntelError("a correction needs a reason")
        old = self.evidence(evidence_id)
        return self.register(
            project_id=old.project_id, run_id=run_id or old.run_id, evidence_type=old.evidence_type,
            method=old.method, filename=filename or f"v{old.version + 1}", data=data,
            build_id=old.build_id, adapter=old.adapter, adapter_version=old.adapter_version,
            limitations=old.limitations, provenance={**old.provenance, "correction": reason.strip()},
            derived_from=old.evidence_id, version=old.version + 1,
        )  # fmt: skip

    def evidence(self, evidence_id: str) -> EvidenceArtifact:
        body = self.store.get("game_evidence", evidence_id)
        if body is None:
            raise GameIntelError(f"unknown evidence {evidence_id}", "not_found")
        return EvidenceArtifact.model_validate(body)

    def read(self, evidence_id: str) -> bytes:
        """The content, verified: raises when the artifact changed on disk or does not match."""
        ev = self.evidence(evidence_id)
        art = self.artifacts.get(ev.artifact_id)
        if art is None:
            raise GameIntelError(f"evidence {evidence_id}: its artifact is gone", "integrity")
        data = self.artifacts.read(art)  # sha256-verified against the artifact record
        if hashlib.sha256(data).hexdigest() != ev.sha256:
            raise GameIntelError(f"evidence {evidence_id} does not match its checksum", "integrity")
        return data

    def for_run(self, run_id: str) -> list[EvidenceArtifact]:
        return [
            EvidenceArtifact.model_validate(b)
            for b in self.store.list("game_evidence", run_id=run_id)
        ]

    # ---- observations ----

    def observe(self, *, project_id: str, run_id: str, statement: str, evidence_ids: list[str],
                scope: dict | None = None) -> Observation:  # fmt: skip
        for eid in evidence_ids:
            if self.evidence(eid).project_id != project_id:
                raise GameIntelError(f"evidence {eid} belongs to another project")
        ob = Observation(project_id=project_id, run_id=run_id, statement=statement,
                         evidence_ids=list(evidence_ids), scope=scope or {})  # fmt: skip
        self.store.put("game_observation", ob.observation_id, ob.model_dump(mode="json"),
                       task_id=run_id, run_id=run_id)  # fmt: skip
        return ob

    def observation(self, observation_id: str) -> Observation:
        body = self.store.get("game_observation", observation_id)
        if body is None:
            raise GameIntelError(f"unknown observation {observation_id}", "not_found")
        return Observation.model_validate(body)

    # ---- claims ----

    def claim(self, *, project_id: str, statement: str, scope: dict | None = None) -> Claim:
        c = Claim(project_id=project_id, statement=statement, scope=scope or {})
        self.store.put("game_claim", c.claim_id, c.model_dump(mode="json"), state=c.status)
        return c

    def get_claim(self, claim_id: str) -> Claim:
        body = self.store.get("game_claim", claim_id)
        if body is None:
            raise GameIntelError(f"unknown claim {claim_id}", "not_found")
        return Claim.model_validate(body)

    def link(self, claim_id: str, relation: str, *, evidence_id: str | None = None,
             observation_id: str | None = None, note: str = "") -> ClaimEvidenceLink:  # fmt: skip
        c = self.get_claim(claim_id)
        if evidence_id:
            target = self.evidence(evidence_id)
            method = target.method
        elif observation_id:
            target = self.observation(observation_id)
            method = ",".join(sorted({self.evidence(e).method for e in target.evidence_ids}))
        else:
            raise GameIntelError("a link cites an evidence or an observation")
        if target.project_id != c.project_id:
            raise GameIntelError(
                "evidence of another project cannot support or contradict this claim"
            )
        ln = ClaimEvidenceLink(claim_id=claim_id, relation=relation, evidence_id=evidence_id,
                               observation_id=observation_id, method=method, note=note)  # fmt: skip
        self.store.put("game_claim_link", ln.link_id, ln.model_dump(mode="json"), run_id=claim_id)
        return ln

    def links(self, claim_id: str) -> list[ClaimEvidenceLink]:
        return [ClaimEvidenceLink.model_validate(b)
                for b in self.store.list("game_claim_link", run_id=claim_id)]  # fmt: skip

    def transition(self, claim_id: str, to: ClaimStatus, reason: str) -> Claim:
        """Move a claim to `to` when its links allow it (models.claim_transition); recorded in history."""
        c = self.get_claim(claim_id)
        if why := claim_transition(c, to, self.links(claim_id)):
            raise GameIntelError(
                f"claim {claim_id}: {c.status} -> {to} refused: {why}", "invalid_transition"
            )
        if to == "hypothesis" and not reason.strip():
            raise GameIntelError("reopening a claim needs a reason")
        c.history.append(
            {"from": c.status, "to": to, "reason": reason.strip(), "at": now().isoformat()}
        )
        c.status = to
        self.store.put("game_claim", c.claim_id, c.model_dump(mode="json"), state=c.status)
        return c
