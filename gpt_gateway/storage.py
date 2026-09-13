from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from gpt_gateway.security import make_render_url


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _s3_config() -> dict[str, str | None]:
    return {
        "bucket": os.environ.get("GPT_GATEWAY_S3_BUCKET"),
        "endpoint_url": os.environ.get("GPT_GATEWAY_S3_ENDPOINT_URL"),
        "region_name": os.environ.get("GPT_GATEWAY_S3_REGION", "auto"),
        "prefix": os.environ.get("GPT_GATEWAY_S3_PREFIX", "openmontage").strip("/"),
    }


def storage_mode() -> str:
    return "s3" if _s3_config()["bucket"] else "local"


def render_link(project_id: str, render_path: Path, ttl_seconds: int) -> dict[str, Any]:
    cfg = _s3_config()
    if not cfg["bucket"]:
        result = make_render_url(project_id, ttl_seconds)
        result["storage"] = "local"
        return result

    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required when GPT_GATEWAY_S3_BUCKET is configured") from exc

    client = boto3.client(
        "s3",
        endpoint_url=cfg["endpoint_url"] or None,
        region_name=cfg["region_name"] or None,
    )
    key = f"{cfg['prefix']}/{project_id}/final.mp4" if cfg["prefix"] else f"{project_id}/final.mp4"
    digest = _sha256(render_path)
    should_upload = True
    try:
        head = client.head_object(Bucket=cfg["bucket"], Key=key)
        metadata = head.get("Metadata", {}) or {}
        should_upload = metadata.get("sha256") != digest or int(head.get("ContentLength", -1)) != render_path.stat().st_size
    except Exception:
        should_upload = True

    if should_upload:
        client.upload_file(
            str(render_path),
            cfg["bucket"],
            key,
            ExtraArgs={
                "ContentType": "video/mp4",
                "Metadata": {"sha256": digest, "project_id": project_id},
            },
        )

    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": cfg["bucket"],
            "Key": key,
            "ResponseContentType": "video/mp4",
            "ResponseContentDisposition": f'attachment; filename="{project_id}.mp4"',
        },
        ExpiresIn=max(60, min(ttl_seconds, 86400)),
    )
    return {
        "url": url,
        "ttl_seconds": max(60, min(ttl_seconds, 86400)),
        "storage": "s3",
        "bucket": cfg["bucket"],
        "key": key,
        "sha256": digest,
        "uploaded": should_upload,
    }
