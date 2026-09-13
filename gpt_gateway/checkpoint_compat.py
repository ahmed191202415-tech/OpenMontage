from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, HTTPException

from gpt_gateway.artifact_api import _persist_artifacts
from gpt_gateway.main import CheckpointRequest, require_api_key, require_project
from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS, get_next_stage, write_checkpoint
from lib.paths import PROJECTS_DIR

CHECKPOINT_PATH = "/api/v1/projects/{project_id}/checkpoint"


def _recover(stage_name: str, metadata: dict[str, Any] | None):
    canonical_name = CANONICAL_STAGE_ARTIFACTS.get(stage_name)
    if not canonical_name or not isinstance(metadata, dict):
        return canonical_name, None

    direct = metadata.get(canonical_name)
    if isinstance(direct, dict):
        return canonical_name, direct

    wrapped = metadata.get("canonical_artifact")
    if isinstance(wrapped, dict):
        nested = wrapped.get(canonical_name)
        if isinstance(nested, dict):
            return canonical_name, nested
        data = wrapped.get("data")
        declared = wrapped.get("name") or wrapped.get("artifact_name")
        if isinstance(data, dict) and declared in (None, canonical_name):
            return canonical_name, data
        if not ({"name", "artifact_name", "data"} & set(wrapped)):
            return canonical_name, wrapped

    if metadata.get("artifact_name") == canonical_name and isinstance(metadata.get("artifact"), dict):
        return canonical_name, metadata["artifact"]

    return canonical_name, None


def install_checkpoint_compat(app) -> None:
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == CHECKPOINT_PATH
            and "POST" in (getattr(route, "methods", set()) or set())
        )
    ]

    def compatible_checkpoint(project_id: str, req: CheckpointRequest) -> dict[str, Any]:
        project_dir = require_project(project_id)
        marker = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        pipeline_type = req.pipeline_type or marker.get("pipeline_type")

        artifacts = dict(req.artifacts)
        canonical_name, recovered = _recover(req.stage, req.metadata)
        recovered_from_metadata = False
        if canonical_name and canonical_name not in artifacts and isinstance(recovered, dict):
            artifacts[canonical_name] = recovered
            recovered_from_metadata = True

        try:
            path = write_checkpoint(
                PROJECTS_DIR,
                project_id,
                req.stage,
                req.status,
                artifacts,
                pipeline_type=pipeline_type,
                style_playbook=req.style_playbook,
                checkpoint_policy=req.checkpoint_policy,
                human_approval_required=req.human_approval_required,
                human_approved=req.human_approved,
                review=req.review,
                cost_snapshot=req.cost_snapshot,
                error=req.error,
                metadata=req.metadata,
            )
        except Exception as exc:
            hint = ""
            if canonical_name and canonical_name not in artifacts:
                hint = (
                    f" If the Action cannot populate artifacts, place the {canonical_name!r} "
                    "object in metadata under canonical_artifact or under its canonical name."
                )
            raise HTTPException(400, f"{exc}{hint}") from exc

        artifact_paths = _persist_artifacts(project_dir, artifacts)
        return {
            "checkpoint_path": str(path),
            "next_stage": get_next_stage(PROJECTS_DIR, project_id, pipeline_type),
            "canonical_artifact_name": canonical_name,
            "canonical_artifact_recovered_from_metadata": recovered_from_metadata,
            "artifact_paths": artifact_paths,
        }

    app.add_api_route(
        CHECKPOINT_PATH,
        compatible_checkpoint,
        methods=["POST"],
        dependencies=[Depends(require_api_key)],
        name="compatible_checkpoint",
    )
