"""Tracking-domain helpers for the first-party ads gateway layer."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import dns.exception
import dns.resolver
import httpx
from sqlalchemy.orm import Session

from app.models.messaging import MessagingDomain, MessagingTrackingDomain


DEFAULT_PROXY_PATHS = [
    "/sdk/versya-messaging.js",
    "/api/v1/track",
    "/api/v1/page",
    "/api/v1/identify",
    "/api/v1/verify-install",
    "/api/v1/consent",
    "/api/v1/alias",
    "/api/v1/visitor-data",
    "/api/v1/recover-identity",
    "/api/v1/config",
    "/track",
    "/page",
    "/identify",
]

PUBLIC_SECOND_LEVEL_SUFFIXES = {
    "ac", "co", "com", "edu", "gov", "net", "org",
}


def normalize_hostname(value: str) -> str:
    hostname = (value or "").strip().lower()
    hostname = hostname.replace("https://", "").replace("http://", "")
    hostname = hostname.split("/")[0].split(":")[0].strip(".")
    if not hostname or "." not in hostname:
        raise ValueError("Tracking hostname must be a real subdomain, e.g. track.example.com")
    return hostname


def parent_cookie_domain(hostname: str) -> Optional[str]:
    parts = hostname.split(".")
    if len(parts) < 3:
        return None
    if len(parts) >= 4 and len(parts[-1]) == 2 and parts[-2] in PUBLIC_SECOND_LEVEL_SUFFIXES:
        return "." + ".".join(parts[-3:])
    return "." + ".".join(parts[-2:])


def tracking_cname_target() -> str:
    return os.getenv("VERSYA_TRACKING_CNAME_TARGET", "tracking.versya.io").strip()


def new_verification_token() -> str:
    return "trk_" + secrets.token_urlsafe(24)


def dns_instructions(domain: MessagingTrackingDomain) -> Dict[str, str]:
    return {
        "cname_name": domain.hostname,
        "cname_target": domain.cname_target,
        "txt_name": f"_versya.{domain.hostname}",
        "txt_value": f"versya-tracking-verify={domain.verification_token}",
    }


def edge_config_for_domain(domain: MessagingTrackingDomain) -> Dict[str, Any]:
    messaging_domain = domain.domain
    if not messaging_domain:
        raise ValueError("Tracking domain is not linked to a MessagingDomain/write key")
    api_origin = os.getenv("API_BASE_URL", os.getenv("VITE_API_BASE_URL", "http://localhost:8001")).rstrip("/")
    # The tracking Worker proxies /sdk/version and /sdk/versya-messaging.js.
    # These endpoints are served by the backend; SDK_CDN_URL may point at the
    # frontend/CDN shell and would make the proxy return HTML instead of SDK JS.
    sdk_origin = (os.getenv("TRACKING_SDK_ORIGIN") or api_origin).rstrip("/")
    return {
        "project_id": domain.project_id,
        "tracking_domain_id": domain.id,
        "hostname": domain.hostname,
        "write_key": messaging_domain.write_key,
        "api_origin": api_origin,
        "sdk_origin": sdk_origin,
        "cookie_domain": parent_cookie_domain(domain.hostname),
        "cookie_keeper_enabled": domain.cookie_keeper_enabled,
        "config_version": domain.config_version,
        "proxy_paths": DEFAULT_PROXY_PATHS,
    }


def verify_cname(hostname: str, expected_target: str) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        answers = dns.resolver.resolve(hostname, "CNAME")
        found = str(answers[0].target).rstrip(".").lower() if answers else None
        expected = expected_target.rstrip(".").lower()
        return found == expected, found, None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False, None, "No CNAME record found"
    except dns.resolver.Timeout:
        return False, None, "DNS lookup timed out"
    except dns.exception.DNSException as exc:
        return False, None, str(exc)


def verify_tracking_txt(hostname: str, token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    record_name = f"_versya.{hostname}"
    expected = f"versya-tracking-verify={token}"
    try:
        answers = dns.resolver.resolve(record_name, "TXT")
        for answer in answers:
            value = "".join(part.decode("utf-8") if isinstance(part, bytes) else str(part) for part in answer.strings)
            if value == expected:
                return True, value, None
        return False, None, "TXT record found, but value did not match"
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False, None, "No TXT record found"
    except dns.resolver.Timeout:
        return False, None, "DNS lookup timed out"
    except dns.exception.DNSException as exc:
        return False, None, str(exc)


@dataclass
class CloudflareResult:
    configured: bool
    success: bool
    message: str
    custom_hostname_id: Optional[str] = None
    ssl_status: Optional[str] = None
    proxy_status: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class CloudflareTrackingService:
    """Small adapter around Cloudflare for SaaS custom hostnames.

    If Cloudflare env vars are not configured, the service returns a graceful
    "not configured" result so dogfood/manual DNS setup can still be tested.
    """

    def __init__(self) -> None:
        self.api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
        self.zone_id = os.getenv("CLOUDFLARE_ZONE_ID") or os.getenv("CF_ZONE_ID")
        self.base_url = "https://api.cloudflare.com/client/v4"

    @property
    def configured(self) -> bool:
        return bool(self.api_token and self.zone_id)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def provision(self, domain: MessagingTrackingDomain) -> CloudflareResult:
        if not self.configured:
            return CloudflareResult(
                configured=False,
                success=False,
                message="Cloudflare managed hostnames are not configured for this environment.",
                details={"missing": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"]},
            )

        if domain.cloudflare_custom_hostname_id:
            return self.sync(domain)

        payload = {
            "hostname": domain.hostname,
            "ssl": {
                "method": "txt",
                "type": "dv",
                "settings": {
                    "http2": "on",
                    "min_tls_version": "1.2",
                    "tls_1_3": "on",
                },
            },
        }
        url = f"{self.base_url}/zones/{self.zone_id}/custom_hostnames"
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=self._headers(), json=payload)
            data = response.json()
        except Exception as exc:
            return CloudflareResult(True, False, f"Cloudflare request failed: {exc}")

        if not data.get("success"):
            return CloudflareResult(True, False, "Cloudflare custom hostname creation failed", details=data)

        result = data.get("result") or {}
        ssl = result.get("ssl") or {}
        return CloudflareResult(
            configured=True,
            success=True,
            message="Cloudflare custom hostname provisioned.",
            custom_hostname_id=result.get("id"),
            ssl_status=ssl.get("status") or "pending",
            proxy_status=result.get("status") or "pending",
            details=result,
        )

    def sync(self, domain: MessagingTrackingDomain) -> CloudflareResult:
        if not self.configured:
            return CloudflareResult(False, False, "Cloudflare managed hostnames are not configured.")
        if not domain.cloudflare_custom_hostname_id:
            return CloudflareResult(True, False, "No Cloudflare custom hostname ID to sync.")

        url = f"{self.base_url}/zones/{self.zone_id}/custom_hostnames/{domain.cloudflare_custom_hostname_id}"
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(url, headers=self._headers())
            data = response.json()
        except Exception as exc:
            return CloudflareResult(True, False, f"Cloudflare request failed: {exc}")

        if not data.get("success"):
            return CloudflareResult(True, False, "Cloudflare custom hostname sync failed", details=data)

        result = data.get("result") or {}
        ssl = result.get("ssl") or {}
        return CloudflareResult(
            configured=True,
            success=True,
            message="Cloudflare custom hostname synced.",
            custom_hostname_id=result.get("id"),
            ssl_status=ssl.get("status") or "pending",
            proxy_status=result.get("status") or "pending",
            details=result,
        )


def resolve_domain_for_tracking(
    db: Session,
    *,
    project_id: int,
    domain_id: Optional[int],
) -> Optional[MessagingDomain]:
    if domain_id:
        return db.query(MessagingDomain).filter(
            MessagingDomain.id == domain_id,
            MessagingDomain.project_id == project_id,
            MessagingDomain.is_active == True,
        ).first()
    return db.query(MessagingDomain).filter(
        MessagingDomain.project_id == project_id,
        MessagingDomain.is_active == True,
    ).order_by(MessagingDomain.is_verified.desc(), MessagingDomain.created_at.asc()).first()


cloudflare_tracking_service = CloudflareTrackingService()
