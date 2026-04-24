import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.main import (
    DEFAULT_BEAM,
    DEFAULT_VAD,
    WORK_ROOT,
    _get_model,
    _normalized_language,
    _segment_payload,
)


LOGGER = logging.getLogger("faster-whisper-worker")

HEARTBEAT_S = int(os.getenv("WHISPER_WORKER_HEARTBEAT_S", "60"))
STALE_AFTER_S = int(os.getenv("WHISPER_WORKER_STALE_AFTER_S", "300"))
POLL_IDLE_S = int(os.getenv("WHISPER_WORKER_POLL_IDLE_S", "5"))
MAX_GAP_S = int(os.getenv("WHISPER_WORKER_MAX_GAP_S", "300"))
STOP = threading.Event()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def job_path(fp_dir: Path) -> Path:
    return fp_dir / "job.json"


def update_job(fp_dir: Path, **updates: Any) -> dict[str, Any]:
    path = job_path(fp_dir)
    job = read_json(path)
    job.update(updates)
    job["updated_at"] = now_iso()
    atomic_write_json(path, job)
    return job


def heartbeat(fp_dir: Path) -> None:
    try:
        update_job(fp_dir, heartbeat_at=time.time())
    except FileNotFoundError:
        LOGGER.warning("heartbeat skipped because job disappeared: %s", fp_dir)


class HeartbeatThread:
    def __init__(self, fp_dir: Path):
        self.fp_dir = fp_dir
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "HeartbeatThread":
        heartbeat(self.fp_dir)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            heartbeat(self.fp_dir)


def release(fp_dir: Path) -> None:
    lease = fp_dir / "lease.lock"
    try:
        lease.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        LOGGER.warning("could not release lease dir: %s", lease)


def startup_reconcile() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    for path in WORK_ROOT.glob("*/job.json"):
        try:
            job = read_json(path)
        except Exception:
            LOGGER.exception("could not read job during reconcile: %s", path)
            continue
        if job.get("status") == "queued":
            release(path.parent)
            continue
        if job.get("status") != "running":
            continue
        age = time.time() - float(job.get("heartbeat_at") or 0)
        if age <= STALE_AFTER_S:
            continue
        fp_dir = path.parent
        job["status"] = "queued"
        job["updated_at"] = now_iso()
        job["last_recovery_at"] = now_iso()
        job["heartbeat_at"] = None
        atomic_write_json(path, job)
        release(fp_dir)
        LOGGER.info("requeued stale running job job_id=%s age=%.1fs", job["job_id"], age)


def claim(fp_dir: Path) -> bool:
    try:
        (fp_dir / "lease.lock").mkdir()
        return True
    except FileExistsError:
        return False


def find_next_queued() -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for path in WORK_ROOT.glob("*/job.json"):
        try:
            job = read_json(path)
        except Exception:
            LOGGER.exception("could not read queued candidate: %s", path)
            continue
        if job.get("status") == "queued":
            candidates.append(job)
    if not candidates:
        return None
    candidates.sort(key=lambda j: j.get("created_at") or "")
    return candidates[0]


def run_command(args: list[str], cwd: Path) -> None:
    LOGGER.info("running command: %s", " ".join(args))
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed exit={completed.returncode}: {completed.stderr[-1000:]}"
        )


def download_audio(job: dict[str, Any], fp_dir: Path) -> Path:
    audio = fp_dir / "audio.m4a"
    if audio.exists() and audio.stat().st_size > 0:
        return audio

    request = job["request"]
    video_id = request["video_id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    run_command(
        [
            "yt-dlp",
            "--no-progress",
            "--match-filters",
            "live_status!=is_live",
            "-f",
            "ba",
            "-x",
            "--audio-format",
            "m4a",
            "--audio-quality",
            "0",
            "-o",
            "audio.%(ext)s",
            url,
        ],
        fp_dir,
    )
    if not audio.exists():
        matches = sorted(fp_dir.glob("audio.*"))
        if matches:
            matches[0].replace(audio)
    if not audio.exists() or audio.stat().st_size == 0:
        raise RuntimeError("yt-dlp did not produce audio.m4a")
    return audio


