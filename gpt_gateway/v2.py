from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from lib.paths import PROJECTS_DIR
from gpt_gateway.job_manager import JobError, JobManager
from gpt_gateway.media import list_media, save_upload
from gpt_gateway.security import verify_render
from gpt_gateway.storage import render_link as create_render_link, storage_mode

router = APIRouter()
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")


class AsyncToolRequest(BaseModel):
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    inputs: dict[str, Any] = Field(default_factory=dict)
    allow_paid: bool = False
    allow_side_effects: bool = False


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("GPT_GATEWAY_API_KEY")
    if not expected:
        raise HTTPException(500, "GPT_GATEWAY_API_KEY is not configured on the server")
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Invalid API key")


def ensure_project_id(project_id: str) -> str:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(400, "Invalid project_id")
    return project_id


def require_project(project_id: str) -> Path:
    ensure_project_id(project_id)
    base = PROJECTS_DIR.resolve()
    path = (PROJECTS_DIR / project_id).resolve()
    if base not in path.parents or not (path / "project.json").exists():
        raise HTTPException(404, "Project not found")
    return path


def serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    return value


jobs = JobManager(PROJECTS_DIR, serialize)


@router.get("/api/v1/gateway/features", dependencies=[Depends(require_api_key)])
def gateway_features() -> dict[str, Any]:
    return {
        "version": "0.2.0",
        "async_jobs": True,
        "media_uploads": True,
        "render_storage": storage_mode(),
        "signed_render_links": bool(os.environ.get("GPT_GATEWAY_SIGNING_SECRET")) or storage_mode() == "s3",
    }


@router.post("/api/v1/projects/{project_id}/media", dependencies=[Depends(require_api_key)])
def upload_media(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    pdir = require_project(project_id)
    try:
        item = save_upload(pdir, file.file, file.filename or "upload.bin", file.content_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        file.file.close()
    return {"media": item}


@router.get("/api/v1/projects/{project_id}/media", dependencies=[Depends(require_api_key)])
def media_list(project_id: str) -> dict[str, Any]:
    return {"media": list_media(require_project(project_id))}


@router.post("/api/v1/tools/{tool_name}/jobs", dependencies=[Depends(require_api_key)])
def submit_tool_job(tool_name: str, req: AsyncToolRequest) -> dict[str, Any]:
    require_project(req.project_id)
    try:
        return jobs.create_tool_job(
            project_id=req.project_id,
            tool_name=tool_name,
            inputs=req.inputs,
            allow_paid=req.allow_paid,
            allow_side_effects=req.allow_side_effects,
        )
    except JobError as exc:
        message = str(exc)
        try:
            detail = json.loads(message)
        except json.JSONDecodeError:
            raise HTTPException(400, message) from exc
        raise HTTPException(409, detail=detail) from exc


@router.get("/api/v1/projects/{project_id}/jobs", dependencies=[Depends(require_api_key)])
def list_jobs(project_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    require_project(project_id)
    return {"jobs": jobs.list(project_id, limit=limit)}


@router.get("/api/v1/projects/{project_id}/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def job_status(project_id: str, job_id: str) -> dict[str, Any]:
    require_project(project_id)
    try:
        return jobs.get(project_id, job_id)
    except JobError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/v1/projects/{project_id}/jobs/{job_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_job(project_id: str, job_id: str) -> dict[str, Any]:
    require_project(project_id)
    try:
        return jobs.cancel(project_id, job_id)
    except JobError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/v1/projects/{project_id}/events", dependencies=[Depends(require_api_key)])
def project_events(project_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    pdir = require_project(project_id)
    path = pdir / "events.jsonl"
    if not path.exists():
        return {"events": [], "next_after": after, "total": 0}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[after : after + limit]
    events: list[Any] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line})
    return {"events": events, "next_after": after + len(selected), "total": len(lines)}


@router.get("/api/v1/projects/{project_id}/render-link", dependencies=[Depends(require_api_key)])
def render_link(project_id: str, ttl_seconds: int = Query(default=900, ge=60, le=86400)) -> dict[str, Any]:
    path = require_project(project_id) / "renders" / "final.mp4"
    if not path.exists():
        raise HTTPException(404, "Final render is not ready")
    try:
        return create_render_link(project_id, path, ttl_seconds)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/api/v1/public/renders/{project_id}")
def public_render(project_id: str, expires: int, sig: str):
    ensure_project_id(project_id)
    try:
        valid = verify_render(project_id, expires, sig)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not valid:
        raise HTTPException(403, "Invalid or expired render link")
    path = require_project(project_id) / "renders" / "final.mp4"
    if not path.exists():
        raise HTTPException(404, "Final render is not ready")
    return FileResponse(path, media_type="video/mp4", filename=f"{project_id}.mp4")
