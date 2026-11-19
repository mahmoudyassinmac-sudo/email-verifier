"""Storage interface.

The engine only talks to this interface, so SQLite can later be swapped for
Supabase (Postgres) by writing one class with the same methods.
See `supabase_schema.sql` for the matching Postgres tables.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


class Storage(ABC):
    # ---------------------------------------------------------------- jobs
    @abstractmethod
    def create_job(self, **fields: Any) -> int: ...

    @abstractmethod
    def update_job(self, job_id: int, **fields: Any) -> None: ...

    @abstractmethod
    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_jobs(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def delete_job(self, job_id: int) -> None: ...

    @abstractmethod
    def find_job_by_hash(self, sha256: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def mark_interrupted_jobs(self) -> int:
        """Jobs left in 'running' after a crash become 'paused'."""

    # ---------------------------------------------------------------- emails
    @abstractmethod
    def insert_emails(self, job_id: int, rows: Iterable[Dict[str, Any]]) -> None: ...

    @abstractmethod
    def update_emails(self, updates: List[Tuple[int, Dict[str, Any]]]) -> None: ...

    @abstractmethod
    def domains_at_stage(self, job_id: int, stage: str) -> List[str]: ...

    @abstractmethod
    def emails_for_domain(self, job_id: int, domain: str, stage: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def release_due_retries(self, job_id: int, now: float) -> int:
        """Move 'retry' emails whose time has come back to the 'smtp' stage."""

    @abstractmethod
    def reset_dns_failures(self, job_id: int) -> int:
        """Send emails finalized as 'DNS lookup failed' back to the MX stage."""

    @abstractmethod
    def requeue_reason(self, job_id: int, reason_prefix: str, stage: str) -> int:
        """Send risky emails whose reason starts with `reason_prefix` back to `stage`."""

    @abstractmethod
    def next_retry_at(self, job_id: int) -> Optional[float]: ...

    @abstractmethod
    def iter_emails(self, job_id: int, statuses: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]: ...

    # ---------------------------------------------------------------- stats
    @abstractmethod
    def counts(self, job_id: int) -> Dict[str, int]: ...

    @abstractmethod
    def reason_breakdown(self, job_id: int) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def top_domains(self, job_id: int, limit: int = 25) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def recent(self, job_id: int, limit: int = 25) -> List[Dict[str, Any]]: ...

    # ---------------------------------------------------------------- domain cache
    @abstractmethod
    def get_domain(self, domain: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def upsert_domain(self, domain: str, **fields: Any) -> None: ...
