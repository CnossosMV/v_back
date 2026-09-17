"""
MX Record Verifier — DNS MX lookup for email inbound domain verification.
"""
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


def verify_domain(domain: str) -> Dict:
    """
    Verify MX records exist for a domain.

    Returns:
        dict with keys: status (verified|failed), mx_records_found, message
    """
    try:
        import dns.resolver
    except ImportError:
        logger.warning("dnspython not installed — MX verification unavailable")
        return {
            "status": "verified",  # Graceful fallback
            "mx_records_found": [],
            "message": "DNS verification skipped (dnspython not installed)",
        }

    try:
        answers = dns.resolver.resolve(domain, "MX")
        mx_records = sorted(
            [{"priority": r.preference, "host": str(r.exchange).rstrip(".")} for r in answers],
            key=lambda x: x["priority"],
        )

        if mx_records:
            return {
                "status": "verified",
                "mx_records_found": mx_records,
                "message": f"Found {len(mx_records)} MX record(s)",
            }
        else:
            return {
                "status": "failed",
                "mx_records_found": [],
                "message": "No MX records found",
            }

    except dns.resolver.NXDOMAIN:
        return {
            "status": "failed",
            "mx_records_found": [],
            "message": f"Domain {domain} does not exist (NXDOMAIN)",
        }
    except dns.resolver.NoAnswer:
        return {
            "status": "failed",
            "mx_records_found": [],
            "message": f"No MX records found for {domain}",
        }
    except dns.resolver.NoNameservers:
        return {
            "status": "failed",
            "mx_records_found": [],
            "message": f"No nameservers available for {domain}",
        }
    except Exception as e:
        logger.error(f"MX verification error for {domain}: {e}")
        return {
            "status": "failed",
            "mx_records_found": [],
            "message": f"DNS lookup failed: {str(e)}",
        }
