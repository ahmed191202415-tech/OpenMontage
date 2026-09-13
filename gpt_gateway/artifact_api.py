from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gpt_gateway.main import ROOT, require_api_key, require_project
from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS, get_next_stage, write_checkpoint
from lib.paths import PROJECTS_DIR
from lib.pipeline_loader import load_pipeline_readonly
from schemas.artifacts import ARTIFACT_NAMES, load_schema

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


class StageCheckpointRequest(BaseModel):
    status: str
    canonical_artifact_json: str | None = None
    additional_artifacts_json: str | None = None
    pipeline_type: str | None = None
    style_playbook: str | None = None
    checkpoint_policy: str = "guided"
    human_approval_required: bool = False
    human_approved: bool = False
    review_json: str | None = None
    cost_snapshot_json: str | None = None
    error: str | None = None
    metadata_json: str | None = None


def _parse_json_object(raw: str | None, field_name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{field_name} must contain valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, f"{field_name} must encode a JSON object")
    return value


def _stage_contract(pipeline_name: str, stage_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        manifest = load_pipeline_readonly(pipeline_name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    stage = next(
        (item for item in manifest.get("stages", []) if item.get("name") == stage_name),
        None,
    )
    if stage is None:
        raise HTTPException(404, f"Unknown stage {pipeline_name}/{stage_name}")
    return manifest, stage


def _canonical_artifact_name(stage_name: str, stage: dict[str, Any]) -> str | None:
    canonical = CANONICAL_STAGE_ARTIFACTS.get(stage_name)
    if canonical:
        return canonical
    produces = stage.get("produces", []) or []
    return produces[0] if produces else None


def _persist_artifacts(project_dir: Path, artifacts: dict[str, Any]) -> list[str]:
    written: list[str] = []
    artifact_dir = project_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    for name, data in artifacts.items():
        if name not in ARTIFACT_NAMES or not isinstance(data, dict):
            continue
        target = artifact_dir / f"{name}.json"
        temp = target.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp, target)
        written.append(str(target))
    return written


@router.get("/pipelines/{pipeline_name}/stages/{stage_name}/artifact-contract")
def stage_artifact_contract(pipeline_name: str, stage_name: str) -> dict[str, Any]:
    _, stage = _stage_contract(pipeline_name, stage_name)
    produces = list(stage.get("produces", []) or [])
    canonical = _canonical_artifact_name(stage_name, stage)

    schemas: dict[str, Any] = {}
    for artifact_name in produces:
        try:
            schemas[artifact_name] = load_schema(artifact_name)
        except FileNotFoundError:
            continue

    if canonical and canonical not in schemas:
        try:
            schemas[canonical] = load_schema(canonical)
        except FileNotFoundError:
            pass

    return {
        "pipeline": pipeline_name,
        "stage": stage_name,
        "canonical_artifact_name": canonical,
        "produces": produces,
        "artifact_schemas": schemas,
        "checkpoint_required": bool(stage.get("checkpoint_required", False)),
        "human_approval_default": bool(stage.get("human_approval_default", False)),
        "submission": {
            "action": "writeStageCheckpoint",
            "canonical_artifact_field": "canonical_artifact_json",
            "format": "JSON-encoded object string",
            "additional_artifacts_field": "additional_artifacts_json",
        },
    }


@router.post("/projects/{project_id}/stages/{stage_name}/checkpoint")
def write_stage_checkpoint(
    project_id: str,
    stage_name: str,
    req: StageCheckpointRequest,
) -> dict[str, Any]:
    project_dir = require_project(project_id)
    marker = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    pipeline_type = req.pipeline_type or marker.get("pipeline_type")
    if not pipeline_type:
        raise HTTPException(400, "Project has no pipeline_type")

    _, stage = _stage_contract(pipeline_type, stage_name)
    canonical_name = _canonical_artifact_name(stage_name, stage)

    artifacts = _parse_json_object(req.additional_artifacts_json, "additional_artifacts_json") or {}
    canonical_artifact = _parse_json_object(req.canonical_artifact_json, "canonical_artifact_json")

    if canonical_artifact is not None:
        if not canonical_name:
            raise HTTPException(
                400,
                f"Stage {stage_name!r} does not declare a canonical artifact",
            )
        if canonical_name in artifacts and artifacts[canonical_name] != canonical_artifact:
            raise HTTPException(
                400,
                f"Canonical artifact {canonical_name!r} was supplied twice with different values",
            )
        artifacts[canonical_name] = canonical_artifact

    if req.status in {"completed", "awaiting_human"} and canonical_name and canonical_name not in artifacts:
        raise HTTPException(
            400,
            f"Stage {stage_name!r} with status {req.status!r} requires canonical artifact "
            f"{canonical_name!r}. Send it as canonical_artifact_json.",
        )

    review = _parse_json_object(req.review_json, "review_json")
    cost_snapshot = _parse_json_object(req.cost_snapshot_json, "cost_snapshot_json")
    metadata = _parse_json_object(req.metadata_json, "metadata_json")

    try:
        checkpoint_path = write_checkpoint(
            PROJECTS_DIR,
            project_id,
            stage_name,
            req.status,
            artifacts,
            pipeline_type=pipeline_type,
            style_playbook=req.style_playbook,
            checkpoint_policy=req.checkpoint_policy,
            human_approval_required=req.human_approval_required,
            human_approved=req.human_approved,
            review=review,
            cost_snapshot=cost_snapshot,
            error=req.error,
            metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    artifact_paths = _persist_artifacts(project_dir, artifacts)
    return {
        "checkpoint_path": str(checkpoint_path),
        "stage": stage_name,
        "status": req.status,
        "canonical_artifact_name": canonical_name,
        "artifact_paths": artifact_paths,
        "next_stage": get_next_stage(PROJECTS_DIR, project_id, pipeline_type),
    }
