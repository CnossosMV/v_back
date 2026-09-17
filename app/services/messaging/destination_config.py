"""
Destination Config Service
Provides destination templates and configuration validation.
"""
from typing import Dict, Any, List, Optional
import json
from cryptography.fernet import Fernet
import os


class DestinationConfigService:
    """
    Manages destination configuration templates and encryption.
    """

    # Destination configuration templates
    DESTINATION_TEMPLATES = {
        'ga4': {
            'name': 'Google Analytics 4',
            'fields': {
                'measurement_id': {
                    'type': 'string',
                    'required': True,
                    'description': 'GA4 Measurement ID (G-XXXXXXXXXX)',
                    'pattern': r'^G-[A-Z0-9]+$'
                },
                'api_secret': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'API Secret for server-side events'
                },
                'debug_mode': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Enable debug mode for testing'
                },
                'dry_run': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Queue and log deliveries without calling GA4'
                },
                'default_currency': {
                    'type': 'string',
                    'required': False,
                    'default': 'BRL',
                    'description': 'Fallback currency for value events'
                }
            },
            'default_consent': ['analytics']
        },
        'meta_pixel': {
            'name': 'Meta (Facebook) Pixel',
            'fields': {
                'pixel_id': {
                    'type': 'string',
                    'required': True,
                    'description': 'Meta Pixel ID'
                },
                'access_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'Conversions API access token'
                },
                'advanced_matching': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Enable advanced matching'
                },
                'attribution_enrichment': {
                    'type': 'select',
                    'required': False,
                    'default': 'current_only',
                    'options': ['current_only', 'historical'],
                    'description': 'Use only current-event attribution or eligible contact history'
                },
                'attribution_diagnostic_only': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Resolve and log historical attribution without changing payloads'
                },
                'test_event_code': {
                    'type': 'string',
                    'required': False,
                    'description': 'Test event code for debugging'
                },
                'dry_run': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Queue and log deliveries without calling Meta'
                },
                'api_version': {
                    'type': 'string',
                    'required': False,
                    'default': 'v20.0',
                    'description': 'Meta Graph API version'
                },
                'default_currency': {
                    'type': 'string',
                    'required': False,
                    'default': 'BRL',
                    'description': 'Fallback currency for value events'
                }
            },
            'default_consent': ['marketing']
        },
        'google_ads': {
            'name': 'Google Ads',
            'fields': {
                'conversion_id': {
                    'type': 'string',
                    'required': True,
                    'description': 'Google Ads Conversion ID (AW-XXXXXXXXX)'
                },
                'conversion_label': {
                    'type': 'string',
                    'required': False,
                    'description': 'Conversion label (for specific conversions)'
                },
                'customer_id': {
                    'type': 'string',
                    'required': False,
                    'description': 'Google Ads customer ID without dashes'
                },
                'login_customer_id': {
                    'type': 'string',
                    'required': False,
                    'description': 'Optional manager account login customer ID'
                },
                'developer_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'Google Ads developer token'
                },
                'access_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'OAuth access token for Google Ads API'
                },
                'refresh_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'OAuth refresh token for Google Ads API'
                },
                'client_id': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'OAuth client ID for token refresh'
                },
                'client_secret': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'OAuth client secret for token refresh'
                },
                'default_conversion_action_id': {
                    'type': 'string',
                    'required': False,
                    'description': 'Fallback conversion action ID'
                },
                'enhanced_conversions': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Send permitted hashed email and phone identifiers'
                },
                'attribution_enrichment': {
                    'type': 'select',
                    'required': False,
                    'default': 'current_only',
                    'options': ['current_only', 'historical'],
                    'description': 'Use only current-event attribution or eligible contact history'
                },
                'attribution_diagnostic_only': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Resolve and log historical attribution without changing payloads'
                },
                'dry_run': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Queue and log deliveries without calling Google Ads'
                },
                'api_version': {
                    'type': 'string',
                    'required': False,
                    'default': 'v24',
                    'description': 'Google Ads API version'
                },
                'default_currency': {
                    'type': 'string',
                    'required': False,
                    'default': 'BRL',
                    'description': 'Fallback currency for value events'
                }
            },
            'default_consent': ['marketing']
        },
        'linkedin': {
            'name': 'LinkedIn Insight Tag',
            'fields': {
                'partner_id': {
                    'type': 'string',
                    'required': True,
                    'description': 'LinkedIn Partner ID'
                },
                'conversions_api_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'Conversions API access token'
                }
            },
            'default_consent': ['marketing']
        },
        'tiktok': {
            'name': 'TikTok Pixel',
            'fields': {
                'pixel_id': {
                    'type': 'string',
                    'required': True,
                    'description': 'TikTok Pixel ID'
                },
                'access_token': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'Events API access token'
                },
                'advanced_matching': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Send permitted hashed contact identifiers'
                },
                'attribution_enrichment': {
                    'type': 'select',
                    'required': False,
                    'default': 'current_only',
                    'options': ['current_only', 'historical'],
                    'description': 'Use only current-event attribution or eligible contact history'
                },
                'attribution_diagnostic_only': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Resolve and log historical attribution without changing payloads'
                },
                'api_version': {
                    'type': 'string',
                    'required': False,
                    'default': 'v1.3',
                    'description': 'TikTok Business API version'
                },
                'dry_run': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Queue and log deliveries without calling TikTok'
                },
                'default_currency': {
                    'type': 'string',
                    'required': False,
                    'default': 'BRL',
                    'description': 'Fallback currency for value events'
                }
            },
            'default_consent': ['marketing']
        },
        'custom': {
            'name': 'Custom Destination',
            'fields': {
                'endpoint_url': {
                    'type': 'string',
                    'required': True,
                    'description': 'Webhook endpoint URL'
                },
                'auth_header': {
                    'type': 'string',
                    'required': False,
                    'sensitive': True,
                    'description': 'Authorization header value'
                },
                'custom_headers': {
                    'type': 'object',
                    'required': False,
                    'description': 'Additional HTTP headers'
                },
                'dry_run': {
                    'type': 'boolean',
                    'required': False,
                    'default': False,
                    'description': 'Queue and log deliveries without calling the webhook'
                }
            },
            'default_consent': []
        }
    }

    def __init__(self):
        # Get encryption key from environment
        self._encryption_key = os.environ.get('ENCRYPTION_KEY')

    def _get_fernet(self) -> Optional[Fernet]:
        """Get Fernet instance for encryption."""
        if not self._encryption_key:
            return None
        try:
            return Fernet(self._encryption_key.encode())
        except Exception:
            return None

    def get_destination_template(self, destination_type: str) -> Optional[Dict[str, Any]]:
        """Get configuration template for a destination type."""
        return self.DESTINATION_TEMPLATES.get(destination_type)

    def get_all_templates(self) -> Dict[str, Any]:
        """Get all destination templates."""
        return self.DESTINATION_TEMPLATES

    def validate_destination_config(
        self,
        destination_type: str,
        config: Dict[str, Any]
    ) -> List[str]:
        """
        Validate destination configuration against template.
        Returns list of validation errors (empty if valid).
        """
        errors = []
        template = self.get_destination_template(destination_type)

        if not template:
            errors.append(f"Unknown destination type: {destination_type}")
            return errors

        fields = template.get('fields', {})

        # Check required fields
        for field_name, field_def in fields.items():
            if field_def.get('required', False):
                if field_name not in config or not config[field_name]:
                    errors.append(f"Missing required field: {field_name}")

        # Type validation
        for field_name, value in config.items():
            if field_name in fields:
                field_def = fields[field_name]
                if not field_def.get('required', False) and value in (None, ''):
                    continue
                expected_type = field_def.get('type')
                if expected_type and not self._check_type(value, expected_type):
                    errors.append(
                        f"Field '{field_name}' should be type '{expected_type}'"
                    )
                options = field_def.get('options')
                if options and value not in options:
                    errors.append(
                        f"Field '{field_name}' must be one of: {', '.join(options)}"
                    )

        return errors

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected type."""
        type_map = {
            'string': str,
            'number': (int, float),
            'integer': int,
            'boolean': bool,
            'array': list,
            'object': dict
        }
        expected = type_map.get(expected_type)
        if expected is None:
            return True
        return isinstance(value, expected)

    def encrypt_config(self, config: Dict[str, Any]) -> Optional[str]:
        """Encrypt configuration for storage."""
        fernet = self._get_fernet()
        if not fernet:
            # Fallback: store as JSON (not encrypted)
            return json.dumps(config)

        config_json = json.dumps(config)
        encrypted = fernet.encrypt(config_json.encode())
        return encrypted.decode()

    def decrypt_config(self, encrypted: str) -> Optional[Dict[str, Any]]:
        """Decrypt configuration from storage."""
        if not encrypted:
            return None

        fernet = self._get_fernet()
        if not fernet:
            # Try to parse as plain JSON (fallback)
            try:
                return json.loads(encrypted)
            except json.JSONDecodeError:
                return None

        try:
            decrypted = fernet.decrypt(encrypted.encode())
            return json.loads(decrypted.decode())
        except Exception:
            # Try to parse as plain JSON (fallback)
            try:
                return json.loads(encrypted)
            except json.JSONDecodeError:
                return None

    def get_default_consent(self, destination_type: str) -> List[str]:
        """Get default consent requirements for a destination type."""
        template = self.get_destination_template(destination_type)
        if template:
            return template.get('default_consent', [])
        return []


# Singleton instance
destination_config = DestinationConfigService()
