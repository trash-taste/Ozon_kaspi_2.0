import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class ParserJobQueue:
    """Simple filesystem-backed queue for bot/worker process separation."""

    ACTIVE_STATUSES = {"pending", "running"}

    def __init__(self, base_dir: Path | str | None = None):
        root = Path(base_dir or os.getenv("PARSER_RUNTIME_DIR", "runtime"))
        self.base_dir = root.resolve()
        self.pending_dir = self.base_dir / "jobs" / "pending"
        self.running_dir = self.base_dir / "jobs" / "running"
        self.done_dir = self.base_dir / "jobs" / "done"
        self.failed_dir = self.base_dir / "jobs" / "failed"
        self.cancel_dir = self.base_dir / "cancel"
        self._ensure_dirs()

    def _ensure_dirs(self):
        for path in (
            self.pending_dir,
            self.running_dir,
            self.done_dir,
            self.failed_dir,
            self.cancel_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        url: str,
        selected_fields: list[str] | None,
        user_id: str | None,
        max_products: int,
    ) -> dict[str, Any]:
        job_id = f"{int(time.time())}_{uuid.uuid4().hex[:10]}"
        job = {
            "job_id": job_id,
            "status": "pending",
            "url": url,
            "selected_fields": selected_fields or [],
            "user_id": str(user_id or "unknown"),
            "max_products": int(max_products),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write_json_atomic(self.pending_dir / f"{job_id}.json", job)
        return job

    def take_next(self) -> dict[str, Any] | None:
        for path in sorted(self.pending_dir.glob("*.json")):
            running_path = self.running_dir / path.name
            try:
                path.replace(running_path)
            except OSError:
                continue
            job = self._read_json(running_path)
            job["status"] = "running"
            job["started_at"] = time.time()
            job["updated_at"] = time.time()
            self._write_json_atomic(running_path, job)
            return job
        return None

    def complete(self, job_id: str, summary: dict[str, Any] | None = None):
        self._finish(job_id, "done", self.done_dir, summary)

    def fail(self, job_id: str, error: str, summary: dict[str, Any] | None = None):
        payload = {"error": error, **(summary or {})}
        self._finish(job_id, "failed", self.failed_dir, payload)

    def _finish(
        self,
        job_id: str,
        status: str,
        target_dir: Path,
        summary: dict[str, Any] | None,
    ):
        running_path = self.running_dir / f"{job_id}.json"
        job = self._read_json(running_path) if running_path.exists() else {}
        job.update(
            {
                "job_id": job_id,
                "status": status,
                "summary": summary or {},
                "finished_at": time.time(),
                "updated_at": time.time(),
            }
        )
        target_path = target_dir / f"{job_id}.json"
        self._write_json_atomic(target_path, job)
        if running_path.exists():
            running_path.unlink()
        self.clear_cancel(job.get("user_id"))

    def request_cancel(self, user_id: str | None = None):
        target = "all" if not user_id else str(user_id)
        (self.cancel_dir / f"{target}.cancel").write_text(
            str(time.time()),
            encoding="utf-8",
        )

    def is_cancel_requested(self, user_id: str | None = None) -> bool:
        return (self.cancel_dir / "all.cancel").exists() or (
            bool(user_id)
            and (self.cancel_dir / f"{user_id}.cancel").exists()
        )

    def clear_cancel(self, user_id: str | None = None):
        targets = [self.cancel_dir / "all.cancel"]
        if user_id:
            targets.append(self.cancel_dir / f"{user_id}.cancel")
        for path in targets:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def active_jobs(self) -> list[dict[str, Any]]:
        return self._jobs_from(self.pending_dir) + self._jobs_from(self.running_dir)

    def latest_result(self, user_id: str | None = None) -> dict[str, Any] | None:
        jobs = self._jobs_from(self.done_dir) + self._jobs_from(self.failed_dir)
        if user_id:
            jobs = [job for job in jobs if str(job.get("user_id")) == str(user_id)]
        if not jobs:
            return None
        return max(jobs, key=lambda job: job.get("updated_at", 0))

    def user_has_active_job(self, user_id: str | None) -> bool:
        if not user_id:
            return bool(self.active_jobs())
        return any(
            str(job.get("user_id")) == str(user_id)
            for job in self.active_jobs()
        )

    def _jobs_from(self, directory: Path) -> list[dict[str, Any]]:
        jobs = []
        for path in sorted(directory.glob("*.json")):
            try:
                jobs.append(self._read_json(path))
            except (OSError, json.JSONDecodeError):
                continue
        return jobs

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
