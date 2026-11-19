"""Resume + port-25-blocked fallback tests."""
import os, sys, tempfile, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
tmp = Path(tempfile.mkdtemp())
os.environ.update({
    "DB_PATH": str(tmp / "t.db"), "JOBS_DIR": str(tmp / "jobs"), "SMTP_PORT": "2526",
    "PREFLIGHT_HOST": "127.0.0.1", "PROBE_HELO": "probe.test", "PER_MX_INTERVAL": "0.3",
    "PER_MX_JITTER": "0", "RCPT_DELAY_MIN": "0.2", "RCPT_DELAY_MAX": "0.2", "RETRY_DELAY": "1",
    "DNS_FALLBACK_SERVERS": "", "SMTP_WORKERS": "1",
    "STATIC_MX": ";".join("d%d.test=127.0.0.1" % i for i in range(30)),
})
import mock_smtp
from verifier import Settings, VerifierEngine

csv = tmp / "big.csv"
with open(csv, "w") as f:
    f.write("email\n")
    for i in range(30):
        for j in range(3):
            f.write("%s%d@d%d.test\n" % ("valid" if j else "nobody", j, i))

ctl, h = mock_smtp.start(2526)
eng = VerifierEngine(Settings.load(Path("/x")))
job = eng.import_file(csv)
stop = threading.Event()
t = threading.Thread(target=eng.run, args=(job, stop)); t.start()
time.sleep(3); stop.set(); t.join()
p = eng.progress(job)
print("after stop:", eng.store.get_job(job)["status"], "processed", p["processed"], "/", p["total"])
assert eng.store.get_job(job)["status"] == "paused" and 0 < p["processed"] < p["total"]
# a brand-new engine (like after a crash) resumes from the DB
eng2 = VerifierEngine(Settings.load(Path("/x")))
assert eng2.import_file(csv) == job, "same file must map to the same unfinished job"
print("resume ->", eng2.run(job), eng2.store.counts(job))
ctl.stop()

# port 25 blocked: nothing listens on 2599
os.environ["SMTP_PORT"] = "2599"; os.environ["DB_PATH"] = str(tmp / "t2.db")
eng3 = VerifierEngine(Settings.load(Path("/x")))
j3 = eng3.import_file(csv)
print("blocked ->", eng3.run(j3), eng3.store.counts(j3))
print(set(e["reason"] for e in eng3.store.iter_emails(j3)))
