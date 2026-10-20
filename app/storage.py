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
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self._items: dict[str, Attachment] = {}
        root.mkdir(parents=True, exist_ok=True)

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


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self._items: dict[str, Artifact] = {}
        root.mkdir(parents=True, exist_ok=True)

    def add(self, task_id: str, kind: str, filename: str, data: bytes | str, **meta) -> Artifact:
        raw = data.encode() if isinstance(data, str) else data
        name = Path(filename).name or "artifact"
        art = Artifact(
            task_id=task_id,
            kind=kind,
            filename=name,
            path=Path("."),
            sha256=sha256(raw),
            size_bytes=len(raw),
            meta=meta,
        )
        path = _inside(self.root, self.root / _safe_segment(task_id) / f"{art.artifact_id}_{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:
            f.write(raw)
        art.path = path
        self._items[art.artifact_id] = art
        return art

    def get(self, artifact_id: str) -> Artifact | None:
        return self._items.get(artifact_id)

    def for_task(self, task_id: str) -> list[Artifact]:
        return [a for a in self._items.values() if a.task_id == task_id]
