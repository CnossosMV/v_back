# Versya Audience Sync Foundation

Audience sync is a separate product surface from destination event delivery/CAPI. Event delivery sends conversions. Audience sync maintains provider-side customer lists for retargeting, suppression, and seed/lookalike workflows.

## Consent Contract

Tenant apps own the truth for consent and send Versya evidence through `/api/v1/consent`, `/api/v1/identify`, `/api/v1/track`, or backend event ingestion.

Supported ads scopes:

- `ad_user_data`: user data may be used for ads/customer-list processing.
- `ad_personalization`: user may be included in personalized advertising audiences.

Example payload:

```json
{
  "user_id": "customer-123",
  "marketing": true,
  "analytics": true,
  "ads_consent": {
    "ad_user_data": {
      "status": "granted",
      "source": "checkout_checkbox",
      "policy_version": "2026-05",
      "evidence_id": "consent_abc123",
      "page_url": "https://customer.com/signup"
    },
    "ad_personalization": {
      "status": "granted",
      "source": "checkout_checkbox",
      "policy_version": "2026-05",
      "evidence_id": "consent_abc123"
    }
  }
}
```

Versya stores immutable `MessagingAdsConsentEvidence` rows and mirrors the latest state in `MessagingUser.consent_channels.ads`. Missing consent is treated as not granted.

## Identifier Handling

Versya does not reuse its internal salted identity hashes for audience uploads. Provider uploads require normalized, unsalted SHA-256 identifiers generated at sync time:

- email: lowercased and trimmed, then SHA-256.
- phone: digits-only phone/E.164, then SHA-256.
- external ID: lowercased and trimmed, then SHA-256.

Sync jobs and item logs store only counts, flags showing which identifier classes were present, provider IDs, and provider responses. Raw PII is not stored in audience sync logs.

## Provider Notes

Meta V1 uses an existing Meta destination credential plus a provider Custom Audience ID. It uploads hashed `EMAIL`, `PHONE`, and `EXTERN_ID` identifiers.

Google V1 uses an existing Google Ads destination credential plus a Customer Match user list ID. It creates an Offline User Data Job with `adUserData=GRANTED` and `adPersonalization=GRANTED` only for users who have both Versya ads consent scopes granted. Google list eligibility, policy acceptance, and processing delays remain Google-side requirements.

TikTok uses the same internal provider adapter contract but remains a V2 adapter.

## Rollout Notes

- Only identified active users are eligible in V1.
- Anonymous visitors are excluded until merged into a `MessagingUser`.
- Users with missing identifiers, missing ads consent, global opt-out, blocked status, sandbox status, merged/deleted status, or rule mismatch are excluded.
- Audience rules can target tags, segment name/rule, lifecycle stage, user properties, performed events, and campaign origin.
- Dry-run sync should be used before live provider uploads during dogfooding.
