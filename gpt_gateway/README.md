# OpenMontage Custom GPT Gateway

This folder exposes OpenMontage through an authenticated FastAPI service so a Custom GPT can act as the creative/video-production agent while OpenMontage remains the execution engine.

## Architecture

`Custom GPT -> GPT Actions -> GPT Gateway -> OpenMontage pipelines/tools/checkpoints -> async workers -> Remotion/HyperFrames/AI providers -> MP4`

The gateway preserves OpenMontage's agent-first model. The GPT reads the native agent contract, selects a pipeline, reads each stage skill, checks real provider availability, runs tools, and writes native checkpoints.

## Milestone 2 capabilities

- Persistent async tool jobs for long renders/generation calls.
- Job status, recent-job listing and best-effort cancellation.
- Project media upload/listing under `assets/input` with size/type validation and SHA-256 metadata.
- Incremental project event retrieval from OpenMontage/Backlot `events.jsonl`.
- Expiring render links.
- Optional S3-compatible render publishing (AWS S3, Cloudflare R2, MinIO, etc.) with presigned URLs.
- Local signed delivery fallback when object storage is not configured.

## Run locally

From the repository root:

```bash
python -m pip install -r requirements.txt
python -m pip install -r gpt_gateway/requirements.txt
export GPT_GATEWAY_API_KEY='replace-with-a-long-random-secret'
export GPT_GATEWAY_SIGNING_SECRET='another-long-random-secret'
export GPT_GATEWAY_PUBLIC_BASE_URL='https://your-domain.example.com'
uvicorn gpt_gateway.app:app --host 0.0.0.0 --port 8000
```

Windows PowerShell:

```powershell
python -m pip install -r requirements.txt
python -m pip install -r gpt_gateway/requirements.txt
$env:GPT_GATEWAY_API_KEY='replace-with-a-long-random-secret'
$env:GPT_GATEWAY_SIGNING_SECRET='another-long-random-secret'
$env:GPT_GATEWAY_PUBLIC_BASE_URL='https://your-domain.example.com'
uvicorn gpt_gateway.app:app --host 0.0.0.0 --port 8000
```

Open `/health` to confirm the gateway is running.

## Custom GPT setup

1. Deploy this repository behind HTTPS.
2. Replace the placeholder server URL in `openapi.yaml` with the deployment URL.
3. Add `openapi.yaml` as the GPT Action schema.
4. Configure API-key authentication with the `X-API-Key` header.
5. Put `GPT_INSTRUCTIONS.md` into the GPT instructions.

For file-heavy workflows, the gateway provides a normal multipart media endpoint. Exact attachment handoff behavior depends on the ChatGPT Action client; the pipeline itself always receives stable project-local media paths after upload.

## Long-running workflow

For expensive/slow tools the GPT should:

1. `dryRunTool`.
2. Ask for explicit approval if cost or side effects require it.
3. `submitToolJob`.
4. Poll `getJobStatus` rather than keeping an HTTP request open.
5. Optionally read `getProjectEvents` for activity details.
6. Write the stage checkpoint after the job succeeds.
7. At the end call `getFinalRenderLink`.

Job records are stored under each project in `.gpt_gateway/jobs`, so completed state survives normal process restarts. A tool that was actively running during a hard restart is marked `interrupted` and should be retried.

## Render storage

Default mode is local disk with an HMAC-signed expiring download URL. Configure `GPT_GATEWAY_PUBLIC_BASE_URL` and `GPT_GATEWAY_SIGNING_SECRET`.

For persistent object storage, set `GPT_GATEWAY_S3_BUCKET` plus standard AWS credentials. `GPT_GATEWAY_S3_ENDPOINT_URL` supports S3-compatible providers such as Cloudflare R2 or MinIO. `getFinalRenderLink` uploads `renders/final.mp4` when needed and returns a presigned URL.

## Production deployment

`Dockerfile` includes FFmpeg, Node/npm, the OpenMontage Python requirements, gateway requirements and Remotion dependencies. Mount the repository `projects/` directory on persistent disk unless S3 output storage is enabled. For true multi-instance/high-throughput production, replace the built-in process-local executor with an external queue such as Redis/Celery or a managed job service; the API/job contract is already separated so that migration does not change the GPT Action surface.
