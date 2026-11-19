"""Reference data: typo fixes, role prefixes, disposable domains, big providers."""
from __future__ import annotations

import re
import threading
import urllib.request
from pathlib import Path
from typing import Optional, Set, Tuple

from .config import DATA_DIR

DISPOSABLE_URL = (
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/main/disposable_email_blocklist.conf"
)
DISPOSABLE_FILE = DATA_DIR / "disposable_domains.txt"
DISPOSABLE_ALLOW_FILE = DATA_DIR / "disposable_allowlist.txt"

# ---------------------------------------------------------------- typos
# whole-domain corrections (most common misspellings)
DOMAIN_TYPOS = {
    # gmail
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com",
    "gmaill.com": "gmail.com", "gmal.com": "gmail.com", "gnail.com": "gmail.com",
    "gmail.co": "gmail.com", "gmail.cm": "gmail.com", "gmail.om": "gmail.com",
    "gmail.comm": "gmail.com", "gmail.cmo": "gmail.com", "gmail.ocm": "gmail.com",
    "gmail.vom": "gmail.com", "gmail.xom": "gmail.com", "gmail.c": "gmail.com",
    "gmaul.com": "gmail.com", "gmali.com": "gmail.com", "gimail.com": "gmail.com",
    "gemail.com": "gmail.com", "gmeil.com": "gmail.com", "gmil.com": "gmail.com",
    "g.mail.com": "gmail.com", "gmail.com.com": "gmail.com", "gmsil.com": "gmail.com",
    "gmail.ae": "gmail.com", "gmailcom": "gmail.com",
    # hotmail
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com", "hotmai.com": "hotmail.com",
    "hotmil.com": "hotmail.com", "hotmaill.com": "hotmail.com", "hotamil.com": "hotmail.com",
    "hotmail.co": "hotmail.com", "hotmail.cm": "hotmail.com", "hotmail.comm": "hotmail.com",
    "hotmail.cmo": "hotmail.com", "hotmali.com": "hotmail.com", "htmail.com": "hotmail.com",
    "homail.com": "hotmail.com", "hotnail.com": "hotmail.com", "hormail.com": "hotmail.com",
    "hotmeil.com": "hotmail.com", "hotmail.con": "hotmail.com", "hotmailcom": "hotmail.com",
    # outlook / live
    "outlok.com": "outlook.com", "outllook.com": "outlook.com", "outloo.com": "outlook.com",
    "outlook.co": "outlook.com", "outlook.cm": "outlook.com", "outlool.com": "outlook.com",
    "otlook.com": "outlook.com", "outlookcom": "outlook.com", "oulook.com": "outlook.com",
    "live.co": "live.com", "live.cm": "live.com", "liv.com": "live.com",
    # yahoo
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com",
    "yahoo.cm": "yahoo.com", "yhoo.com": "yahoo.com", "yahho.com": "yahoo.com",
    "yaoo.com": "yahoo.com", "yahoo.comm": "yahoo.com", "yahoo.cmo": "yahoo.com",
    "yahool.com": "yahoo.com", "tahoo.com": "yahoo.com", "uahoo.com": "yahoo.com",
    # icloud
    "iclod.com": "icloud.com", "icloud.co": "icloud.com", "icoud.com": "icloud.com",
    "iclould.com": "icloud.com", "icloud.cm": "icloud.com", "icluod.com": "icloud.com",
    # aol / msn
    "aol.co": "aol.com", "aoll.com": "aol.com", "msn.co": "msn.com",
}

# TLD corrections applied to ANY domain (these TLDs do not exist)
TLD_TYPOS = {
    ".con": ".com", ".cpm": ".com", ".cmo": ".com", ".ocm": ".com", ".comm": ".com",
    ".coom": ".com", ".vom": ".com", ".xom": ".com", ".c0m": ".com", ".clm": ".com",
    ".nett": ".net", ".nte": ".net", ".ogr": ".org", ".orgg": ".org",
}

