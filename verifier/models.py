"""Shared constants: statuses, pipeline stages, and reason texts."""
from __future__ import annotations

# final statuses (status column)
PENDING = "pending"
VALID = "valid"
RISKY = "risky"
INVALID = "invalid"
DUPLICATE = "duplicate"

STATUS_LABEL = {
    VALID: "Valid",
    RISKY: "Risky",
    INVALID: "Invalid",
    DUPLICATE: "Duplicate",
    PENDING: "Pending",
}

# pipeline position of a pending email
STAGE_MX = "mx"        # needs stage 5
STAGE_SMTP = "smtp"    # needs stage 6
STAGE_RETRY = "retry"  # waiting for a greylist / temp-error retry
STAGE_DONE = "done"

# job statuses
JOB_READY = "ready"
JOB_RUNNING = "running"
JOB_PAUSED = "paused"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"


class R:
    """Reason texts written to the `reason` column."""

    # invalid
    EMPTY = "Empty value"
    SYNTAX = "Invalid syntax: {detail}"
    DISPOSABLE = "Disposable / temporary email domain"
    NXDOMAIN = "Domain does not exist"
    NULL_MX = "Domain does not accept email (null MX)"
    NO_MAIL_SERVER = "Domain has no mail server (no MX or A record)"
    MAILBOX_NOT_FOUND = "Mailbox does not exist (SMTP {code})"
    MAILBOX_DISABLED = "Mailbox disabled (SMTP {code})"

    # risky
    CATCH_ALL = "Catch-all domain (accepts any address)"
    MAJOR_PROVIDER = "Major provider ({provider}) - not SMTP-verifiable"
    HOSTED_PROVIDER = "Hosted on {provider} - not SMTP-verifiable"
    ROLE = "Role address ({prefix}@)"
    MAILBOX_FULL = "Mailbox full (SMTP {code})"
    UNCLEAR_REJECT = "SMTP rejected, reason unclear (SMTP {code})"
    UNKNOWN_TEMP = "Unknown: greylisted / temporary error (SMTP {code})"
    UNKNOWN_TIMEOUT = "Unknown: SMTP timeout / connection failed"
    UNKNOWN_BLOCKED = "Unknown: server blocked the probe IP"
    UNKNOWN_SENDER = "Unknown: server rejected probe sender (SMTP {code})"
    UNKNOWN_DNS = "Unknown: DNS lookup failed"
    NOT_CHECKED_PORT25 = "Not SMTP-checked: outbound port 25 is blocked"
    NOT_CHECKED_DISABLED = "Not SMTP-checked: SMTP stage disabled"
    NOT_CHECKED_IP_BLOCKED = "Not SMTP-checked: probe IP blocked by many servers"

    # valid
    MAILBOX_EXISTS = "Mailbox exists (SMTP {code})"

    # duplicate
    DUPLICATE = "Duplicate of row {row}"
