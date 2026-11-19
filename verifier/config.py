"""Runtime settings for the verifier.

Values come from (highest priority first):
  1. real environment variables
  2. the `.env` file in the project root
  3. the defaults below
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = BASE_DIR / "runtime"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw is not None else default


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _static_mx(raw: str) -> Dict[str, List[str]]:
    """Parse STATIC_MX="domain1=host1|host2;domain2=host3" (testing only)."""
    out: Dict[str, List[str]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        dom, hosts = part.split("=", 1)
        out[dom.strip().lower()] = [h.strip() for h in hosts.split("|") if h.strip()]
    return out


@dataclass
class Settings:
    # storage
    db_path: str = str(RUNTIME_DIR / "verifier.db")
    jobs_dir: str = str(RUNTIME_DIR / "jobs")

    # SMTP stage
    smtp_enabled: bool = True
    smtp_port: int = 25
    smtp_timeout: float = 20.0
    probe_helo: str = ""            # empty = auto (PTR of public IP, else address literal)
    probe_from: str = ""            # empty = null sender  MAIL FROM:<>
    use_starttls: bool = False
    preflight_host: str = "gmail-smtp-in.l.google.com"

    # concurrency & politeness
    smtp_workers: int = 4
    dns_workers: int = 16
    per_mx_interval: float = 8.0     # min seconds between connections to the same mail provider
    per_mx_jitter: float = 7.0       # + random 0..jitter seconds
    rcpt_delay_min: float = 0.5
    rcpt_delay_max: float = 1.5
    max_rcpt_per_session: int = 10
    max_mx_hosts_to_try: int = 2

    # retries (greylisting / temporary errors)
    retry_delay: float = 600.0       # seconds before re-probing a temp-failed address
    max_retries: int = 2

    # circuit breaker: stop SMTP if too many servers block our IP
    block_window: int = 20
    block_ratio: float = 0.5

    # provider policy
    skip_hosted_business: bool = True   # Google Workspace / Microsoft 365 / Yahoo hosted business domains

    # caches
    mx_cache_days: int = 7
    catch_all_cache_days: int = 14

    # DNS
    dns_timeout: float = 6.0
    dns_fallback_servers: List[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])

    # dashboard server
    host: str = "127.0.0.1"
    port: int = 8765

    # testing hook: domain -> list of MX hosts (bypasses DNS)
    static_mx: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, env_file: Optional[Path] = None) -> "Settings":
        _load_env_file(env_file or (BASE_DIR / ".env"))
        s = cls(
            db_path=_str("DB_PATH", str(RUNTIME_DIR / "verifier.db")),
            jobs_dir=_str("JOBS_DIR", str(RUNTIME_DIR / "jobs")),
            smtp_enabled=_bool("SMTP_ENABLED", True),
            smtp_port=_int("SMTP_PORT", 25),
            smtp_timeout=_float("SMTP_TIMEOUT", 20.0),
            probe_helo=_str("PROBE_HELO", ""),
            probe_from=_str("PROBE_FROM", ""),
            use_starttls=_bool("USE_STARTTLS", False),
            preflight_host=_str("PREFLIGHT_HOST", "gmail-smtp-in.l.google.com"),
            smtp_workers=_int("SMTP_WORKERS", 4),
            dns_workers=_int("DNS_WORKERS", 16),
            per_mx_interval=_float("PER_MX_INTERVAL", 8.0),
            per_mx_jitter=_float("PER_MX_JITTER", 7.0),
            rcpt_delay_min=_float("RCPT_DELAY_MIN", 0.5),
            rcpt_delay_max=_float("RCPT_DELAY_MAX", 1.5),
            max_rcpt_per_session=_int("MAX_RCPT_PER_SESSION", 10),
            max_mx_hosts_to_try=_int("MAX_MX_HOSTS_TO_TRY", 2),
            retry_delay=_float("RETRY_DELAY", 600.0),
            max_retries=_int("MAX_RETRIES", 2),
            block_window=_int("BLOCK_WINDOW", 20),
            block_ratio=_float("BLOCK_RATIO", 0.5),
            skip_hosted_business=_bool("SKIP_HOSTED_BUSINESS", True),
            mx_cache_days=_int("MX_CACHE_DAYS", 7),
            catch_all_cache_days=_int("CATCH_ALL_CACHE_DAYS", 14),
            dns_timeout=_float("DNS_TIMEOUT", 6.0),
            dns_fallback_servers=_list("DNS_FALLBACK_SERVERS", ["1.1.1.1", "8.8.8.8"]),
            host=_str("HOST", "127.0.0.1"),
            port=_int("PORT", 8765),
            static_mx=_static_mx(_str("STATIC_MX", "")),
        )
        Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(s.jobs_dir).mkdir(parents=True, exist_ok=True)
        return s

    def public_dict(self) -> Dict[str, object]:
        """Settings safe to show in the dashboard."""
        return {
            "smtp_enabled": self.smtp_enabled,
            "smtp_port": self.smtp_port,
            "probe_helo": self.probe_helo or "(auto)",
            "probe_from": self.probe_from or "<> (null sender)",
            "smtp_workers": self.smtp_workers,
            "per_mx_interval": self.per_mx_interval,
            "per_mx_jitter": self.per_mx_jitter,
            "max_rcpt_per_session": self.max_rcpt_per_session,
            "retry_delay_min": round(self.retry_delay / 60, 1),
            "max_retries": self.max_retries,
            "skip_hosted_business": self.skip_hosted_business,
        }
