"""DNS failures are retried (multi-pass) and old 'DNS lookup failed' rows are re-checked on resume."""
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
os.environ.update({"DB_PATH": str(tmp / "t.db"), "JOBS_DIR": str(tmp / "jobs"), "SMTP_ENABLED": "false",
                   "DNS_FALLBACK_SERVERS": "", "DNS_OVER_HTTPS": "false"})
from verifier import Settings, VerifierEngine
from verifier import engine as eng_mod
from verifier.dns_check import MxResult, MX_ERROR, MX_OK

calls = {}
def flaky(domain):
    calls[domain] = calls.get(domain, 0) + 1
    if domain.startswith("flaky") and calls[domain] < 2:
        return MxResult(MX_ERROR, [], "NoNameservers")
    if domain.startswith("dead"):
        return MxResult(MX_ERROR, [], "NoNameservers")
    return MxResult(MX_OK, ["mx." + domain])

csv = tmp / "l.csv"
csv.write_text("email\n" + "".join("u%d@flaky%d.test\n" % (i, i) for i in range(5)) + "a@ok.test\nb@dead.test\n")
e = VerifierEngine(Settings.load(Path("/x")))
e.resolver.resolve = flaky
eng_mod_wait = None
job = e.import_file(csv)
e.run(job)
res = {r["email"]: (r["status"], r["reason"]) for r in e.store.iter_emails(job)}
for k, v in res.items(): print(k, v)
assert all(v[1] == "Not SMTP-checked: SMTP stage disabled" for k, v in res.items() if "flaky" in k or "ok" in k)
assert res["b@dead.test"] == ("risky", "Unknown: DNS lookup failed")
# resume re-checks the DNS failure; now it resolves
calls.clear()
e.resolver.resolve = lambda d: MxResult(MX_OK, ["mx." + d])
e.store.update_job(job, status="paused")
e.run(job)
print("after resume:", [(r["email"], r["status"], r["reason"]) for r in e.store.iter_emails(job) if "dead" in r["email"]])

# hosted-business requeue when SKIP_HOSTED_BUSINESS flips to false
e.store.update_emails([(r["id"], {"status": "risky", "stage": "done", "reason": "Hosted on Microsoft 365 - not SMTP-verifiable"})
                       for r in e.store.iter_emails(job) if r["email"] == "a@ok.test"])
e.s.skip_hosted_business = False
e.store.update_job(job, status="paused")
e.run(job)
print("after hosted requeue:", [(r["email"], r["reason"]) for r in e.store.iter_emails(job) if r["email"] == "a@ok.test"])
