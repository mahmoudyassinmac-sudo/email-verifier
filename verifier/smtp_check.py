"""Stage 6: SMTP RCPT TO probing (no message is ever sent).

Session per domain:
    connect -> EHLO -> MAIL FROM -> RCPT TO:<random-impossible>  (catch-all test)
    -> RCPT TO:<each address> ... -> QUIT
DATA is never issued, so nothing is delivered.
"""
from __future__ import annotations

import hashlib
import random
import re
import smtplib
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Settings
from .lists import provider_key

# outcome kinds for a single RCPT
OK = "ok"
NO_MAILBOX = "no_mailbox"
DISABLED = "disabled"
FULL = "full"
TEMP = "temp"
BLOCKED = "blocked"
UNCLEAR = "unclear"
CONN_FAIL = "conn_fail"
SENDER_REJECTED = "sender_rejected"

_BLOCK_WORDS = re.compile(
    r"spamhaus|blocked|block ?list|blacklist|black ?listed|\brbl\b|dnsbl|reputation|"
    r"client host|not authori[sz]ed|access denied|\bpbl\b|residential|dynamic ?ip|"
    r"dial-?up|poor reputation|banned|denied by policy|5\.7\.1|5\.7\.606|5\.7\.511|"
    r"listed|sorbs|barracuda|spamcop|cbl\b|refused|rejected due to",
    re.I,
)
# block signals that are not just an enhanced status code (5.7.1 is also used for "user unknown")
_STRICT_BLOCK_WORDS = re.compile(
    r"spamhaus|blocked|block ?list|blacklist|black ?listed|\brbl\b|dnsbl|reputation|"
    r"client host|not authori[sz]ed|\bpbl\b|residential|dynamic|dial-?up|ers-dul|"
    r"sender ip|reverse dns|ptr|proofpoint|ipcheck|banned|denied by policy|policy reasons|"
    r"sorbs|barracuda|spamcop|\bcbl\b|refused|helo|listed",
    re.I,
)
_RELAY_WORDS = re.compile(r"relay(ing)? (access )?denied|relay not permitted|unable to relay", re.I)
_NO_USER_WORDS = re.compile(
    r"user unknown|unknown user|no such user|no such recipient|does not exist|doesn't exist|"
    r"not exist|unknown recipient|recipient unknown|invalid recipient|recipient not found|"
    r"mailbox unavailable|mailbox not found|no mailbox|address rejected|"
    r"unrouteable|undeliverable address|user not found|not a valid|no such address|"
    r"recipient rejected|account (?:does not|doesn't) exist|5\.1\.1|5\.1\.0|5\.1\.10|5\.1\.6",
    re.I,
)
_DISABLED_WORDS = re.compile(r"disabled|inactive|deactivated|suspended|5\.2\.1", re.I)
_FULL_WORDS = re.compile(r"over ?quota|quota exceeded|mailbox full|insufficient storage|5\.2\.2", re.I)


def classify(code: int, message: str) -> str:
    msg = message or ""
    if code in (250, 251):
        return OK
    if 400 <= code < 500:
        if code == 452 and _FULL_WORDS.search(msg):
            return FULL
        return TEMP
    if 500 <= code < 600:
        if _RELAY_WORDS.search(msg):
            return UNCLEAR          # server does not handle mail for this domain
        if _NO_USER_WORDS.search(msg) and not _STRICT_BLOCK_WORDS.search(msg):
            return NO_MAILBOX
        if _FULL_WORDS.search(msg) or code == 552:
            return FULL
        if _DISABLED_WORDS.search(msg):
            return DISABLED
        if _BLOCK_WORDS.search(msg):
            return BLOCKED
        if code in (550, 551, 553) and not msg.strip():
            return NO_MAILBOX
        return UNCLEAR
    return UNCLEAR


@dataclass
class RcptResult:
    kind: str
    code: int = 0
    message: str = ""


@dataclass
class DomainProbe:
    catch_all: Optional[bool] = None
    session_error: Optional[RcptResult] = None   # set when the whole domain failed
    results: Dict[str, RcptResult] = field(default_factory=dict)
    mx_used: str = ""
    ip_blocked: bool = False


