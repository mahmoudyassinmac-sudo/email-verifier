"""Local dashboard + HTTP API for the verifier.

Runs on the machine that does the probing (the Mac), because Vercel cannot run
long jobs. Endpoints are plain JSON so n8n, or a future Vercel/Supabase dashboard,
can call the same API.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from verifier import __version__
from verifier.config import Settings
from verifier.engine import VerifierEngine
from verifier.lists import disposable_domains
from verifier.models import JOB_RUNNING

log = logging.getLogger("verifier.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class JobRunner:
    """Runs one job at a time in a background thread (one probing IP = one queue)."""

    def __init__(self, engine: VerifierEngine):
        self.engine = engine
        self._threads: Dict[int, threading.Thread] = {}
        self._stops: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def running_job(self) -> Optional[int]:
        with self._lock:
            for jid, t in self._threads.items():
                if t.is_alive():
                    return jid
        return None

    def is_running(self, job_id: int) -> bool:
        with self._lock:
            t = self._threads.get(job_id)
            return bool(t and t.is_alive())

    def start(self, job_id: int) -> None:
        current = self.running_job()
        if current is not None and current != job_id:
            raise HTTPException(409, "Job #%d is already running. Pause it first." % current)
        if current == job_id:
            return
        stop = threading.Event()
        t = threading.Thread(target=self._run, args=(job_id, stop), daemon=True, name="job-%d" % job_id)
        with self._lock:
            self._stops[job_id] = stop
            self._threads[job_id] = t
        t.start()

    def _run(self, job_id: int, stop: threading.Event) -> None:
        try:
            self.engine.run(job_id, stop)
        except Exception:
            log.exception("job %s failed", job_id)

    def pause(self, job_id: int) -> None:
        with self._lock:
            stop = self._stops.get(job_id)
        if stop:
            stop.set()


def _safe_name(name: str) -> str:
    name = Path(name or "list").name
    return re.sub(r"[^A-Za-z0-9._\- ]+", "_", name)[:120] or "list"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.load()
    engine = VerifierEngine(settings)
    engine.store.mark_interrupted_jobs()
    runner = JobRunner(engine)
    # port-25 / public-IP check can take a few seconds: do it in the background
    threading.Thread(target=engine.smtp_status, kwargs={"refresh": True}, daemon=True).start()
    app = FastAPI(title="Email Verifier", version=__version__)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    upload_dir = Path(settings.jobs_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    def job_summary(job: Dict[str, Any]) -> Dict[str, Any]:
        p = engine.progress(int(job["id"]))
        j = p["job"]
        running = runner.is_running(int(j["id"]))
        status = j["status"]
        if status == JOB_RUNNING and not running:
            status = "paused"
        return {
            "id": j["id"], "name": j["name"], "status": status, "running": running,
            "stage_label": j.get("stage_label") or "", "message": j.get("message") or "",
            "error": (j.get("error") or "").split("\n")[0],
            "email_column": j.get("email_column"), "total_rows": j.get("total_rows"),
            "created_at": j.get("created_at"), "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "counts": p["counts"], "processed": p["processed"], "pending": p["pending"],
            "total": p["total"], "percent": p["percent"], "rate_per_min": p["rate_per_min"],
            "eta_seconds": p["eta_seconds"],
        }

    def get_job_or_404(job_id: int) -> Dict[str, Any]:
        job = engine.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    # ------------------------------------------------------------ system
    def smtp_info(refresh: bool) -> Dict[str, Any]:
        if refresh:
            return engine.smtp_status(refresh=True)
        cached = engine._smtp_cache
        if cached is None:
            return {"checking": True}
        return dict(cached)

    @app.get("/api/health")
    def health(refresh: bool = False) -> Dict[str, Any]:
        return {
            "version": __version__,
            "smtp": smtp_info(refresh),
            "settings": settings.public_dict(),
            "disposable_domains": len(disposable_domains()),
            "running_job": runner.running_job(),
            "time": time.time(),
        }

    # ------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs() -> Dict[str, Any]:
        return {"jobs": [job_summary(j) for j in engine.store.list_jobs()]}

    @app.post("/api/jobs")
    def create_job(file: UploadFile = File(...), email_column: str = Form(""),
                   autostart: bool = Form(False)) -> Dict[str, Any]:
        name = _safe_name(file.filename or "list.csv")
        if Path(name).suffix.lower() not in (".csv", ".xlsx", ".xlsm", ".txt", ".tsv"):
            raise HTTPException(400, "Upload a .csv or .xlsx file")
        dest = upload_dir / ("%d_%s" % (int(time.time()), name))
        with open(dest, "wb") as out:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        try:
            job_id = engine.import_file(dest, name=name, email_column=email_column or None)
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, str(exc))
        if autostart:
            runner.start(job_id)
        return job_summary(get_job_or_404(job_id))

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: int) -> Dict[str, Any]:
        job = get_job_or_404(job_id)
        out = job_summary(job)
        out["reasons"] = engine.store.reason_breakdown(job_id)
        out["top_domains"] = engine.store.top_domains(job_id, 12)
        out["recent"] = engine.store.recent(job_id, 20)
        return out

    @app.post("/api/jobs/{job_id}/start")
    def start_job(job_id: int) -> Dict[str, Any]:
        job = get_job_or_404(job_id)
        if job["status"] == "completed":
            raise HTTPException(400, "Job already completed")
        runner.start(job_id)
        time.sleep(0.3)
        return job_summary(get_job_or_404(job_id))

    @app.post("/api/jobs/{job_id}/recheck")
    def recheck_job(job_id: int) -> Dict[str, Any]:
        get_job_or_404(job_id)
        if runner.running_job() is not None:
            raise HTTPException(409, "Another job is running. Pause it first.")
        n = engine.prepare_recheck(job_id)
        if not n:
            return {"ok": True, "requeued": 0, "message": "Nothing to re-check"}
        runner.start(job_id)
        return {"ok": True, "requeued": n, "message": "Re-checking %d unresolved emails" % n}

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: int) -> Dict[str, Any]:
        get_job_or_404(job_id)
        runner.pause(job_id)
        return {"ok": True, "message": "Pausing - the current domains finish first, progress is saved"}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: int) -> Dict[str, Any]:
        get_job_or_404(job_id)
        if runner.is_running(job_id):
            raise HTTPException(409, "Pause the job before deleting it")
        engine.store.delete_job(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download/{kind}")
    def download(job_id: int, kind: str):
        get_job_or_404(job_id)
        if kind not in ("xlsx", "csv"):
            raise HTTPException(404, "Unknown file type")
        paths = engine.export(job_id)
        path = Path(paths[kind])
        media = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                 if kind == "xlsx" else "text/csv")
        return FileResponse(str(path), media_type=media, filename=path.name)

    # ------------------------------------------------------------ single email (n8n)
    @app.post("/api/verify")
    def verify_post(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        email = str(payload.get("email") or "")
        if not email:
            raise HTTPException(400, "Body must be JSON: {\"email\": \"...\"}")
        return engine.verify_single(email, smtp=bool(payload.get("smtp", True)))

    @app.get("/api/verify")
    def verify_get(email: str, smtp: bool = True) -> Dict[str, Any]:
        return engine.verify_single(email, smtp=smtp)

    return app


def serve(settings: Optional[Settings] = None, host: Optional[str] = None,
          port: Optional[int] = None) -> None:
    import uvicorn

    settings = settings or Settings.load()
    app = create_app(settings)
    h = host or settings.host
    p = port or settings.port
    print("Dashboard: http://%s:%d" % ("127.0.0.1" if h in ("0.0.0.0", "") else h, p))
    uvicorn.run(app, host=h, port=p, log_level="warning")