def segment_audio(job: dict[str, Any], fp_dir: Path, audio: Path) -> list[Path]:
    chunks_dir = fp_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(chunks_dir.glob("*.m4a"))
    if existing:
        return existing

    chunk_seconds = int(job["request"]["chunk_seconds"])
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(chunks_dir / "%04d.m4a"),
        ],
        fp_dir,
    )
    chunks = sorted(chunks_dir.glob("*.m4a"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce chunk files")
    return chunks


def transcribe_chunks(job: dict[str, Any], fp_dir: Path, chunks: list[Path]) -> None:
    request = job["request"]
    model_name = request["model"]
    language = (
        _normalized_language(request.get("language"))
        if request.get("language_mode") == "fixed"
        else None
    )
    model = _get_model(model_name)
    chunk_seconds = int(request["chunk_seconds"])

    for chunk in chunks:
        if STOP.is_set():
            raise RuntimeError("worker stopping")
        idx = int(chunk.stem)
        out = chunk.with_suffix(".json")
        if out.exists():
            try:
                existing = read_json(out)
                if existing.get("status") == "ok":
                    continue
            except Exception:
                LOGGER.warning("will overwrite unreadable chunk output: %s", out)

        LOGGER.info("transcribing job_id=%s chunk=%s", job["job_id"], chunk.name)
        try:
            with HeartbeatThread(fp_dir):
                segments, info = model.transcribe(
                    str(chunk),
                    language=language,
                    beam_size=DEFAULT_BEAM,
                    vad_filter=DEFAULT_VAD,
                )
                segment_list = [_segment_payload(segment) for segment in segments]
            text = "".join(segment["text"] for segment in segment_list).strip()
            payload = {
                "idx": idx,
                "status": "ok",
                "start_s": idx * chunk_seconds,
                "end_s": idx * chunk_seconds + float(info.duration),
                "duration_s": float(info.duration),
                "language": info.language,
                "language_probability": float(info.language_probability),
                "text": text,
                "segments": segment_list,
            }
        except Exception as exc:
            LOGGER.exception("chunk transcription failed job_id=%s chunk=%s", job["job_id"], chunk)
            payload = {
                "idx": idx,
                "status": "failed",
                "start_s": idx * chunk_seconds,
                "end_s": (idx + 1) * chunk_seconds,
                "error": str(exc)[:500],
            }
        atomic_write_json(out, payload)
        heartbeat(fp_dir)


def load_chunk_results(chunks: list[Path], chunk_seconds: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for chunk in chunks:
        idx = int(chunk.stem)
        out = chunk.with_suffix(".json")
        if out.exists():
            try:
                payload = read_json(out)
            except Exception as exc:
                payload = {"idx": idx, "status": "failed", "error": str(exc)}
        else:
            payload = {"idx": idx, "status": "failed", "error": "missing_transcript"}
        payload.setdefault("idx", idx)
        payload.setdefault("start_s", idx * chunk_seconds)
        payload.setdefault("end_s", (idx + 1) * chunk_seconds)
        results.append(payload)
    results.sort(key=lambda item: int(item["idx"]))
    return results


def evaluate_coverage(chunks: list[Path], job: dict[str, Any]) -> dict[str, Any]:
    chunk_seconds = int(job["request"]["chunk_seconds"])
    results = load_chunk_results(chunks, chunk_seconds)
    if not results:
        return {"accept": False, "reason": "no_chunks", "all_ok": False}

    ok = [r for r in results if r.get("status") == "ok"]
    failed = [r for r in results if r.get("status") != "ok"]
    first_ok = results[0].get("status") == "ok"
    last_ok = results[-1].get("status") == "ok"

    max_gap_s = 0
    current_gap = 0
    for result in results:
        if result.get("status") == "ok":
            max_gap_s = max(max_gap_s, current_gap * chunk_seconds)
            current_gap = 0
        else:
            current_gap += 1
    max_gap_s = max(max_gap_s, current_gap * chunk_seconds)

    languages: dict[str, int] = {}
    for result in ok:
        language = result.get("language")
        if language:
            languages[language] = languages.get(language, 0) + 1
    language = max(languages.items(), key=lambda item: item[1])[0] if languages else None

    accept = first_ok and last_ok and max_gap_s <= MAX_GAP_S
    return {
        "accept": accept,
        "all_ok": len(failed) == 0,
        "first_chunk_ok": first_ok,
        "last_chunk_ok": last_ok,
        "max_gap_s": max_gap_s,
        "ok_chunks": len(ok),
        "failed_chunks": len(failed),
        "total_chunks": len(results),
        "language": language,
    }


def write_full_txt(fp_dir: Path, chunks: list[Path], job: dict[str, Any]) -> int:
    chunk_seconds = int(job["request"]["chunk_seconds"])
    results = load_chunk_results(chunks, chunk_seconds)
    texts = [r.get("text", "").strip() for r in results if r.get("status") == "ok"]
    text = "\n\n".join(t for t in texts if t)
    full = fp_dir / "full.txt"
    tmp = fp_dir / "full.txt.tmp"
    tmp.write_text(text + ("\n" if text else ""), encoding="utf-8")
    tmp.replace(full)
    return full.stat().st_size


def mark_error(
    fp_dir: Path,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    recoverable: bool = True,
) -> None:
    update_job(
        fp_dir,
        status="error",
        heartbeat_at=None,
        error={
            "code": code,
            "message": message[:500],
            "detail": detail,
            "recoverable": recoverable,
        },
    )


def process(job: dict[str, Any], fp_dir: Path) -> None:
    LOGGER.info("starting job job_id=%s", job["job_id"])
    update_job(fp_dir, status="running", started_at=now_iso(), heartbeat_at=time.time())
    audio = download_audio(job, fp_dir)
    heartbeat(fp_dir)
    chunks = segment_audio(job, fp_dir, audio)
    heartbeat(fp_dir)
    transcribe_chunks(job, fp_dir, chunks)
    coverage = evaluate_coverage(chunks, job)
    if not coverage["accept"]:
        mark_error(
            fp_dir,
            "whisper_coverage_insufficient",
            "coverage policy rejected transcript",
            coverage,
            recoverable=True,
        )
        return

    char_count = write_full_txt(fp_dir, chunks, job)
    source = "whisper-large-v3" if coverage["all_ok"] else "whisper-large-v3-partial"
    result = {
        "path": str(fp_dir / "full.txt"),
        "char_count": char_count,
        "source": source,
        "language": coverage.get("language") or "auto",
        "coverage": coverage,
    }
    update_job(
        fp_dir,
        status="done",
        completed_at=now_iso(),
        heartbeat_at=None,
        result=result,
        error=None,
    )
    LOGGER.info("completed job job_id=%s source=%s", job["job_id"], source)


def handle_signal(signum: int, _: object) -> None:
    LOGGER.info("received signal %s, stopping worker", signum)
    STOP.set()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    startup_reconcile()

    while not STOP.is_set():
        job = find_next_queued()
        if job is None:
            STOP.wait(POLL_IDLE_S)
            continue

        fp_dir = WORK_ROOT / job["job_id"]
        if not claim(fp_dir):
            continue
        try:
            process(job, fp_dir)
        except Exception as exc:
            LOGGER.exception("job failed job_id=%s", job.get("job_id"))
            mark_error(fp_dir, "worker_exception", str(exc), recoverable=True)
        finally:
            release(fp_dir)


if __name__ == "__main__":
    main()
