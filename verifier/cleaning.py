"""Stages 1-4: cleaning, syntax, disposable, role. Pure functions, no network."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from . import lists
from .models import INVALID, PENDING, R

_INVISIBLE = re.compile("[​‌‍‎‏⁠﻿ ]")
_LOCAL_ALLOWED = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


@dataclass
class OfflineResult:
    original_email: str
    email: str = ""
    domain: str = ""
    local: str = ""
    status: str = PENDING
    reason: str = ""
    is_role: bool = False
    role_prefix: str = ""
    is_disposable: bool = False
    typo_fixed: str = ""          # "gmial.com -> gmail.com" when a fix was applied
    stage_failed: Optional[int] = None


# ---------------------------------------------------------------- stage 1
def clean(raw: str) -> Tuple[str, str]:
    """Return (cleaned_email, typo_fix_note)."""
    s = "" if raw is None else str(raw)
    s = _INVISIBLE.sub("", s).strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    # "Name <a@b.com>" -> a@b.com
    m = re.search(r"<([^<>]+@[^<>]+)>", s)
    if m:
        s = m.group(1)
    s = s.strip().strip("\"'`()[]{}<>,;:").strip()
    s = s.replace(" ", "").lower()
    # full-width @ and common separators people paste
    s = s.replace("＠", "@").replace("(at)", "@").replace("[at]", "@")
    s = s.rstrip(".")

    if s.count("@") != 1:
        return s, ""
    local, domain = s.split("@")
    domain = domain.strip(".")
    while ".." in domain:
        domain = domain.replace("..", ".")
    fixed = _fix_domain(domain)
    note = ""
    if fixed != domain:
        note = "%s -> %s" % (domain, fixed)
        domain = fixed
    return "%s@%s" % (local, domain), note


def _fix_domain(domain: str) -> str:
    if domain in lists.DOMAIN_TYPOS:
        return lists.DOMAIN_TYPOS[domain]
    for bad, good in lists.TLD_TYPOS.items():
        if domain.endswith(bad):
            candidate = domain[: -len(bad)] + good
            return lists.DOMAIN_TYPOS.get(candidate, candidate)
    return domain


# ---------------------------------------------------------------- stage 2
def syntax_error(email: str) -> Optional[str]:
    """Return None if the address is syntactically valid, else a short reason."""
    if not email:
        return "empty"
    if email.count("@") != 1:
        return "missing @" if "@" not in email else "more than one @"
    if len(email) > 254:
        return "longer than 254 characters"
    local, domain = email.split("@")
    if not local:
        return "empty local part"
    if len(local) > 64:
        return "local part longer than 64 characters"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return "misplaced dot in local part"
    if not _LOCAL_ALLOWED.match(local):
        return "illegal character in local part"
    if not domain:
        return "empty domain"
    labels = domain.split(".")
    if len(labels) < 2:
        return "domain has no TLD"
    for label in labels:
        if not _LABEL.match(label):
            return "invalid domain label"
    if not _TLD.match(labels[-1]):
        return "invalid TLD"
    return None


# ---------------------------------------------------------------- stage 4
def role_prefix(local: str) -> Optional[str]:
    base = local.split("+", 1)[0]
    if base in lists.ROLE_PREFIXES:
        return base
    # info.dubai, sales-uae, info_2 ...
    head = re.split(r"[._\-]", base, 1)[0]
    if head in lists.ROLE_PREFIXES and head not in ("it", "pr", "ir", "hi", "web", "dev"):
        return head
    return None


# ---------------------------------------------------------------- 1 -> 4
def process_offline(raw: str) -> OfflineResult:
    res = OfflineResult(original_email="" if raw is None else str(raw))

    # 1. cleaning
    email, note = clean(raw)
    res.email = email
    res.typo_fixed = note
    if not email:
        res.status, res.reason, res.stage_failed = INVALID, R.EMPTY, 1
        return res

    # 2. syntax
    err = syntax_error(email)
    if err:
        res.status, res.reason, res.stage_failed = INVALID, R.SYNTAX.format(detail=err), 2
        if "@" in email:
            res.domain = email.rsplit("@", 1)[1]
        return res
    res.local, res.domain = email.split("@")

    # 3. disposable
    if lists.is_disposable(res.domain):
        res.is_disposable = True
        res.status, res.reason, res.stage_failed = INVALID, R.DISPOSABLE, 3
        return res

    # 4. role (flag only, continues)
    prefix = role_prefix(res.local)
    if prefix:
        res.is_role = True
        res.role_prefix = prefix
    return res
