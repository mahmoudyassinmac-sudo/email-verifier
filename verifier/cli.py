"""Command line interface.

    python -m verifier run LIST.csv            import + verify + export (resumes automatically)
    python -m verifier resume JOB_ID
    python -m verifier jobs
    python -m verifier export JOB_ID
    python -m verifier check someone@example.com
    python -m verifier smtp-test
    python -m verifier update-lists
    python -m verifier serve                   start the dashboard
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .config import Settings
from .models import DUPLICATE, INVALID, JOB_COMPLETED, PENDING, RISKY, VALID


def _fmt_eta(seconds) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def _progress_line(engine, job_id: int) -> str:
    p = engine.progress(job_id)
    c = p["counts"]
    label = (p["job"].get("stage_label") or "")[:60]
    return ("[%s] %s/%s (%.1f%%) | valid %s  risky %s  invalid %s  dup %s | %s/min | ETA %s"
            % (label, "{:,}".format(p["processed"]), "{:,}".format(p["total"]), p["percent"],
               "{:,}".format(c.get(VALID, 0)), "{:,}".format(c.get(RISKY, 0)),
               "{:,}".format(c.get(INVALID, 0)), "{:,}".format(c.get(DUPLICATE, 0)),
               p["rate_per_min"], _fmt_eta(p["eta_seconds"])))


def _run_with_progress(engine, job_id: int) -> str:
    stop = threading.Event()
    result = {"status": None, "error": None}

    def worker():
        try:
            result["status"] = engine.run(job_id, stop)
        except Exception as exc:
            result["error"] = exc

    def on_sigint(signum, frame):
        if stop.is_set():
            print("\nForce quit.")
            sys.exit(1)
        print("\nStopping safely... (progress is saved, press Ctrl+C again to force)")
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    width = 0
    while t.is_alive():
        line = _progress_line(engine, job_id)
        width = max(width, len(line))
        sys.stdout.write("\r" + line.ljust(width))
        sys.stdout.flush()
        t.join(2.0)
    line = _progress_line(engine, job_id)
    sys.stdout.write("\r" + line.ljust(width) + "\n")
    if result["error"]:
        raise result["error"]
    return result["status"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="verifier", description="Email list verifier")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="import a CSV/XLSX list and verify it")
    p_run.add_argument("file")
    p_run.add_argument("--column", help="name of the email column (auto-detected if omitted)")
    p_run.add_argument("--no-smtp", action="store_true", help="skip stage 6")

    p_res = sub.add_parser("resume", help="resume a paused job")
    p_res.add_argument("job_id", type=int)
    p_res.add_argument("--no-smtp", action="store_true")

    sub.add_parser("jobs", help="list jobs")
    p_exp = sub.add_parser("export", help="write Excel + valid CSV")
    p_exp.add_argument("job_id", type=int)
    p_exp.add_argument("--out", help="output folder")

    p_chk = sub.add_parser("check", help="verify a single address")
    p_chk.add_argument("email")
    p_chk.add_argument("--no-smtp", action="store_true")

    sub.add_parser("smtp-test", help="check whether outbound port 25 works")
    sub.add_parser("update-lists", help="download the latest disposable-domain list")
    p_srv = sub.add_parser("serve", help="start the dashboard")
    p_srv.add_argument("--host")
    p_srv.add_argument("--port", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    if getattr(args, "no_smtp", False):
        settings.smtp_enabled = False

    if args.cmd == "update-lists":
        from .lists import update_disposable_list
        n, path = update_disposable_list()
        print("Disposable list updated: %s domains -> %s" % ("{:,}".format(n), path))
        return 0

    if args.cmd == "serve":
        from server.app import serve
        serve(settings, host=args.host, port=args.port)
        return 0

    from .engine import VerifierEngine
    engine = VerifierEngine(settings)

    if args.cmd == "smtp-test":
        st = engine.smtp_status(refresh=True)
        print("SMTP enabled : %s" % st["enabled"])
        print("Port %d open : %s  (%s)" % (settings.smtp_port, st["port_open"], st["detail"]))
        print("Public IP    : %s" % (st.get("public_ip") or "unknown"))
        print("HELO name    : %s" % st["helo"])
        print("MAIL FROM    : %s" % st["mail_from"])
        return 0 if st["port_open"] else 2

    if args.cmd == "check":
        r = engine.verify_single(args.email, smtp=not args.no_smtp)
        for k in ("email", "status", "reason", "domain", "mx_host", "is_role", "is_disposable", "typo_fixed"):
            print("%-14s %s" % (k, r.get(k)))
        return 0

    if args.cmd == "jobs":
        jobs = engine.store.list_jobs()
        if not jobs:
            print("No jobs yet.")
        for j in jobs:
            c = engine.store.counts(j["id"])
            print("#%-4d %-10s %-40s total %-7s valid %-7s risky %-7s invalid %-7s pending %s"
                  % (j["id"], j["status"], j["name"][:40], c.get("total", 0), c.get(VALID, 0),
                     c.get(RISKY, 0), c.get(INVALID, 0), c.get(PENDING, 0)))
        return 0

    if args.cmd == "export":
        paths = engine.export(args.job_id, Path(args.out) if args.out else None)
        print("Excel: %s\nCSV  : %s" % (paths["xlsx"], paths["csv"]))
        return 0

    if args.cmd == "run":
        path = Path(args.file).expanduser()
        if not path.exists():
            print("File not found: %s" % path)
            return 1
        print("Importing %s (stages 1-4)..." % path.name)
        job_id = engine.import_file(path, email_column=args.column)
        job = engine.store.get_job(job_id)
        print("Job #%d - email column: '%s' - %s rows" % (job_id, job["email_column"],
                                                           "{:,}".format(job["total_rows"])))
        if job["status"] == JOB_COMPLETED:
            print("This file was already verified. Exporting again.")
        else:
            st = engine.smtp_status(refresh=True)
            if settings.smtp_enabled and not st["port_open"]:
                print("WARNING: port %d looks blocked (%s). Stage 6 will be skipped and those "
                      "emails go to Risky." % (settings.smtp_port, st["detail"]))
            status = _run_with_progress(engine, job_id)
            if status != JOB_COMPLETED:
                print("Job #%d %s. Resume with:  python -m verifier resume %d" % (job_id, status, job_id))
                return 0
        paths = engine.export(job_id)
        print("Done.\nExcel: %s\nCSV  : %s" % (paths["xlsx"], paths["csv"]))
        return 0

    if args.cmd == "resume":
        status = _run_with_progress(engine, args.job_id)
        if status == JOB_COMPLETED:
            paths = engine.export(args.job_id)
            print("Done.\nExcel: %s\nCSV  : %s" % (paths["xlsx"], paths["csv"]))
        else:
            print("Job #%d %s." % (args.job_id, status))
        return 0
    return 1
