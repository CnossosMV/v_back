"""
DNS Verification Service for Messaging Middleware

Verifies domain ownership by checking for a TXT record at _versya.{domain}
containing the verification token.

Supports subdomain verification by checking parent domains:
- For app.example.com, checks _versya.app.example.com first
- If not found, falls back to _versya.example.com

Example:
- Domain: example.com
- TXT Record: _versya.example.com
- Value: versya-verify=abc123xyz
"""
import dns.resolver
import dns.exception
import logging
from typing import Tuple, Optional, List

logger = logging.getLogger(__name__)

# Domains that skip DNS verification (for development)
SKIP_VERIFICATION_DOMAINS = [
    'localhost',
    '127.0.0.1',
    '0.0.0.0',
]


class DNSVerifier:
    """
    Verifies domain ownership via DNS TXT records.

    The expected TXT record format is:
    - Record name: _versya.{domain}
    - Record value: versya-verify={verification_token}

    Example for example.com with token "abc123":
    - Add TXT record: _versya.example.com
    - With value: versya-verify=abc123
    """

    def __init__(self, timeout: float = 5.0):
        """
        Initialize the DNS verifier.

        Args:
            timeout: DNS query timeout in seconds
        """
        self.timeout = timeout
        self.resolver = dns.resolver.Resolver()
        self.resolver.timeout = timeout
        self.resolver.lifetime = timeout

    def should_skip_verification(self, domain: str) -> bool:
        """
        Check if domain should skip DNS verification.

        Args:
            domain: The domain to check

        Returns:
            True if verification should be skipped (localhost, etc.)
        """
        domain_lower = domain.lower().strip()

        # Check exact matches
        if domain_lower in SKIP_VERIFICATION_DOMAINS:
            return True

        # Check if it's a localhost variant with port
        if domain_lower.startswith('localhost:'):
            return True
        if domain_lower.startswith('127.0.0.1:'):
            return True

        # Check for .local domains (development)
        if domain_lower.endswith('.local'):
            return True

        return False

    def get_txt_record_name(self, domain: str) -> str:
        """
        Get the expected TXT record name for a domain.

        Args:
            domain: The domain (e.g., "example.com")

        Returns:
            The TXT record name (e.g., "_versya.example.com")
        """
        # Remove any port number
        clean_domain = domain.split(':')[0].strip().lower()
        return f"_versya.{clean_domain}"

    def get_domains_to_check(self, domain: str) -> List[str]:
        """
        Get list of domains to check for TXT record, including parent domains.

        For subdomain verification, we check the exact domain first,
        then fall back to parent domains.

        Args:
            domain: The domain (e.g., "app.staging.example.com")

        Returns:
            List of domains to check, from most specific to least specific.
            Example: ["app.staging.example.com", "staging.example.com", "example.com"]
        """
        # Remove any port number
        clean_domain = domain.split(':')[0].strip().lower()

        domains_to_check = [clean_domain]
        parts = clean_domain.split('.')

        # Generate parent domains (need at least 2 parts for a valid domain)
        # e.g., for "app.staging.example.com":
        # - staging.example.com
        # - example.com
        while len(parts) > 2:
            parts = parts[1:]  # Remove leftmost subdomain
            parent_domain = '.'.join(parts)
            domains_to_check.append(parent_domain)

        return domains_to_check

    def get_expected_txt_value(self, verification_token: str) -> str:
        """
        Get the expected TXT record value.

        Args:
            verification_token: The verification token

        Returns:
            The expected TXT value (e.g., "versya-verify=abc123")
        """
        return f"versya-verify={verification_token}"

    def _check_txt_record(
        self,
        txt_record_name: str,
        expected_value: str
    ) -> Tuple[Optional[bool], str, Optional[str]]:
        """
        Check a single TXT record for verification.

        Args:
            txt_record_name: The TXT record name to check
            expected_value: The expected verification value

        Returns:
            Tuple of (verified: Optional[bool], message: str, found_value: Optional[str])
            - verified=True: Found and matched
            - verified=False: Found but wrong token
            - verified=None: Not found, should try next
        """
        try:
            answers = self.resolver.resolve(txt_record_name, 'TXT')

            for rdata in answers:
                txt_value = ''.join([s.decode('utf-8') if isinstance(s, bytes) else s
                                     for s in rdata.strings])
                txt_value = txt_value.strip()

                logger.debug(f"Found TXT record at {txt_record_name}: {txt_value}")

                if txt_value == expected_value:
                    return True, f"Domain verified via {txt_record_name}", txt_value

                if txt_value.startswith('versya-verify='):
                    logger.warning(
                        f"Found versya-verify TXT at {txt_record_name} but token doesn't match. "
                        f"Expected: {expected_value}, Found: {txt_value}"
                    )
                    return False, f"TXT record found at {txt_record_name} but token doesn't match", txt_value

            # TXT records exist but none match
            return None, f"TXT record at {txt_record_name} has no versya-verify value", None

        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return None, f"No TXT record at {txt_record_name}", None

        except dns.resolver.Timeout:
            logger.error(f"DNS query timeout for {txt_record_name}")
            return None, f"Timeout checking {txt_record_name}", None

        except dns.exception.DNSException as e:
            logger.error(f"DNS error for {txt_record_name}: {e}")
            return None, f"DNS error: {str(e)}", None

    def verify_domain(
        self,
        domain: str,
        verification_token: str
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Verify domain ownership via DNS TXT record.

        For subdomains, checks the exact domain first, then falls back
        to parent domains. This allows verifying example.com once
        and having all subdomains (app.example.com, etc.) verified.

        Args:
            domain: The domain to verify (e.g., "example.com" or "app.example.com")
            verification_token: The expected verification token

        Returns:
            Tuple of (verified: bool, message: str, found_value: Optional[str])
        """
        # Skip verification for development domains
        if self.should_skip_verification(domain):
            logger.info(f"Skipping DNS verification for development domain: {domain}")
            return True, "Development domain - verification skipped", None

        expected_value = self.get_expected_txt_value(verification_token)
        domains_to_check = self.get_domains_to_check(domain)

        logger.info(
            f"Verifying domain {domain}: checking {len(domains_to_check)} domain(s) "
            f"for token {verification_token}"
        )

        checked_records = []
        last_error = None

        # Try each domain (exact first, then parents)
        for check_domain in domains_to_check:
            txt_record_name = self.get_txt_record_name(check_domain)
            checked_records.append(txt_record_name)

            logger.debug(f"Checking {txt_record_name}...")

            verified, message, found_value = self._check_txt_record(txt_record_name, expected_value)

            if verified is True:
                logger.info(f"Domain {domain} verified successfully via {txt_record_name}")
                return True, message, found_value

            if verified is False:
                # Found record but wrong token - don't continue checking
                return False, message, found_value

            # verified is None - not found, continue to parent
            last_error = message

        # None of the domains had the record
        if len(domains_to_check) > 1:
            parent_domain = domains_to_check[-1]
            return False, (
                f"No TXT record found. For subdomains, you can add the record to your "
                f"main domain: _versya.{parent_domain}"
            ), None
        else:
            return False, f"No TXT record found at _versya.{domain}. Please add the DNS record and try again.", None


# Singleton instance
dns_verifier = DNSVerifier()
