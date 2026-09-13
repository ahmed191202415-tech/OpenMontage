from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, HTTPException

from gpt_gateway.main import (
    ROOT,
    app,
    get_stage_skill,
    load_pipeline_readonly,
    read_text,
    require_api_key,
)
from gpt_gateway.v2 import router as gateway_v2_router

app.include_router(gateway_v2_router)


_STAGE_INSTRUCTIONS_PATH = "/api/v1/pipelines/{pipeline_name}/stages/{stage_name}/instructions"

# The original v1 handler treated the manifest skill id as a literal repo path.
# OpenMontage manifests use logical ids such as
# `pipelines/animation/research-director`, while the files live under
# `skills/pipelines/animation/research-director.md`. Remove the old route and
# register a resolver-aware replacement so Custom GPTs can read stage skills.
app.router.routes = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == _STAGE_INSTRUCTIONS_PATH
        and "GET" in (getattr(route, "methods", set()) or set())
    )
]


def _resolve_stage_skill(skill: str) -> Path | None:
    relative = Path(skill)
    if relative.is_absolute():
        return None

    candidates: list[Path] = []
    for base in (ROOT, ROOT / "skills"):
        candidate = base / relative
        candidates.append(candidate)
        if candidate.suffix == "":
            candidates.append(candidate.with_suffix(".md"))

    root = ROOT.resolve()
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if root != resolved and root not in resolved.parents:
            continue
        if resolved.is_file():
            return resolved
    return None


def _manifest_stage_fallback(pipeline_name: str, stage_name: str, stage: dict) -> str:
    return (
        "No dedicated stage skill file could be resolved. Continue using the pipeline "
        "manifest as the authoritative stage contract; do not skip the stage. Respect "
        "its declared inputs, outputs, tools, checkpoints, approval gates, review focus, "
        "and success criteria. Use the Custom GPT as the reasoning/directing agent and "
        "OpenMontage tools only for execution.\n\n"
        f"Pipeline: {pipeline_name}\nStage: {stage_name}\n"
        "Stage contract:\n"
        + json.dumps(stage, ensure_ascii=False, indent=2)
    )


@app.get(
    _STAGE_INSTRUCTIONS_PATH,
    dependencies=[Depends(require_api_key)],
)
def resolved_stage_instructions(pipeline_name: str, stage_name: str) -> dict:
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

    skill = get_stage_skill(manifest, stage_name)
    if skill:
        skill_path = _resolve_stage_skill(skill)
        if skill_path is not None:
            return {
                "pipeline": pipeline_name,
                "stage": stage_name,
                "skill": skill,
                "skill_path": str(skill_path.relative_to(ROOT)),
                "instruction_source": "skill_file",
                "instructions": read_text(skill_path),
            }

    return {
        "pipeline": pipeline_name,
        "stage": stage_name,
        "skill": skill,
        "skill_path": None,
        "instruction_source": "manifest_fallback",
        "warning": (
            f"Stage skill {skill!r} could not be resolved; using the manifest stage contract."
            if skill
            else "No stage skill is declared; using the manifest stage contract."
        ),
        "instructions": _manifest_stage_fallback(pipeline_name, stage_name, stage),
    }
