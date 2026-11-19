"""Stage 5: MX lookup (one lookup per unique domain; results cached in storage)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import dns.exception
import dns.resolver

from .config import Settings

MX_OK = "ok"                 # MX hosts found
MX_IMPLICIT = "implicit"     # no MX, but an A/AAAA record (RFC 5321 implicit MX)
MX_NXDOMAIN = "nxdomain"
MX_NULL = "null_mx"
MX_NONE = "no_mail_server"
MX_ERROR = "dns_error"       # timeout / SERVFAIL -> Unknown, retried next run


@dataclass
class MxResult:
    status: str
    hosts: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def primary(self) -> str:
        return self.hosts[0] if self.hosts else ""


class MxResolver:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._system = dns.resolver.Resolver(configure=True)
        self._system.lifetime = settings.dns_timeout
        self._system.timeout = min(3.0, settings.dns_timeout)
        self._fallback: Optional[dns.resolver.Resolver] = None
        if settings.dns_fallback_servers:
            fb = dns.resolver.Resolver(configure=False)
            fb.nameservers = list(settings.dns_fallback_servers)
            fb.lifetime = settings.dns_timeout
            fb.timeout = min(3.0, settings.dns_timeout)
            self._fallback = fb

    def resolve(self, domain: str) -> MxResult:
        static = self.settings.static_mx.get(domain)
        if static:
            return MxResult(MX_OK, list(static), "static override")
        result = self._resolve_with(self._system, domain)
        if result.status == MX_ERROR and self._fallback is not None:
            fb = self._resolve_with(self._fallback, domain)
            if fb.status != MX_ERROR:
                return fb
        return result

    @staticmethod
    def _resolve_with(resolver: dns.resolver.Resolver, domain: str) -> MxResult:
        try:
            answer = resolver.resolve(domain, "MX")
            records = sorted(
                ((r.preference, str(r.exchange).rstrip(".").lower()) for r in answer),
                key=lambda x: x[0],
            )
            hosts = [h for _, h in records if h]
            if not hosts:  # RFC 7505 null MX:  "0 ."
                return MxResult(MX_NULL, [], "null MX")
            return MxResult(MX_OK, hosts)
        except dns.resolver.NXDOMAIN:
            return MxResult(MX_NXDOMAIN, [], "NXDOMAIN")
        except dns.resolver.NoAnswer:
            pass
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            return MxResult(MX_ERROR, [], type(exc).__name__)
        except dns.exception.DNSException as exc:
            return MxResult(MX_ERROR, [], type(exc).__name__)

        # no MX records -> implicit MX via A / AAAA
        for rtype in ("A", "AAAA"):
            try:
                resolver.resolve(domain, rtype)
                return MxResult(MX_IMPLICIT, [domain], "no MX, using %s record" % rtype)
            except dns.resolver.NXDOMAIN:
                return MxResult(MX_NXDOMAIN, [], "NXDOMAIN")
            except (dns.resolver.NoAnswer,):
                continue
            except dns.exception.DNSException as exc:
                return MxResult(MX_ERROR, [], type(exc).__name__)
        return MxResult(MX_NONE, [], "no MX/A/AAAA")


def _valid_ip(text: str) -> Optional[str]:
    import ipaddress

    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


def public_ip(timeout: float = 5.0) -> Optional[str]:
    """Public IP via OpenDNS (DNS); falls back to HTTPS/HTTP echo services."""
    try:
        r = dns.resolver.Resolver(configure=False)
        r.nameservers = ["208.67.222.222", "208.67.220.220"]
        r.lifetime = timeout
        ip = _valid_ip(str(r.resolve("myip.opendns.com", "A")[0]))
        if ip:
            return ip
    except Exception:
        pass
    import urllib.request

    for url in ("https://api.ipify.org", "https://icanhazip.com", "http://api.ipify.org",
                "http://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                ip = _valid_ip(resp.read(64).decode("ascii", "ignore"))
                if ip:
                    return ip
        except Exception:
            continue
    return None


def public_ip_and_ptr(timeout: float = 5.0):
    """Return (public_ip, ptr_hostname); either may be None."""
    ip = public_ip(timeout)
    if not ip:
        return None, None
    try:
        import dns.reversename

        sysr = dns.resolver.Resolver(configure=True)
        sysr.lifetime = timeout
        ptr = str(sysr.resolve(dns.reversename.from_address(ip), "PTR")[0]).rstrip(".")
    except Exception:
        ptr = None
    return ip, ptr
