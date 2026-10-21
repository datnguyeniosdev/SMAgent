"""Image inspection (stdlib only), configurable asset validation rules and manifests."""

import struct
from dataclasses import asdict, dataclass, field

from app.storage import sha256


@dataclass
class ImageInfo:
    format: str
    width: int | None
    height: int | None
    has_alpha: bool | None  # None = could not determine
    color_mode: str | None


def inspect_image(data: bytes) -> ImageInfo:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        w, h, _depth, ctype = struct.unpack(">IIBB", data[16:26])
        modes = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}
        alpha = ctype in (4, 6) or b"tRNS" in data[: data.find(b"IDAT")]
        return ImageInfo("png", w, h, alpha, modes.get(ctype))
    if data.startswith(b"\xff\xd8\xff"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            seglen = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return ImageInfo("jpeg", w, h, False, "RGB")
            i += 2 + seglen
        return ImageInfo("jpeg", None, None, False, "RGB")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            alpha = bool(data[20] & 0x10)
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return ImageInfo("webp", w, h, alpha, None)
        return ImageInfo("webp", None, None, None, None)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return ImageInfo("gif", w, h, None, "P")
    return ImageInfo("unknown", None, None, None, None)


@dataclass
class AssetRules:
    require_alpha: bool = True
    max_dimension: int = 4096
    power_of_two: bool = False
    expected_width: int | None = None
    expected_height: int | None = None


def validate(info: ImageInfo, rules: AssetRules) -> list[str]:
    issues = []
    if info.format != "png":
        issues.append(f"format is {info.format}, expected png")
    if info.width is None or info.height is None:
        issues.append("dimensions could not be read")
        return issues
    if max(info.width, info.height) > rules.max_dimension:
        issues.append(f"{info.width}x{info.height} exceeds max {rules.max_dimension}")
    if rules.power_of_two and any(d & (d - 1) for d in (info.width, info.height)):
        issues.append(f"{info.width}x{info.height} is not power-of-two (atlas/compression)")
    if rules.expected_width and (info.width, info.height) != (
        rules.expected_width,
        rules.expected_height,
    ):
        issues.append(
            f"size {info.width}x{info.height} != expected "
            f"{rules.expected_width}x{rules.expected_height}"
        )
    if rules.require_alpha and info.has_alpha is not True:
        issues.append("no alpha channel")
    return issues


@dataclass
class ManifestEntry:
    asset_id: str
    task_id: str
    name: str
    reference_ids: list[str]
    provider: str | None
    model: str | None
    prompt_artifact_id: str | None
    width: int | None
    height: int | None
    format: str
    color_mode: str | None
    has_alpha: bool | None
    sha256: str
    storage_path: str
    validation_status: str  # passed | failed
    validation_issues: list[str] = field(default_factory=list)
    approval_status: str = "pending"  # pending | approved | rejected


def manifest_entry(
    asset_id: str,
    task_id: str,
    name: str,
    data: bytes,
    storage_path: str,
    rules: AssetRules,
    reference_ids: list[str],
    provider: str | None = None,
    model: str | None = None,
    prompt_artifact_id: str | None = None,
) -> ManifestEntry:
    info = inspect_image(data)
    issues = validate(info, rules)
    return ManifestEntry(
        asset_id=asset_id,
        task_id=task_id,
        name=name,
        reference_ids=reference_ids,
        provider=provider,
        model=model,
        prompt_artifact_id=prompt_artifact_id,
        width=info.width,
        height=info.height,
        format=info.format,
        color_mode=info.color_mode,
        has_alpha=info.has_alpha,
        sha256=sha256(data),
        storage_path=storage_path,
        validation_status="failed" if issues else "passed",
        validation_issues=issues,
    )


def check_manifest(entries: list[ManifestEntry], expected_names: list[str]) -> list[str]:
    """Cross-asset checks: duplicates by hash, missing and duplicate names."""
    problems = []
    seen: dict[str, str] = {}
    for e in entries:
        if e.sha256 in seen:
            problems.append(f"{e.name} duplicates {seen[e.sha256]}")
        seen[e.sha256] = e.name
    names = [e.name for e in entries]
    problems += [f"missing asset: {n}" for n in expected_names if n not in names]
    problems += [f"duplicate name: {n}" for n in {n for n in names if names.count(n) > 1}]
    return problems


def manifest_dict(entries: list[ManifestEntry]) -> list[dict]:
    return [asdict(e) for e in entries]
