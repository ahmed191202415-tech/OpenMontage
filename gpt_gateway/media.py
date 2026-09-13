from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".mov", ".webm", ".mkv",
    ".mp3", ".wav", ".m4a", ".aac",
    ".pdf", ".txt", ".md", ".json", ".srt", ".vtt",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    name = _SAFE_NAME.sub("-", name).strip(".-") or "upload.bin"
    return name[:160]


def media_dir(project_dir: Path) -> Path:
    path = project_dir / "assets" / "input"
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(project_dir: Path) -> Path:
    return project_dir / "artifacts" / "gateway_media.json"


def _load_manifest(project_dir: Path) -> dict:
    path = manifest_path(project_dir)
    if not path.exists():
        return {"version": "1.0", "media": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "1.0", "media": []}


def _save_manifest(project_dir: Path, payload: dict) -> None:
    path = manifest_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save_upload(project_dir: Path, fileobj: BinaryIO, filename: str, content_type: str | None) -> dict:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext or 'no extension'}")

    max_bytes = int(os.environ.get("GPT_GATEWAY_MAX_UPLOAD_MB", "200")) * 1024 * 1024
    target_dir = media_dir(project_dir)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    target = target_dir / filename
    counter = 2
    while target.exists():
        target = target_dir / f"{stem}-{counter}{suffix}"
        counter += 1

    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"Upload exceeds {max_bytes // (1024 * 1024)} MB limit")
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    relative = target.relative_to(project_dir).as_posix()
    item = {
        "media_id": "media_" + digest.hexdigest()[:20],
        "filename": target.name,
        "path": str(target),
        "relative_path": relative,
        "content_type": content_type or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "created_at": utc_now(),
    }
    manifest = _load_manifest(project_dir)
    manifest.setdefault("media", []).append(item)
    _save_manifest(project_dir, manifest)
    return item


def list_media(project_dir: Path) -> list[dict]:
    return list(_load_manifest(project_dir).get("media", []))