class ProviderRateLimiter:
    """Minimum gap (+ random jitter) between connections to the same mail provider."""

    def __init__(self, interval: float, jitter: float):
        self.interval = interval
        self.jitter = jitter
        self._next: Dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, mx_host: str) -> float:
        """Reserve the next slot; returns seconds to wait before connecting."""
        key = provider_key(mx_host)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(key, 0.0))
            self._next[key] = slot + self.interval + random.uniform(0, self.jitter)
            return slot - now

    def ready_in(self, mx_host: str) -> float:
        key = provider_key(mx_host)
        with self._lock:
            return max(0.0, self._next.get(key, 0.0) - time.monotonic())


def _decode(msg) -> str:
    if isinstance(msg, bytes):
        return msg.decode("utf-8", errors="replace")
    return str(msg or "")


def random_local_part(domain: str) -> str:
    """An address that cannot exist. Stable per domain, so a greylisting server
    sees the same sender/recipient pair again on retry and lets it through."""
    digest = hashlib.sha1(("catch-all-probe:" + domain).encode()).hexdigest()
    return "zz" + digest[:18] + "x9q"


class SmtpProber:
    def __init__(self, settings: Settings, limiter: ProviderRateLimiter, helo: str):
        self.s = settings
        self.limiter = limiter
        self.helo = helo

    # ------------------------------------------------------------ helpers
    def _sleep(self, seconds: float, stop: Optional[threading.Event]) -> bool:
        """Sleep; return False if stopped."""
        if seconds <= 0:
            return not (stop and stop.is_set())
        if stop is None:
            time.sleep(seconds)
            return True
        return not stop.wait(seconds)

    def _open(self, mx: str, stop) -> Tuple[Optional[smtplib.SMTP], Optional[RcptResult]]:
        wait = self.limiter.reserve(mx)
        if not self._sleep(wait, stop):
            return None, RcptResult(CONN_FAIL, 0, "stopped")
        smtp = smtplib.SMTP(timeout=self.s.smtp_timeout)
        try:
            code, msg = smtp.connect(mx, self.s.smtp_port)
        except smtplib.SMTPConnectError as exc:
            return None, self._from_code(exc.smtp_code, _decode(exc.smtp_error))
        except (socket.timeout, OSError, smtplib.SMTPException) as exc:
            return None, RcptResult(CONN_FAIL, 0, "%s: %s" % (type(exc).__name__, exc))
        if code != 220:
            self._close(smtp)
            return None, self._from_code(code, _decode(msg))
        try:
            code, msg = smtp.ehlo(self.helo)
            if code != 250:
                code, msg = smtp.helo(self.helo)
                if code != 250:
                    self._close(smtp)
                    return None, self._from_code(code, _decode(msg))
            if self.s.use_starttls and smtp.has_extn("starttls"):
                try:
                    smtp.starttls()
                    smtp.ehlo(self.helo)
                except Exception:
                    pass
        except (socket.timeout, OSError, smtplib.SMTPException) as exc:
            self._close(smtp)
            return None, RcptResult(CONN_FAIL, 0, "%s: %s" % (type(exc).__name__, exc))
        return smtp, None

    @staticmethod
    def _from_code(code: int, msg: str) -> RcptResult:
        kind = classify(code, msg)
        if kind in (NO_MAILBOX, DISABLED, FULL, UNCLEAR):
            # a whole-session rejection is never about a mailbox
            kind = BLOCKED if _BLOCK_WORDS.search(msg) or 500 <= code < 600 else TEMP
        return RcptResult(kind, code, msg[:300])

    @staticmethod
    def _close(smtp: smtplib.SMTP) -> None:
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

    def _mail_from(self, smtp: smtplib.SMTP) -> Optional[RcptResult]:
        code, msg = smtp.mail(self.s.probe_from or "")
        if code != 250:
            text = _decode(msg)
            kind = classify(code, text)
            if kind == TEMP:
                return RcptResult(TEMP, code, text[:300])
            if kind == BLOCKED:
                return RcptResult(BLOCKED, code, text[:300])
            return RcptResult(SENDER_REJECTED, code, text[:300])
        return None

    def _rcpt(self, smtp: smtplib.SMTP, address: str) -> RcptResult:
        code, msg = smtp.rcpt(address)
        text = _decode(msg)
        return RcptResult(classify(code, text), code, text[:300])

    # ------------------------------------------------------------ main
    def probe_domain(
        self,
        domain: str,
        mx_hosts: List[str],
        emails: List[str],
        known_catch_all: Optional[bool] = None,
        stop: Optional[threading.Event] = None,
    ) -> DomainProbe:
        out = DomainProbe(catch_all=known_catch_all)
        if known_catch_all is True:
            return out

        last_error: Optional[RcptResult] = None
        for mx in mx_hosts[: max(1, self.s.max_mx_hosts_to_try)]:
            if stop and stop.is_set():
                break
            res = self._probe_on_host(domain, mx, emails, out, stop)
            if res is None:
                out.mx_used = mx
                return out
            last_error = res
            if res.kind == BLOCKED:
                out.ip_blocked = True
                break  # same operator will block us on the backup MX too
        out.session_error = last_error or RcptResult(CONN_FAIL, 0, "no MX host reachable")
        return out

    def _probe_on_host(self, domain, mx, emails, out: DomainProbe, stop) -> Optional[RcptResult]:
        """Returns None on success (results written to `out`), or an error for the session."""
        null_sender = not self.s.probe_from
        smtp, err = self._open(mx, stop)
        if err:
            return err
        try:
            err = self._mail_from(smtp)
            if err:
                return err

            # ---- catch-all test
            if out.catch_all is None:
                probe = self._rcpt(smtp, "%s@%s" % (random_local_part(domain), domain))
                if probe.kind == OK:
                    out.catch_all = True
                    return None
                if probe.kind in (TEMP, BLOCKED, CONN_FAIL):
                    return probe
                if probe.kind == UNCLEAR and _BLOCK_WORDS.search(probe.message):
                    return RcptResult(BLOCKED, probe.code, probe.message)
                out.catch_all = False

            # ---- real recipients
            sent = 1
            pending = [e for e in emails if e not in out.results]
            for addr in pending:
                if stop and stop.is_set():
                    break
                if sent >= self.s.max_rcpt_per_session:
                    self._close(smtp)
                    smtp, err = self._open(mx, stop)
                    if err:
                        for rest in [e for e in emails if e not in out.results]:
                            out.results[rest] = RcptResult(TEMP if err.kind != BLOCKED else BLOCKED, err.code, err.message)
                        return None
                    err = self._mail_from(smtp)
                    if err:
                        for rest in [e for e in emails if e not in out.results]:
                            out.results[rest] = err
                        return None
                    sent = 0
                elif null_sender and sent > 0:
                    # many servers allow only one recipient per null-sender transaction
                    smtp.rset()
                    err = self._mail_from(smtp)
                    if err:
                        for rest in [e for e in emails if e not in out.results]:
                            out.results[rest] = err
                        return None
                self._sleep(random.uniform(self.s.rcpt_delay_min, self.s.rcpt_delay_max), stop)
                out.results[addr] = self._rcpt(smtp, addr)
                sent += 1
            return None
        except smtplib.SMTPServerDisconnected as exc:
            for rest in [e for e in emails if e not in out.results]:
                out.results[rest] = RcptResult(TEMP, 0, "server disconnected: %s" % exc)
            if out.catch_all is None:
                return RcptResult(TEMP, 0, "server disconnected: %s" % exc)
            return None
        except (socket.timeout, OSError, smtplib.SMTPException) as exc:
            if out.catch_all is None:
                return RcptResult(CONN_FAIL, 0, "%s: %s" % (type(exc).__name__, exc))
            for rest in [e for e in emails if e not in out.results]:
                out.results[rest] = RcptResult(CONN_FAIL, 0, "%s: %s" % (type(exc).__name__, exc))
            return None
        finally:
            if smtp is not None:
                self._close(smtp)


def port25_open(host: str, port: int, timeout: float = 8.0) -> Tuple[bool, str]:
    """TCP connect test. A successful connect is enough; the banner is informational
    (some servers deliberately delay it)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    banner = ""
    try:
        sock.settimeout(12.0)
        banner = sock.recv(512).decode("utf-8", errors="replace").strip()
        sock.sendall(b"QUIT\r\n")
    except Exception:
        banner = "(connected, no banner yet)"
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return True, banner[:120]
