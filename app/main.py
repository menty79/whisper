import hashlib
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from av.error import InvalidDataError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("faster-whisper-http")

MODEL_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR", "/models")
DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "small")
DEFAULT_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
DEFAULT_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
DEFAULT_BEAM = int(os.getenv("WHISPER_BEAM", "5"))
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANG", "auto")
DEFAULT_VAD = os.getenv("WHISPER_VAD", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEFAULT_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "4"))
PRELOAD_MODEL = os.getenv("WHISPER_PRELOAD", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LOCAL_FILES_ONLY = os.getenv("WHISPER_LOCAL_FILES_ONLY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WORK_ROOT = Path(os.getenv("WHISPER_WORK_DIR", "/work/transcripts/by-video"))
PIPELINE_VERSION = os.getenv("WHISPER_PIPELINE_VERSION", "1")

_MODEL_LOCK = Lock()
_MODELS: dict[str, WhisperModel] = {}


class WhisperJobRequest(BaseModel):
    video_id: str = Field(..., min_length=3)
    model: str = DEFAULT_MODEL
    language_mode: str = "auto"
    language: str | None = None
    chunk_seconds: int = Field(150, ge=30, le=1800)
    retry_errors: bool = False


def _candidate_model_dirs(model_name: str) -> list[Path]:
    cache_root = Path(MODEL_CACHE_DIR)
    return [
        Path(model_name),
        cache_root / model_name,
        cache_root / f"models--Systran--faster-whisper-{model_name}" / "snapshots" / "main",
    ]


def _resolve_model_source(model_name: str) -> str:
    for candidate in _candidate_model_dirs(model_name):
        if candidate.is_dir() and (candidate / "model.bin").is_file():
            LOGGER.info("Using local model directory for %s: %s", model_name, candidate)
            return str(candidate)

    return model_name


def _parse_bool(value: str | None, fallback: bool) -> bool:
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalized_language(language: str | None) -> str | None:
    if not language:
        return None
    language = language.strip()
    if not language or language.lower() == "auto":
        return None
    return language


def _get_model(model_name: str) -> WhisperModel:
    with _MODEL_LOCK:
        model = _MODELS.get(model_name)
        if model is not None:
            return model

        model_source = _resolve_model_source(model_name)
        LOGGER.info(
            "Loading model name=%s source=%s device=%s compute_type=%s cpu_threads=%s local_files_only=%s",
            model_name,
            model_source,
            DEFAULT_DEVICE,
            DEFAULT_COMPUTE_TYPE,
            DEFAULT_CPU_THREADS,
            LOCAL_FILES_ONLY,
        )
        model = WhisperModel(
            model_source,
            device=DEFAULT_DEVICE,
            compute_type=DEFAULT_COMPUTE_TYPE,
            download_root=MODEL_CACHE_DIR,
            cpu_threads=DEFAULT_CPU_THREADS,
            local_files_only=LOCAL_FILES_ONLY,
        )
        _MODELS[model_name] = model
        return model


def _segment_payload(segment: Any) -> dict[str, Any]:
    return {
        "id": segment.id,
        "start": float(segment.start),
        "end": float(segment.end),
        "text": segment.text,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(
    video_id: str,
    model: str,
    language_mode: str,
    language: str | None,
    chunk_seconds: int,
) -> str:
    lang_tag = f"{language_mode}:{language or '-'}"
    blob = f"{video_id}|{model}|{lang_tag}|{chunk_seconds}|v{PIPELINE_VERSION}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _job_dir(job_id: str) -> Path:
    return WORK_ROOT / job_id


def _job_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def _public_job_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "heartbeat_at": job.get("heartbeat_at"),
    }
    if job.get("result") is not None:
        payload["result"] = job["result"]
    if job.get("error") is not None:
        payload["error"] = job["error"]
    return payload


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(MODEL_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if PRELOAD_MODEL:
        _get_model(DEFAULT_MODEL)
    yield


app = FastAPI(title="faster-whisper-http", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "loaded_models": sorted(_MODELS.keys()),
        "cache_dir": MODEL_CACHE_DIR,
        "work_dir": str(WORK_ROOT),
    }


@app.post("/jobs/whisper")
async def enqueue_whisper_job(request: WhisperJobRequest) -> dict[str, Any]:
    if request.language_mode not in {"auto", "fixed"}:
        raise HTTPException(
            status_code=400,
            detail="language_mode must be auto or fixed",
        )
    if request.language_mode == "fixed" and not _normalized_language(request.language):
        raise HTTPException(
            status_code=400,
            detail="language is required when language_mode is fixed",
        )

    language = _normalized_language(request.language)
    job_id = _fingerprint(
        request.video_id,
        request.model,
        request.language_mode,
        language,
        request.chunk_seconds,
    )
    path = _job_path(job_id)

    if path.exists():
        job = _read_json(path)
        error = job.get("error") or {}
        if (
            request.retry_errors
            and job.get("status") == "error"
            and error.get("recoverable", False)
        ):
            job["status"] = "queued"
            job["updated_at"] = _now_iso()
            job["queued_at"] = _now_iso()
            job["heartbeat_at"] = None
            job["error"] = None
            _atomic_write_json(path, job)
        return _public_job_payload(job)

    now = _now_iso()
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "queued_at": now,
        "heartbeat_at": None,
        "pipeline_version": PIPELINE_VERSION,
        "request": {
            "video_id": request.video_id,
            "model": request.model,
            "language_mode": request.language_mode,
            "language": language,
            "chunk_seconds": request.chunk_seconds,
        },
        "result": None,
        "error": None,
    }
    _atomic_write_json(path, job)
    return _public_job_payload(job)


@app.get("/jobs/{job_id}")
async def get_whisper_job(job_id: str) -> dict[str, Any]:
    path = _job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job_payload(_read_json(path))


@app.post("/audio")
async def transcribe_audio(
    audio: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE),
    task: str = Form("transcribe"),
    model: str = Form(DEFAULT_MODEL),
    beam_size: int = Form(DEFAULT_BEAM),
    vad_filter: str | None = Form(None),
    initial_prompt: str | None = Form(None),
) -> dict[str, Any]:
    if task not in {"transcribe", "translate"}:
        raise HTTPException(status_code=400, detail="task must be transcribe or translate")

    whisper_model = _get_model(model)
    suffix = Path(audio.filename or "audio.bin").suffix or ".bin"
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = Path(temp_file.name)
            while chunk := await audio.read(1024 * 1024):
                temp_file.write(chunk)

        LOGGER.info(
            "Starting transcription filename=%s size_bytes=%s task=%s language=%s model=%s beam=%s vad=%s",
            audio.filename,
            temp_path.stat().st_size,
            task,
            language,
            model,
            beam_size,
            _parse_bool(vad_filter, DEFAULT_VAD),
        )

        segments, info = whisper_model.transcribe(
            str(temp_path),
            language=_normalized_language(language),
            task=task,
            beam_size=beam_size,
            vad_filter=_parse_bool(vad_filter, DEFAULT_VAD),
            initial_prompt=initial_prompt,
        )

        segment_list = [_segment_payload(segment) for segment in segments]
        text = "".join(segment["text"] for segment in segment_list).strip()

        return {
            "text": text,
            "language": info.language,
            "language_probability": float(info.language_probability),
            "duration": float(info.duration),
            "duration_after_vad": (
                float(info.duration_after_vad)
                if info.duration_after_vad is not None
                else None
            ),
            "model": model,
            "task": task,
            "segments": segment_list,
        }
    except HTTPException:
        raise
    except InvalidDataError as exc:
        raise HTTPException(
            status_code=400,
            detail="uploaded file is not valid audio data",
        ) from exc
    except Exception as exc:
        LOGGER.exception("Transcription failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await audio.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
