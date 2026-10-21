"""Attachment and artifact storage on the local filesystem.

User-supplied filenames never become path components: files are stored as
<root>/<task_id>/<generated_id><ext>. Archives are stored but never extracted.
"""

import hashlib
import shutil
from pathlib import Path

from app.models import Artifact, Attachment


class AttachmentError(ValueError):
    pass


# mime -> (extension, signature check)
_BINARY = {
    "image/png": (".png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": (".jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "image/gif": (".gif", lambda b: b[:6] in (b"GIF87a", b"GIF89a")),
    "image/webp": (".webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    "application/pdf": (".pdf", lambda b: b.startswith(b"%PDF-")),
}
_TEXT = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/x-csharp": ".cs",
    "text/x-python": ".py",
}
ALLOWED_MIME = set(_BINARY) | set(_TEXT)


def sniff_ok(mime: str, data: bytes) -> bool:
    if mime in _BINARY:
        return _BINARY[mime][1](data)
    if mime in _TEXT:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return b"\x00" not in data
    return False


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_segment(value: str) -> str:
    if not value or not value.replace("_", "").isalnum():
        raise AttachmentError(f"invalid id segment: {value!r}")
    return value


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise AttachmentError("path escapes storage root")
    return resolved


class AttachmentStore:
    def __init__(self, root: Path, max_bytes: int, store=None):
        self.root = root
        self.max_bytes = max_bytes
        self.store = store
        self._items: dict[str, Attachment] = {}
        root.mkdir(parents=True, exist_ok=True)
        for body in store.list("attachment") if store else []:
            att = Attachment.model_validate(body)
            self._items[att.attachment_id] = att

    def _persist(self, att: Attachment) -> None:
        if self.store:
            self.store.put("attachment", att.attachment_id, att, task_id=att.task_id)

    def save(
        self,
        data: bytes,
        filename: str,
        mime_type: str,
        source: str,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> Attachment:
        mime_type = (mime_type or "").split(";")[0].strip().lower()
        if len(data) > self.max_bytes:
            raise AttachmentError(f"{filename}: {len(data)} bytes exceeds {self.max_bytes}")
        if not data:
            raise AttachmentError(f"{filename}: empty file")
        if mime_type not in ALLOWED_MIME:
            raise AttachmentError(f"{filename}: type {mime_type!r} not permitted")
        if not sniff_ok(mime_type, data):
            raise AttachmentError(f"{filename}: content does not match {mime_type}")
        ext = _BINARY[mime_type][0] if mime_type in _BINARY else _TEXT[mime_type]
        att = Attachment(
            filename=Path(filename).name[:200] or "file",
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=sha256(data),
            storage_path=Path("."),
            source=source,
            task_id=task_id,
            message_id=message_id,
        )
        folder = _safe_segment(task_id or "inbox")
        path = _inside(self.root, self.root / folder / f"{att.attachment_id}{ext}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:  # never overwrite
            f.write(data)
        att.storage_path = path
        self._items[att.attachment_id] = att
        self._persist(att)
        return att

    def get(self, attachment_id: str, task_id: str | None = None) -> Attachment:
        att = self._items.get(attachment_id)
        if att is None:
            raise AttachmentError(f"unknown attachment {attachment_id}")
        if task_id is not None and att.task_id not in (None, task_id):
            raise AttachmentError(f"attachment {attachment_id} belongs to another task")
        return att

    def bind(self, attachment_id: str, task_id: str) -> Attachment:
        """Assign an inbox attachment to a task (one-way)."""
        att = self.get(attachment_id, task_id)
        att.task_id = task_id
        self._persist(att)
        return att

    def read(self, att: Attachment) -> bytes:
        path = _inside(self.root, att.storage_path)
        if path.is_symlink():
            raise AttachmentError("refusing to read symlink")
        return path.read_bytes()

    def cleanup_task(self, task_id: str, in_use: set[str]) -> bool:
        """Delete a task's media directory unless the task is still in use."""
        if task_id in in_use:
            return False
        folder = _inside(self.root, self.root / _safe_segment(task_id))
        if folder.is_dir():
            shutil.rmtree(folder)
        for k in [k for k, a in self._items.items() if a.task_id == task_id]:
            del self._items[k]
        return True


_MIME = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".diff": "text/x-diff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".html": "text/html",
}


class ArtifactStore:
    def __init__(self, root: Path, store=None, max_bytes: int = 50 * 1024 * 1024):
        self.root = root
        self.store = store
        self.max_bytes = max_bytes
        self._items: dict[str, Artifact] = {}
        root.mkdir(parents=True, exist_ok=True)
        for body in store.list("artifact") if store else []:
            art = Artifact.model_validate(body)
            self._items[art.artifact_id] = art

    def _persist(self, art: Artifact) -> None:
        if self.store:
            self.store.put("artifact", art.artifact_id, art, task_id=art.task_id)

    def set_validation(self, artifact_id: str, status: str) -> None:
        art = self._items[artifact_id]
        art.validation_status = status
        self._persist(art)

    def read(self, art: Artifact) -> bytes:
        path = _inside(self.root, art.path)
        if path.is_symlink():
            raise AttachmentError("refusing to read symlink")
        data = path.read_bytes()
        if sha256(data) != art.sha256:
            raise AttachmentError(f"artifact {art.artifact_id} content changed on disk")
        return data

    def add(
        self,
        task_id: str,
        kind: str,
        filename: str,
        data: bytes | str,
        *,
        step_id: str | None = None,
        producer: str | None = None,
        **meta,
    ) -> Artifact:
        raw = data.encode() if isinstance(data, str) else data
        if len(raw) > self.max_bytes:
            raise AttachmentError(f"artifact {filename} exceeds {self.max_bytes} bytes")
        name = Path(filename).name or "artifact"
        art = Artifact(
            task_id=task_id,
            kind=kind,
            filename=name,
            path=Path("."),
            sha256=sha256(raw),
            size_bytes=len(raw),
            mime_type=_MIME.get(Path(name).suffix.lower(), "application/octet-stream"),
            step_id=step_id,
            producer=producer,
            meta=meta,
        )
        path = _inside(self.root, self.root / _safe_segment(task_id) / f"{art.artifact_id}_{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:
            f.write(raw)
        art.path = path
        self._items[art.artifact_id] = art
        self._persist(art)
        return art

    def delete(self, artifact_id: str) -> None:
        art = self._items.pop(artifact_id)
        path = _inside(self.root, art.path)
        if path.exists() and not path.is_symlink():
            path.unlink()
        if self.store:
            self.store.put(
                "artifact_deleted",
                artifact_id,
                {"task_id": art.task_id, "sha256": art.sha256},
                task_id=art.task_id,
            )

    def all(self) -> list[Artifact]:
        return list(self._items.values())

    def get(self, artifact_id: str) -> Artifact | None:
        return self._items.get(artifact_id)

    def for_task(self, task_id: str) -> list[Artifact]:
        return [a for a in self._items.values() if a.task_id == task_id]
