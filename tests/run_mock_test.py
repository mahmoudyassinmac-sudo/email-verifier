"""End-to-end test of all 6 stages against the mock SMTP server (no internet needed)."""
import json, os, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

tmp = Path(tempfile.mkdtemp())
os.environ.update({
    "DB_PATH": str(tmp / "t.db"), "JOBS_DIR": str(tmp / "jobs"),
    "SMTP_PORT": "2525", "PREFLIGHT_HOST": "127.0.0.1", "PROBE_HELO": "probe.test",
    "PER_MX_INTERVAL": "0.2", "PER_MX_JITTER": "0.1", "RCPT_DELAY_MIN": "0", "RCPT_DELAY_MAX": "0.05",
    "RETRY_DELAY": "2", "MAX_RETRIES": "2", "DNS_FALLBACK_SERVERS": "",
    "STATIC_MX": ";".join([
        "company.test=127.0.0.1", "catchall.test=127.0.0.1", "blocked.test=127.0.0.1",
        "grey.test=127.0.0.1", "gmail.com=gmail-smtp-in.l.google.com",
        "hotmail.com=hotmail-com.olc.protection.outlook.com",
        "m365corp.test=m365corp-test.mail.protection.outlook.com",
        "company.com=127.0.0.1",
    ]),
})
import mock_smtp
from verifier import Settings, VerifierEngine

ctl, handler = mock_smtp.start(2525)
try:
    eng = VerifierEngine(Settings.load(Path("/nonexistent")))
    job = eng.import_file(ROOT / "tests" / "sample_list.csv")
    t0 = time.time()
    status = eng.run(job)
    print("status:", status, "in %.1fs" % (time.time() - t0))
    for e in eng.store.iter_emails(job):
        print("%-28s %-9s %-60s %s" % (e["original_email"][:28], e["status"], e["reason"][:60], e["typo_fixed"] or ""))
    print(json.dumps(eng.store.counts(job)))
    paths = eng.export(job)
    print(paths)
    print("single:", eng.verify_single("valid9@company.test"))
    print("single:", eng.verify_single("nobody@company.test"))
    print("single:", eng.verify_single("x@gmail.com"))
finally:
    ctl.stop()

# re-check: unresolved results of a finished job go back into the pipeline
ctl, handler = mock_smtp.start(2527)
eng.s.smtp_port = 2527
n = eng.prepare_recheck(job)
print("recheck requeued:", n, eng.store.get_job(job)["status"])
print("recheck ->", eng.run(job), eng.store.counts(job))
ctl.stop()
