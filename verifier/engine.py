"""The verification engine: orchestrates the 6 stages, persists progress, resumes."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import lists
from .cleaning import process_offline
from .config import Settings
from .dns_check import (MX_ERROR, MX_IMPLICIT, MX_NONE, MX_NULL, MX_NXDOMAIN, MX_OK,
                        MxResolver, MxResult, public_ip_and_ptr)
from .io_input import load_list
from .models import (DUPLICATE, INVALID, JOB_COMPLETED, JOB_FAILED, JOB_PAUSED, JOB_READY,
                     JOB_RUNNING, PENDING, RISKY, STAGE_DONE, STAGE_MX, STAGE_RETRY, STAGE_SMTP,
                     VALID, R)
from .smtp_check import (BLOCKED, CONN_FAIL, DISABLED, FULL, NO_MAILBOX, OK, SENDER_REJECTED,
                         UNCLEAR, ProviderRateLimiter, RcptResult, SmtpProber, port25_open)
from .storage import Storage, open_storage

log = logging.getLogger("verifier")

Outcome = Tuple[str, str]  # (status, reason)


def finalize_role(status: str, reason: str, is_role: bool, prefix: str) -> Outcome:
    """Role addresses are flagged, never deleted: a valid role address becomes Risky."""
    if not is_role:
        return status, reason
    if status == VALID:
        return RISKY, R.ROLE.format(prefix=prefix) + " - mailbox exists"
    if status == RISKY:
        return RISKY, "%s; %s" % (reason, R.ROLE.format(prefix=prefix))
    return status, reason


def mx_outcome(mx: MxResult) -> Optional[Outcome]:
    """None means: continue to stage 6."""
    if mx.status in (MX_OK, MX_IMPLICIT):
        return None
    if mx.status == MX_NXDOMAIN:
        return INVALID, R.NXDOMAIN
    if mx.status == MX_NULL:
        return INVALID, R.NULL_MX
    if mx.status == MX_NONE:
        return INVALID, R.NO_MAIL_SERVER
    return RISKY, R.UNKNOWN_DNS


def rcpt_outcome(r: RcptResult) -> Optional[Outcome]:
    """None means: temporary -> retry later."""
    code = r.code or "-"
    if r.kind == OK:
        return VALID, R.MAILBOX_EXISTS.format(code=code)
    if r.kind == NO_MAILBOX:
        return INVALID, R.MAILBOX_NOT_FOUND.format(code=code)
    if r.kind == DISABLED:
        return INVALID, R.MAILBOX_DISABLED.format(code=code)
    if r.kind == FULL:
        return RISKY, R.MAILBOX_FULL.format(code=code)
    if r.kind == UNCLEAR:
        return RISKY, R.UNCLEAR_REJECT.format(code=code)
    if r.kind == BLOCKED:
        return RISKY, R.UNKNOWN_BLOCKED
    if r.kind == SENDER_REJECTED:
        return RISKY, R.UNKNOWN_SENDER.format(code=code)
    return None  # TEMP / CONN_FAIL


def exhausted_outcome(r: RcptResult) -> Outcome:
    if r.kind == CONN_FAIL:
        return RISKY, R.UNKNOWN_TIMEOUT
    return RISKY, R.UNKNOWN_TEMP.format(code=r.code or "-")


class VerifierEngine:
    def __init__(self, settings: Optional[Settings] = None, storage: Optional[Storage] = None):
        self.s = settings or Settings.load()
        self.store = storage or open_storage(self.s)
        self.resolver = MxResolver(self.s)
        self.limiter = ProviderRateLimiter(self.s.per_mx_interval, self.s.per_mx_jitter)
        self._helo: Optional[str] = None
        self._public_ip: Optional[str] = None
        self._smtp_cache: Optional[Dict[str, Any]] = None
        self._samples: Dict[int, Deque[Tuple[float, int]]] = {}
        self._label_at: Dict[int, float] = {}
        self._lock = threading.Lock()
        # per-run state
        self._block_reason: Optional[str] = None
        self._outcomes: Deque[bool] = deque(maxlen=max(1, self.s.block_window))
        self._sender_rejects = 0

    # ================================================================ SMTP readiness
    @property
    def helo(self) -> str:
        if self._helo is None:
            if self.s.probe_helo:
                self._helo = self.s.probe_helo
            else:
                ip, ptr = public_ip_and_ptr()
                self._public_ip = ip
                sender_domain = self.s.probe_from.rsplit("@", 1)[-1] if "@" in self.s.probe_from else ""
                if ptr:
                    self._helo = ptr
                elif sender_domain:
                    self._helo = sender_domain
                elif ip:
                    self._helo = "[%s]" % ip
                else:
                    self._helo = "localhost.localdomain"
        return self._helo

    def smtp_status(self, refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            c = self._smtp_cache
            if c and not refresh and time.time() - c["checked_at"] < 600:
                return dict(c)
        if not self.s.smtp_enabled:
            info = {"enabled": False, "port_open": None, "detail": "SMTP stage disabled in settings"}
        else:
            ok, detail = port25_open(self.s.preflight_host, self.s.smtp_port)
            info = {"enabled": True, "port_open": ok, "detail": detail}
        info["helo"] = self.helo if info.get("port_open") else (self.s.probe_helo or "(auto)")
        info["public_ip"] = self._public_ip
        info["mail_from"] = self.s.probe_from or "<> (null sender)"
        info["checked_at"] = time.time()
        with self._lock:
            self._smtp_cache = info
        return dict(info)

    # ================================================================ import (stages 1-4)
    def import_file(self, path, name: Optional[str] = None, email_column: Optional[str] = None,
                    reuse_unfinished: bool = True) -> int:
        path = Path(path)
        lst = load_list(path, email_column)
        if reuse_unfinished:
            existing = self.store.find_job_by_hash(lst.sha256)
            if existing and existing["status"] != JOB_COMPLETED:
                return int(existing["id"])

        job_id = self.store.create_job(
            name=name or path.name,
            source_file=str(path),
            source_sha256=lst.sha256,
            email_column=lst.email_column,
            columns_json=json.dumps(lst.columns, ensure_ascii=False),
            status=JOB_READY,
            stage_label="Imported - stages 1-4 done",
            total_rows=len(lst.rows),
        )

        seen: Dict[str, int] = {}
        records: List[Dict[str, Any]] = []
        for row_no, raw, extras in lst.rows:
            off = process_offline(raw)
            rec: Dict[str, Any] = {
                "row_index": row_no,
                "original_email": off.original_email,
                "email": off.email,
                "domain": off.domain,
                "is_role": int(off.is_role),
                "role_prefix": off.role_prefix,
                "is_disposable": int(off.is_disposable),
                "typo_fixed": off.typo_fixed,
                "attempts": 0,
                "extra_json": json.dumps(extras, ensure_ascii=False),
                "reason": "",
            }
            if off.email and off.email in seen:
                rec.update(status=DUPLICATE, stage=STAGE_DONE,
                           reason=R.DUPLICATE.format(row=seen[off.email]))
            else:
                if off.email:
                    seen[off.email] = row_no
                if off.status == INVALID:
                    rec.update(status=INVALID, stage=STAGE_DONE, reason=off.reason)
                else:
                    rec.update(status=PENDING, stage=STAGE_MX)
            records.append(rec)
        self.store.insert_emails(job_id, records)
        return job_id

    # ================================================================ run (stages 5-6)
    def run(self, job_id: int, stop: Optional[threading.Event] = None) -> str:
        """Run / resume a job until finished or `stop` is set. Returns the final job status."""
        stop = stop or threading.Event()
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("Job %s not found" % job_id)
        self.store.update_job(job_id, status=JOB_RUNNING, error=None, message=None,
                              started_at=job.get("started_at") or time.time())
        self._block_reason = None
        self._outcomes.clear()
        self._sender_rejects = 0
        try:
            self._stage_mx(job_id, stop)
            if not stop.is_set():
                self._stage_smtp(job_id, stop)
            if stop.is_set():
                self.store.update_job(job_id, status=JOB_PAUSED, stage_label="Paused",
                                      message="Paused - progress saved")
                return JOB_PAUSED
            self.store.update_job(job_id, status=JOB_COMPLETED, finished_at=time.time(),
                                  stage_label="Completed")
            try:
                self.export(job_id)
            except Exception as exc:  # export problems must not fail the job
                log.warning("auto-export failed: %s", exc)
            return JOB_COMPLETED
        except Exception as exc:
            self.store.update_job(job_id, status=JOB_FAILED, stage_label="Failed",
                                  error="%s\n%s" % (exc, traceback.format_exc()[-2000:]))
            raise

    def prepare_recheck(self, job_id: int) -> int:
        """Re-open the unresolved results of a finished job: DNS failures go back to the
        MX stage, every 'Unknown: ...' / 'Not SMTP-checked: ...' goes back to SMTP."""
        n = self.store.reset_dns_failures(job_id)
        n += self.store.requeue_reason(job_id, "Unknown: ", STAGE_SMTP)
        n += self.store.requeue_reason(job_id, "Not SMTP-checked: ", STAGE_SMTP)
        if not self.s.skip_hosted_business:
            n += self.store.requeue_reason(job_id, "Hosted on ", STAGE_SMTP)
        if n:
            self.store.update_job(job_id, status=JOB_PAUSED, finished_at=None,
                                  stage_label="Re-check queued (%d emails)" % n, message=None)
        return n

    def _label(self, job_id: int, text: str, force: bool = False) -> None:
        now = time.time()
        if force or now - self._label_at.get(job_id, 0) > 1.0:
            self._label_at[job_id] = now
            self.store.update_job(job_id, stage_label=text)

    # ---------------------------------------------------------------- stage 5
    def _cached_mx(self, domain: str) -> Optional[MxResult]:
        row = self.store.get_domain(domain)
        if not row or not row.get("mx_checked_at"):
            return None
        if row["mx_status"] == MX_ERROR:
            return None
        if time.time() - row["mx_checked_at"] > self.s.mx_cache_days * 86400:
            return None
        return MxResult(row["mx_status"], row.get("mx_hosts") or [], row.get("mx_detail") or "")

    def lookup_mx(self, domain: str) -> MxResult:
        cached = self._cached_mx(domain)
        if cached:
            return cached
        res = self.resolver.resolve(domain)
        self.store.upsert_domain(domain, mx_status=res.status, mx_hosts=res.hosts,
                                 mx_detail=res.detail, mx_checked_at=time.time())
        return res

    def _apply_mx(self, job_id: int, domain: str, mx: MxResult) -> None:
        emails = self.store.emails_for_domain(job_id, domain, STAGE_MX)
        outcome = mx_outcome(mx)
        updates = []
        for e in emails:
            if outcome is None:
                updates.append((e["id"], {"stage": STAGE_SMTP, "mx_host": mx.primary}))
            else:
                status, reason = finalize_role(outcome[0], outcome[1], bool(e["is_role"]), e["role_prefix"] or "")
                updates.append((e["id"], {"status": status, "reason": reason, "stage": STAGE_DONE,
                                          "mx_host": mx.primary}))
        self.store.update_emails(updates)

    def _stage_mx(self, job_id: int, stop: threading.Event) -> None:
        reset = self.store.reset_dns_failures(job_id)
        if reset:
            log.info("re-checking %d emails whose DNS lookup failed earlier", reset)
        passes = max(1, self.s.dns_passes)
        for attempt in range(passes):
            if stop.is_set():
                return
            last = attempt == passes - 1
            failed = self._mx_pass(job_id, stop, attempt, last,
                                   workers=self.s.dns_workers if attempt == 0 else max(1, self.s.dns_workers // 2))
            if not failed:
                return
            if not last:
                self._label(job_id, "Stage 5/6 - MX lookup: retrying %d domains that failed DNS" % failed, force=True)
                stop.wait(self.s.dns_retry_wait)

    def _mx_pass(self, job_id: int, stop: threading.Event, attempt: int, last: bool, workers: int) -> int:
        """One pass over domains still at the MX stage. Returns how many hit DNS errors
        (left pending unless this is the last pass)."""
        domains = self.store.domains_at_stage(job_id, STAGE_MX)
        if not domains:
            return 0
        total = len(domains)
        done = 0
        failed = 0
        tag = "" if attempt == 0 else " - retry %d" % attempt
        need: List[str] = []
        for d in domains:
            cached = self._cached_mx(d)
            if cached:
                self._apply_mx(job_id, d, cached)
                done += 1
            else:
                need.append(d)
        self._label(job_id, "Stage 5/6 - MX lookup%s (%d/%d domains)" % (tag, done, total), force=True)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.resolver.resolve, d): d for d in need}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = MxResult(MX_ERROR, [], str(exc))
                self.store.upsert_domain(d, mx_status=res.status, mx_hosts=res.hosts,
                                         mx_detail=res.detail, mx_checked_at=time.time())
                if res.status == MX_ERROR and not last:
                    failed += 1          # stays at the MX stage for the next pass
                else:
                    if res.status == MX_ERROR:
                        failed += 1
                    self._apply_mx(job_id, d, res)
                done += 1
                self._label(job_id, "Stage 5/6 - MX lookup%s (%d/%d domains)" % (tag, done, total))
                if stop.is_set():
                    for f in futures:
                        f.cancel()
                    break
        return failed

    # ---------------------------------------------------------------- stage 6
    def _skip_reason(self, domain: str, mx_host: str) -> Optional[str]:
        prov = lists.consumer_provider(domain)
        if prov:
            return R.MAJOR_PROVIDER.format(provider=prov)
        if self.s.skip_hosted_business:
            hosted = lists.hosted_provider(mx_host)
            if hosted:
                return R.HOSTED_PROVIDER.format(provider=hosted)
        return None

    def _finalize(self, job_id: int, emails: List[Dict[str, Any]], status: str, reason: str,
                  extra: Optional[Dict[str, Any]] = None) -> None:
        updates = []
        for e in emails:
            st, rs = finalize_role(status, reason, bool(e["is_role"]), e["role_prefix"] or "")
            fields = {"status": st, "reason": rs, "stage": STAGE_DONE}
            if extra:
                fields.update(extra)
            updates.append((e["id"], fields))
        self.store.update_emails(updates)

    def _stage_smtp(self, job_id: int, stop: threading.Event) -> None:
        if not self.s.skip_hosted_business:
            n = self.store.requeue_reason(job_id, "Hosted on ", STAGE_SMTP)
            if n:
                log.info("probing %d emails on hosted business domains (SKIP_HOSTED_BUSINESS=false)", n)
        # preflight
        if not self.s.smtp_enabled:
            self._block_reason = R.NOT_CHECKED_DISABLED
        else:
            st = self.smtp_status(refresh=True)
            if not st.get("port_open"):
                self._block_reason = R.NOT_CHECKED_PORT25
                self.store.update_job(job_id, message="Port %d blocked: %s" % (self.s.smtp_port, st.get("detail")))
        prober = SmtpProber(self.s, self.limiter, self.helo if not self._block_reason else "localhost")

        while not stop.is_set():
            self.store.release_due_retries(job_id, time.time())
            domains = self.store.domains_at_stage(job_id, STAGE_SMTP)
            if not domains:
                nxt = self.store.next_retry_at(job_id)
                if nxt is None:
                    return
                if self._block_reason:
                    # nothing will be probed any more - close the waiting ones now
                    self.store.release_due_retries(job_id, float("inf"))
                    continue
                while not stop.is_set() and time.time() < nxt:
                    waiting = self.store.counts(job_id).get("stage_retry", 0)
                    secs = int(max(0, nxt - time.time()))
                    self._label(job_id, "Stage 6/6 - waiting to retry %d greylisted/temp-failed "
                                        "emails (next in %d:%02d)" % (waiting, secs // 60, secs % 60))
                    stop.wait(1.0)
                continue

            # fast path: domains that are never probed
            network: List[Tuple[str, List[str]]] = []
            for d in domains:
                info = self.store.get_domain(d) or {}
                hosts = info.get("mx_hosts") or []
                emails = self.store.emails_for_domain(job_id, d, STAGE_SMTP)
                primary = hosts[0] if hosts else ""
                skip = self._skip_reason(d, primary)
                if skip:
                    self._finalize(job_id, emails, RISKY, skip)
                elif self._block_reason:
                    self._finalize(job_id, emails, RISKY, self._block_reason)
                elif info.get("catch_all") == 1 and self._fresh_catch_all(info):
                    self._finalize(job_id, emails, RISKY, R.CATCH_ALL)
                else:
                    network.append((d, hosts))
            if stop.is_set() or not network:
                continue

            random.shuffle(network)  # spread load across providers
            total = len(network)
            done = 0
            self._label(job_id, "Stage 6/6 - SMTP probing (0/%d domains)" % total, force=True)
            with ThreadPoolExecutor(max_workers=max(1, self.s.smtp_workers)) as pool:
                futures = [pool.submit(self._probe_domain, job_id, d, hosts, stop, prober)
                           for d, hosts in network]
                for fut in as_completed(futures):
                    fut.result()
                    done += 1
                    self._label(job_id, "Stage 6/6 - SMTP probing (%d/%d domains)" % (done, total))

    def _fresh_catch_all(self, info: Dict[str, Any]) -> bool:
        t = info.get("catch_all_checked_at") or 0
        return time.time() - t < self.s.catch_all_cache_days * 86400

    def _record_session(self, blocked: bool) -> None:
        with self._lock:
            self._outcomes.append(blocked)
            n = len(self._outcomes)
            if (self._block_reason is None and n >= self.s.block_window
                    and sum(self._outcomes) / n >= self.s.block_ratio):
                self._block_reason = R.NOT_CHECKED_IP_BLOCKED
                log.warning("Circuit breaker: %d/%d recent SMTP sessions were blocked; "
                            "stopping SMTP probing for this run.", sum(self._outcomes), n)

    def _probe_domain(self, job_id: int, domain: str, hosts: List[str], stop: threading.Event,
                      prober: SmtpProber) -> None:
        try:
            self._probe_domain_inner(job_id, domain, hosts, stop, prober)
        except Exception as exc:  # never let one domain kill the run
            log.exception("probe failed for %s", domain)
            emails = self.store.emails_for_domain(job_id, domain, STAGE_SMTP)
            self._finalize(job_id, emails, RISKY, R.UNKNOWN_TIMEOUT,
                           {"smtp_message": ("internal error: %s" % exc)[:300]})

    def _probe_domain_inner(self, job_id: int, domain: str, hosts: List[str], stop: threading.Event,
                            prober: SmtpProber) -> None:
        if stop.is_set():
            return
        emails = self.store.emails_for_domain(job_id, domain, STAGE_SMTP)
        if not emails:
            return
        if self._block_reason:
            self._finalize(job_id, emails, RISKY, self._block_reason)
            return
        info = self.store.get_domain(domain) or {}
        known = None
        if info.get("catch_all") is not None and self._fresh_catch_all(info):
            known = bool(info["catch_all"])

        probe = prober.probe_domain(domain, hosts, [e["email"] for e in emails], known, stop)

        if probe.catch_all is not None and known is None:
            self.store.upsert_domain(domain, catch_all=int(probe.catch_all),
                                     catch_all_checked_at=time.time())
        blocked = probe.ip_blocked or bool(probe.session_error and probe.session_error.kind == BLOCKED)
        if probe.results and not blocked:
            blocked = all(r.kind == BLOCKED for r in probe.results.values())
        if not (probe.session_error and probe.session_error.message == "stopped"):
            self._record_session(blocked)

        if probe.session_error and probe.session_error.kind == SENDER_REJECTED:
            with self._lock:
                self._sender_rejects += 1
                warn = self._sender_rejects == 3
            if warn:
                self.store.update_job(job_id, message=(
                    "Mail servers are rejecting the probe sender %s (%s %s). Set PROBE_FROM in .env "
                    "to an address on a real domain and restart." % (
                        self.s.probe_from or "<>", probe.session_error.code,
                        probe.session_error.message[:120])))

        mx_used = probe.mx_used or (hosts[0] if hosts else "")
        if probe.catch_all:
            self._finalize(job_id, emails, RISKY, R.CATCH_ALL, {"mx_host": mx_used})
            return

        updates = []
        now = time.time()
        for e in emails:
            r = probe.session_error or probe.results.get(e["email"])
            if r is None or (r.kind == CONN_FAIL and r.message == "stopped"):
                continue  # untouched -> stays pending, resumes next run
            fields: Dict[str, Any] = {"smtp_code": r.code or None, "smtp_message": r.message[:300],
                                      "mx_host": mx_used}
            outcome = rcpt_outcome(r)
            if outcome is None:
                attempts = int(e["attempts"] or 0) + 1
                if attempts <= self.s.max_retries and not self._block_reason:
                    fields.update(stage=STAGE_RETRY, attempts=attempts,
                                  next_attempt_at=now + self.s.retry_delay)
                    updates.append((e["id"], fields))
                    continue
                outcome = exhausted_outcome(r)
                fields["attempts"] = attempts
            st, rs = finalize_role(outcome[0], outcome[1], bool(e["is_role"]), e["role_prefix"] or "")
            fields.update(status=st, reason=rs, stage=STAGE_DONE)
            updates.append((e["id"], fields))
        self.store.update_emails(updates)

    # ================================================================ single email (n8n)
    def verify_single(self, raw: str, smtp: bool = True) -> Dict[str, Any]:
        off = process_offline(raw)
        out: Dict[str, Any] = {
            "email": off.email, "original_email": off.original_email, "domain": off.domain,
            "status": off.status, "reason": off.reason, "mx_host": "", "is_role": off.is_role,
            "is_disposable": off.is_disposable, "typo_fixed": off.typo_fixed,
        }
        if off.status == INVALID:
            return self._pretty(out)
        mx = self.lookup_mx(off.domain)
        out["mx_host"] = mx.primary
        outcome = mx_outcome(mx)
        if outcome is None:
            outcome = self._single_smtp(off.email, off.domain, mx, smtp)
        st, rs = finalize_role(outcome[0], outcome[1], off.is_role, off.role_prefix)
        out["status"], out["reason"] = st, rs
        return self._pretty(out)

    def _single_smtp(self, email: str, domain: str, mx: MxResult, smtp: bool) -> Outcome:
        skip = self._skip_reason(domain, mx.primary)
        if skip:
            return RISKY, skip
        if not smtp or not self.s.smtp_enabled:
            return RISKY, R.NOT_CHECKED_DISABLED
        if not self.smtp_status().get("port_open"):
            return RISKY, R.NOT_CHECKED_PORT25
        info = self.store.get_domain(domain) or {}
        known = None
        if info.get("catch_all") is not None and self._fresh_catch_all(info):
            known = bool(info["catch_all"])
        prober = SmtpProber(self.s, self.limiter, self.helo)
        probe = prober.probe_domain(domain, mx.hosts, [email], known)
        if probe.catch_all is not None and known is None:
            self.store.upsert_domain(domain, catch_all=int(probe.catch_all),
                                     catch_all_checked_at=time.time())
        if probe.catch_all:
            return RISKY, R.CATCH_ALL
        r = probe.session_error or probe.results.get(email)
        if r is None:
            return RISKY, R.UNKNOWN_TIMEOUT
        return rcpt_outcome(r) or exhausted_outcome(r)

    @staticmethod
    def _pretty(out: Dict[str, Any]) -> Dict[str, Any]:
        out["status"] = {VALID: "Valid", RISKY: "Risky", INVALID: "Invalid"}.get(out["status"], out["status"])
        return out

    # ================================================================ progress
    def progress(self, job_id: int) -> Dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("Job %s not found" % job_id)
        c = self.store.counts(job_id)
        total = c.get("total", 0)
        pending = c.get(PENDING, 0)
        processed = total - pending
        now = time.time()
        samples = self._samples.setdefault(job_id, deque(maxlen=600))
        if samples and processed < samples[-1][1]:
            samples.clear()  # a re-check put emails back in the queue: restart the speed window
        if not samples or now - samples[-1][0] >= 1.0:
            samples.append((now, processed))
        rate = 0.0
        window = [s for s in samples if now - s[0] <= 300]
        if len(window) >= 2 and window[-1][0] > window[0][0]:
            rate = max(0.0, (window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0]))
        eta = None
        if job["status"] == JOB_RUNNING and pending:
            if rate > 0:
                eta = pending / rate
            nxt = self.store.next_retry_at(job_id)
            if nxt and c.get("stage_smtp", 0) == 0 and c.get("stage_mx", 0) == 0:
                eta = max(eta or 0, nxt - now)
        return {
            "job": job,
            "counts": c,
            "total": total,
            "processed": processed,
            "pending": pending,
            "percent": round(100.0 * processed / total, 1) if total else 100.0,
            "rate_per_min": round(rate * 60, 1),
            "eta_seconds": int(eta) if eta is not None else None,
        }

    # ================================================================ export
    def export(self, job_id: int, out_dir: Optional[Path] = None) -> Dict[str, str]:
        from .export import export_excel, export_valid_csv

        job = self.store.get_job(job_id)
        if not job:
            raise ValueError("Job %s not found" % job_id)
        out_dir = Path(out_dir or Path(self.s.jobs_dir) / ("job_%d" % job_id))
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(job["name"]).stem or "list"
        xlsx = out_dir / ("%s_verified.xlsx" % stem)
        csvp = out_dir / ("%s_valid.csv" % stem)
        export_excel(self.store, job, xlsx)
        export_valid_csv(self.store, job, csvp)
        return {"xlsx": str(xlsx), "csv": str(csvp)}
