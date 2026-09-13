from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class JobError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class JobManager:
    """Small persistent async job runner for long OpenMontage tool calls."""

    def __init__(self, projects_dir: Path, serializer: Callable[[Any], Any]) -> None:
        self.projects_dir = projects_dir
        self.serializer = serializer
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(os.environ.get("GPT_GATEWAY_MAX_WORKERS", "2"))),
            thread_name_prefix="openmontage-gpt-job",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    def _project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def _jobs_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id) / ".gpt_gateway" / "jobs"

    def _job_path(self, project_id: str, job_id: str) -> Path:
        if not job_id.startswith("job_") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in job_id):
            raise JobError("Invalid job_id")
        return self._jobs_dir(project_id) / f"{job_id}.json"

    def create_tool_job(self, *, project_id: str, tool_name: str, inputs: dict[str, Any], allow_paid: bool, allow_side_effects: bool) -> dict[str, Any]:
        from tools.tool_registry import registry

        registry.ensure_discovered()
        tool = registry.get(tool_name)
        if tool is None:
            raise JobError(f"Unknown tool: {tool_name}")

        project_dir = self._project_dir(project_id)
        if not (project_dir / "project.json").exists():
            raise JobError("Project not found")

        resolved_inputs = dict(inputs)
        resolved_inputs.setdefault("project_dir", str(project_dir))
        dry = tool.dry_run(resolved_inputs)
        estimated_cost = float(dry.get("estimated_cost_usd", 0.0) or 0.0)
        side_effects = list(getattr(tool, "side_effects", []) or [])

        if estimated_cost > 0 and not allow_paid:
            raise JobError(json.dumps({"approval_required": "paid_tool", "estimated_cost_usd": estimated_cost, "message": "Ask the user for approval, then retry with allow_paid=true."}))
        if side_effects and not allow_side_effects:
            raise JobError(json.dumps({"approval_required": "side_effects", "side_effects": side_effects, "message": "Ask the user for approval, then retry with allow_side_effects=true."}))

        job_id = "job_" + uuid.uuid4().hex
        record: dict[str, Any] = {
            "job_id": job_id,
            "project_id": project_id,
            "kind": "tool",
            "tool_name": tool_name,
            "status": "queued",
            "progress": 0,
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "estimated_cost_usd": estimated_cost,
            "side_effects": side_effects,
            "result": None,
            "error": None,
        }
        _atomic_json_write(self._job_path(project_id, job_id), record)
        future = self.executor.submit(self._run_tool_job, project_id, job_id, tool_name, resolved_inputs)
        with self._lock:
            self._futures[job_id] = future
        future.add_done_callback(lambda _f, jid=job_id: self._forget(jid))
        return record

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def _run_tool_job(self, project_id: str, job_id: str, tool_name: str, inputs: dict[str, Any]) -> None:
        from tools.tool_registry import registry

        record = self.get(project_id, job_id, normalize_interrupted=False)
        record.update(status="running", progress=10, started_at=utc_now())
        _atomic_json_write(self._job_path(project_id, job_id), record)
        try:
            registry.ensure_discovered()
            tool = registry.get(tool_name)
            if tool is None:
                raise JobError(f"Unknown tool: {tool_name}")
            result = tool.execute(inputs)
            record.update(status="succeeded", progress=100, finished_at=utc_now(), result=self.serializer(result), error=None)
        except Exception as exc:
            record.update(status="failed", progress=100, finished_at=utc_now(), error=str(exc)[:4000])
        _atomic_json_write(self._job_path(project_id, job_id), record)

    def get(self, project_id: str, job_id: str, *, normalize_interrupted: bool = True) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        if not path.exists():
            raise JobError("Job not found")
        record = json.loads(path.read_text(encoding="utf-8"))
        if normalize_interrupted and record.get("status") == "running":
            with self._lock:
                alive = job_id in self._futures and not self._futures[job_id].done()
            if not alive:
                record.update(status="interrupted", finished_at=record.get("finished_at") or utc_now(), error=record.get("error") or "Gateway process restarted while this job was running. Retry the tool call.")
                _atomic_json_write(path, record)
        return record

    def list(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        paths = sorted(self._jobs_dir(project_id).glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, min(limit, 200))]
        return [self.get(project_id, p.stem) for p in paths]

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        record = self.get(project_id, job_id)
        if record.get("status") not in {"queued", "running"}:
            return record
        with self._lock:
            future = self._futures.get(job_id)
            cancelled = bool(future and future.cancel())
        if cancelled:
            record.update(status="cancelled", progress=100, finished_at=utc_now())
        else:
            record["cancel_requested"] = True
            record["cancel_note"] = "The underlying tool is already running and does not expose cooperative cancellation."
        _atomic_json_write(self._job_path(project_id, job_id), record)
        return record