# ---------------------------------------------------------------- roles
ROLE_PREFIXES: Set[str] = {
    "info", "admin", "administrator", "sales", "support", "contact", "contactus",
    "hello", "hi", "office", "marketing", "hr", "humanresources", "jobs", "career",
    "careers", "recruitment", "recruiting", "hiring", "noreply", "no-reply",
    "donotreply", "do-not-reply", "billing", "accounts", "accounting", "finance",
    "enquiries", "enquiry", "inquiries", "inquiry", "team", "help", "helpdesk",
    "webmaster", "postmaster", "hostmaster", "abuse", "security", "service",
    "services", "customerservice", "customercare", "care", "reception", "booking",
    "bookings", "reservations", "media", "press", "pr", "legal", "compliance",
    "management", "manager", "mail", "email", "general", "feedback", "orders",
    "order", "purchasing", "procurement", "it", "tech", "operations", "ops",
    "invest", "investor", "investors", "ir", "leasing", "rentals", "lettings",
    "property", "properties", "realestate", "agent", "agents", "partners",
    "partnership", "news", "newsletter", "events", "social", "web", "website",
    "list", "subscribe", "unsubscribe", "dev", "root", "sysadmin", "office.admin",
}

# ---------------------------------------------------------------- providers
CONSUMER_PATTERNS = [
    ("Gmail", re.compile(r"^(gmail|googlemail)\.[a-z.]+$")),
    ("Outlook/Hotmail/Live", re.compile(r"^(hotmail|outlook|live|msn|windowslive|passport)\.[a-z.]+$")),
    ("Yahoo", re.compile(r"^(yahoo|ymail|rocketmail)\.[a-z.]+$")),
]

HOSTED_MX_SUFFIXES = [
    ("Google Workspace", ("google.com", "googlemail.com")),
    ("Microsoft 365", ("outlook.com", "hotmail.com", "outlook.office365.com")),
    ("Yahoo", ("yahoodns.net", "yahoo.com")),
]

_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or", "go", "sch"}


def consumer_provider(domain: str) -> Optional[str]:
    for name, rx in CONSUMER_PATTERNS:
        if rx.match(domain):
            return name
    return None


def hosted_provider(mx_host: str) -> Optional[str]:
    mx_host = (mx_host or "").lower().rstrip(".")
    for name, suffixes in HOSTED_MX_SUFFIXES:
        for suf in suffixes:
            if mx_host == suf or mx_host.endswith("." + suf):
                return name
    return None


def provider_key(mx_host: str) -> str:
    """Group MX hosts by provider so rate limits apply per provider
    (e.g. all *.mimecast.com share one limit)."""
    labels = (mx_host or "").lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# ---------------------------------------------------------------- disposable
_disposable_lock = threading.Lock()
_disposable: Optional[Set[str]] = None


def _read_domain_file(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def disposable_domains() -> Set[str]:
    global _disposable
    with _disposable_lock:
        if _disposable is None:
            _disposable = _read_domain_file(DISPOSABLE_FILE) - _read_domain_file(DISPOSABLE_ALLOW_FILE)
        return _disposable


def is_disposable(domain: str) -> bool:
    blocked = disposable_domains()
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in blocked:
            return True
    return False


def update_disposable_list(timeout: float = 30.0) -> Tuple[int, str]:
    """Download the latest open-source list from GitHub."""
    global _disposable
    with urllib.request.urlopen(DISPOSABLE_URL, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    count = sum(1 for l in text.splitlines() if l.strip() and not l.startswith("#"))
    if count < 1000:
        raise RuntimeError("Downloaded list looks too small (%d entries); keeping the old one." % count)
    DISPOSABLE_FILE.write_text(text, encoding="utf-8")
    with _disposable_lock:
        _disposable = None
    return count, str(DISPOSABLE_FILE)
