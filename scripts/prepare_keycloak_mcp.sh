#!/usr/bin/env bash
set -euo pipefail

cd /opt/customer-app

if [ ! -f .env ]; then
  echo "ERROR: /opt/customer-app/.env not found"
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

REALM="${KEYCLOAK_REALM:-customer}"
KEYCLOAK_BASE="${KEYCLOAK_URL:-https://auth.versya.io}"
ADMIN_REALM="${KEYCLOAK_ADMIN_REALM:-master}"
ADMIN_USER="${KEYCLOAK_ADMIN_USER:?KEYCLOAK_ADMIN_USER is required}"
ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD is required}"

# The client whose tokens the MCP connector will use.
# For v1 this can be customer-frontend. Later, prefer a dedicated MCP OAuth client.
MCP_OAUTH_CLIENT_ID="${MCP_OAUTH_CLIENT_ID:-${KEYCLOAK_CLIENT_ID:-customer-frontend}}"

# Dedicated resource/audience expected by the backend.
MCP_AUDIENCE_CLIENT_ID="${MCP_AUDIENCE_CLIENT_ID:-versya-mcp}"

TENANT_SCOPES=(
  "versya.projects:read"
  "versya.projects:write"
  "versya.templates:read"
  "versya.templates:write"
  "versya.variables:read"
  "versya.variables:write"
  "versya.funnels:read"
  "versya.funnels:write"
  "versya.automations:read"
  "versya.automations:write"
  "versya.contacts:read"
  "versya.agents:read"
  "versya.channels:read"
  "versya.imports:read"
  "versya.imports:write"
  "versya.lifecycle:read"
  "versya.lifecycle:write"
  "versya.ingestion:read"
  "versya.ingestion:write"
  "versya.campaigns:read"
  "versya.campaigns:write"
)

# Local and persistent agents should be able to refresh without depending on
# the realm's short online-session timeout. This is an OAuth session scope,
# never a Versya product permission, and still requires user consent.
AGENT_SESSION_SCOPES=(
  "openid"
  "profile"
  "email"
  "offline_access"
)

ADMIN_SCOPES=(
  "versya.admin:ops"
)

SCOPES=("${TENANT_SCOPES[@]}" "${ADMIN_SCOPES[@]}")

# Tenant-facing OAuth clients must never receive the administrative scope by
# accident. A dedicated administrative client can opt in explicitly.
MCP_GRANT_ADMIN_SCOPE="${MCP_GRANT_ADMIN_SCOPE:-false}"

# Agent clients such as Codex use OAuth Dynamic Client Registration (DCR) and
# a loopback callback. Keep URI validation enabled, while disabling the
# impossible-to-maintain allowlist of every agent's source IP.
MCP_ENABLE_DYNAMIC_REGISTRATION="${MCP_ENABLE_DYNAMIC_REGISTRATION:-true}"
MCP_DYNAMIC_REGISTRATION_TRUSTED_HOSTS="${MCP_DYNAMIC_REGISTRATION_TRUSTED_HOSTS:-localhost,127.0.0.1}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command missing: $1"
    exit 1
  }
}

kc_get() {
  curl -fsS -H "Authorization: Bearer ${ADMIN_TOKEN}" "$@"
}

kc_post_json() {
  local url="$1"
  local body="$2"
  curl -sS -o "${KC_RESPONSE_FILE}" -w "%{http_code}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${body}" \
    "${url}"
}

kc_put_json() {
  local url="$1"
  local body="$2"
  curl -sS -o "${KC_RESPONSE_FILE}" -w "%{http_code}" \
    -X PUT \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${body}" \
    "${url}"
}

is_true() {
  case "${1,,}" in
    1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_audience_mapper() {
  local owner_path="$1"
  local owner_label="$2"
  local mapper_name="versya-mcp-audience"
  local mapper_endpoint="${realm_url}/${owner_path}/protocol-mappers/models"
  local mappers_json mapper_id mapper_configured mapper_body status

  mappers_json="$(kc_get "${mapper_endpoint}")"
  mapper_id="$(
    printf '%s' "${mappers_json}" \
    | jq -r --arg name "${mapper_name}" '.[] | select(.name == $name) | .id' \
    | head -n 1
  )"
  mapper_configured="$(
    printf '%s' "${mappers_json}" \
    | jq -r \
        --arg name "${mapper_name}" \
        --arg audience "${MCP_AUDIENCE_CLIENT_ID}" \
        '[.[] | select(
          .name == $name
          and .protocolMapper == "oidc-audience-mapper"
          and (.config["included.client.audience"] // "") == $audience
          and (.config["access.token.claim"] // "") == "true"
          and (.config["id.token.claim"] // "") == "false"
        )] | length'
  )"
  mapper_body="$(
    jq -n \
      --arg name "${mapper_name}" \
      --arg audience "${MCP_AUDIENCE_CLIENT_ID}" \
      '{
        name: $name,
        protocol: "openid-connect",
        protocolMapper: "oidc-audience-mapper",
        consentRequired: false,
        config: {
          "included.client.audience": $audience,
          "id.token.claim": "false",
          "access.token.claim": "true"
        }
      }'
  )"

  if [ -z "${mapper_id}" ]; then
    status="$(kc_post_json "${mapper_endpoint}" "${mapper_body}")"
    if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed creating audience mapper on ${owner_label}; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
  elif [ "${mapper_configured}" -gt 0 ]; then
    echo "Audience mapper on ${owner_label} is already configured."
  else
    status="$(kc_put_json "${mapper_endpoint}/${mapper_id}" "${mapper_body}")"
    if [ "${status}" != "204" ]; then
      echo "ERROR: failed updating audience mapper on ${owner_label}; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
  fi
}

load_registration_policies() {
  local policy_type="$1"
  curl -fsS -G \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    --data-urlencode "parent=${REALM_ID}" \
    --data-urlencode "type=${policy_type}" \
    "${realm_url}/components"
}

configure_dynamic_registration() {
  local policy_type="org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
  local policies_json trusted_policy trusted_id trusted_hosts_json trusted_body
  local allowed_policy allowed_id allowed_scopes_json allowed_body policy_body status

  policies_json="$(load_registration_policies "${policy_type}")"

  if [ "$(printf '%s' "${policies_json}" | jq '[.[] | select(.subType == "anonymous" and .providerId == "consent-required")] | length')" -eq 0 ]; then
    echo "Ensuring anonymous DCR Consent Required policy exists..."
    policy_body="$(
      jq -n \
        --arg parent "${REALM_ID}" \
        --arg provider_type "${policy_type}" \
        '{
          name: "Consent Required",
          parentId: $parent,
          providerId: "consent-required",
          providerType: $provider_type,
          subType: "anonymous",
          config: {}
        }'
    )"
    status="$(kc_post_json "${realm_url}/components" "${policy_body}")"
    if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed creating anonymous DCR Consent Required policy; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
    policies_json="$(load_registration_policies "${policy_type}")"
  fi

  if [ "$(printf '%s' "${policies_json}" | jq '[.[] | select(.subType == "anonymous" and .providerId == "max-clients")] | length')" -eq 0 ]; then
    echo "Ensuring anonymous DCR Max Clients policy exists..."
    policy_body="$(
      jq -n \
        --arg parent "${REALM_ID}" \
        --arg provider_type "${policy_type}" \
        '{
          name: "Max Clients Limit",
          parentId: $parent,
          providerId: "max-clients",
          providerType: $provider_type,
          subType: "anonymous",
          config: {"max-clients": ["200"]}
        }'
    )"
    status="$(kc_post_json "${realm_url}/components" "${policy_body}")"
    if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed creating anonymous DCR Max Clients policy; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
    policies_json="$(load_registration_policies "${policy_type}")"
  fi

  trusted_policy="$(
    printf '%s' "${policies_json}" \
    | jq -c '[.[] | select(.subType == "anonymous" and .providerId == "trusted-hosts")][0] // empty'
  )"
  trusted_id="$(printf '%s' "${trusted_policy}" | jq -r '.id // empty')"
  if [ -z "${trusted_id}" ]; then
    echo "ERROR: anonymous Trusted Hosts registration policy not found"
    exit 1
  fi

  trusted_hosts_json="$(
    printf '%s' "${MCP_DYNAMIC_REGISTRATION_TRUSTED_HOSTS}" \
    | jq -R 'split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))'
  )"
  trusted_body="$(
    printf '%s' "${trusted_policy}" \
    | jq \
        --argjson hosts "${trusted_hosts_json}" \
        '.config["trusted-hosts"] = (((.config["trusted-hosts"] // []) + $hosts) | unique)
         | .config["host-sending-registration-request-must-match"] = ["false"]
         | .config["client-uris-must-match"] = ["true"]'
  )"
  status="$(kc_put_json "${realm_url}/components/${trusted_id}" "${trusted_body}")"
  if [ "${status}" != "204" ]; then
    echo "ERROR: failed configuring anonymous DCR Trusted Hosts; HTTP ${status}"
    cat "${KC_RESPONSE_FILE}"
    exit 1
  fi

  allowed_policy="$(
    printf '%s' "${policies_json}" \
    | jq -c '[.[] | select(.subType == "anonymous" and .providerId == "allowed-client-templates")][0] // empty'
  )"
  allowed_id="$(printf '%s' "${allowed_policy}" | jq -r '.id // empty')"
  if [ -z "${allowed_id}" ]; then
    echo "ERROR: anonymous Allowed Client Scopes registration policy not found"
    exit 1
  fi

  allowed_scopes_json="$(
    printf '%s\n' "${TENANT_SCOPES[@]}" "${AGENT_SESSION_SCOPES[@]}" \
    | jq -R . \
    | jq -s .
  )"
  allowed_body="$(
    printf '%s' "${allowed_policy}" \
    | jq \
        --argjson scopes "${allowed_scopes_json}" \
        '.config["allowed-client-scopes"] = (((.config["allowed-client-scopes"] // []) + $scopes) | unique)
         | .config["allow-default-scopes"] = ["true"]'
  )"
  status="$(kc_put_json "${realm_url}/components/${allowed_id}" "${allowed_body}")"
  if [ "${status}" != "204" ]; then
    echo "ERROR: failed configuring anonymous DCR Allowed Client Scopes; HTTP ${status}"
    cat "${KC_RESPONSE_FILE}"
    exit 1
  fi

  echo "Anonymous DCR is limited to trusted callback hosts: ${MCP_DYNAMIC_REGISTRATION_TRUSTED_HOSTS}"
  echo "Anonymous DCR permits consented long-running sessions through offline_access."
}

need_cmd curl
need_cmd jq
need_cmd mktemp

# A fixed /tmp response path can be left owned by a different deployment user
# or collide with another run, causing curl error 23 before the HTTP status can
# be inspected. Keep every invocation isolated and clean it up on all exits.
KC_RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/versya-kc-response.XXXXXX")"
trap 'rm -f "${KC_RESPONSE_FILE}"' EXIT

echo "Getting Keycloak admin token for realm ${ADMIN_REALM}..."
ADMIN_TOKEN="$(
  curl -fsS \
    -d "client_id=admin-cli" \
    -d "username=${ADMIN_USER}" \
    -d "password=${ADMIN_PASSWORD}" \
    -d "grant_type=password" \
    "${KEYCLOAK_BASE}/realms/${ADMIN_REALM}/protocol/openid-connect/token" \
  | jq -r ".access_token"
)"

if [ -z "${ADMIN_TOKEN}" ] || [ "${ADMIN_TOKEN}" = "null" ]; then
  echo "ERROR: could not obtain admin token"
  exit 1
fi

realm_url="${KEYCLOAK_BASE}/admin/realms/${REALM}"

echo "Checking realm ${REALM}..."
REALM_ID="$(kc_get "${realm_url}" | jq -r '.id // empty')"
if [ -z "${REALM_ID}" ]; then
  echo "ERROR: could not resolve realm ID for ${REALM}"
  exit 1
fi

echo "Ensuring resource/audience client ${MCP_AUDIENCE_CLIENT_ID} exists..."
aud_client_uuid="$(
  kc_get "${realm_url}/clients?clientId=${MCP_AUDIENCE_CLIENT_ID}" \
  | jq -r ".[0].id // empty"
)"

if [ -z "${aud_client_uuid}" ]; then
  body="$(
    jq -n \
      --arg clientId "${MCP_AUDIENCE_CLIENT_ID}" \
      '{
        clientId: $clientId,
        name: "Versya MCP Resource",
        enabled: true,
        protocol: "openid-connect",
        publicClient: false,
        bearerOnly: false,
        serviceAccountsEnabled: false,
        standardFlowEnabled: false,
        directAccessGrantsEnabled: false,
        implicitFlowEnabled: false
      }'
  )"
  status="$(kc_post_json "${realm_url}/clients" "${body}")"
  if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
    echo "ERROR: failed creating ${MCP_AUDIENCE_CLIENT_ID}; HTTP ${status}"
    cat "${KC_RESPONSE_FILE}"
    exit 1
  fi
  aud_client_uuid="$(
    kc_get "${realm_url}/clients?clientId=${MCP_AUDIENCE_CLIENT_ID}" \
    | jq -r ".[0].id // empty"
  )"
fi

if [ -z "${aud_client_uuid}" ]; then
  echo "ERROR: could not resolve ${MCP_AUDIENCE_CLIENT_ID} UUID"
  exit 1
fi

echo "Resolving OAuth client ${MCP_OAUTH_CLIENT_ID}..."
oauth_client_uuid="$(
  kc_get "${realm_url}/clients?clientId=${MCP_OAUTH_CLIENT_ID}" \
  | jq -r ".[0].id // empty"
)"

if [ -z "${oauth_client_uuid}" ]; then
  if [ "${MCP_OAUTH_CLIENT_ID}" = "claude-mcp" ]; then
    echo "OAuth client claude-mcp not found; creating public PKCE client for Claude..."
    body="$(
      jq -n \
        '{
          clientId: "claude-mcp",
          name: "Claude MCP",
          enabled: true,
          protocol: "openid-connect",
          publicClient: true,
          bearerOnly: false,
          standardFlowEnabled: true,
          directAccessGrantsEnabled: false,
          serviceAccountsEnabled: false,
          implicitFlowEnabled: false,
          redirectUris: [
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback"
          ],
          webOrigins: [
            "https://claude.ai",
            "https://claude.com"
          ],
          attributes: {
            "pkce.code.challenge.method": "S256"
          }
        }'
    )"
    status="$(kc_post_json "${realm_url}/clients" "${body}")"
    if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed creating claude-mcp; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
    oauth_client_uuid="$(
      kc_get "${realm_url}/clients?clientId=${MCP_OAUTH_CLIENT_ID}" \
      | jq -r ".[0].id // empty"
    )"
  else
    echo "ERROR: OAuth client not found: ${MCP_OAUTH_CLIENT_ID}"
    exit 1
  fi
fi

if [ -z "${oauth_client_uuid}" ]; then
  echo "ERROR: could not resolve OAuth client UUID: ${MCP_OAUTH_CLIENT_ID}"
  exit 1
fi

echo "Ensuring versya.* client scopes exist..."
all_client_scopes="$(kc_get "${realm_url}/client-scopes")"
for scope in "${SCOPES[@]}"; do
  scope_id="$(
    echo "${all_client_scopes}" \
    | jq -r --arg name "${scope}" '.[] | select(.name == $name) | .id' \
    | head -n 1
  )"

  if [ -z "${scope_id}" ]; then
    body="$(
      jq -n \
        --arg name "${scope}" \
        '{
          name: $name,
          description: ("Versya MCP scope " + $name),
          protocol: "openid-connect",
          attributes: {
            "include.in.token.scope": "true",
            "display.on.consent.screen": "true",
            "consent.screen.text": $name
          }
        }'
    )"
    status="$(kc_post_json "${realm_url}/client-scopes" "${body}")"
    if [ "${status}" != "201" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed creating client scope ${scope}; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi

    all_client_scopes="$(kc_get "${realm_url}/client-scopes")"
    scope_id="$(
      echo "${all_client_scopes}" \
      | jq -r --arg name "${scope}" '.[] | select(.name == $name) | .id' \
      | head -n 1
    )"
  fi

  if [ -z "${scope_id}" ]; then
    echo "ERROR: could not resolve client scope ${scope}"
    exit 1
  fi

  if [ "${scope}" != "versya.admin:ops" ]; then
    ensure_audience_mapper "client-scopes/${scope_id}" "client scope ${scope}"

    echo "Ensuring realm-optional scope ${scope} is available to new OAuth clients..."
    status="$(
      curl -sS -o "${KC_RESPONSE_FILE}" -w "%{http_code}" \
        -X PUT \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        "${realm_url}/default-optional-client-scopes/${scope_id}"
    )"
    if [ "${status}" != "204" ] && [ "${status}" != "409" ]; then
      echo "ERROR: failed assigning realm-optional scope ${scope}; HTTP ${status}"
      cat "${KC_RESPONSE_FILE}"
      exit 1
    fi
  fi

  if [ "${scope}" = "versya.admin:ops" ] && ! is_true "${MCP_GRANT_ADMIN_SCOPE}"; then
    echo "Ensuring administrative scope is absent from tenant OAuth client ${MCP_OAUTH_CLIENT_ID}..."
    for scope_assignment_path in optional-client-scopes default-client-scopes; do
      status="$(
        curl -sS -o "${KC_RESPONSE_FILE}" -w "%{http_code}" \
          -X DELETE \
          -H "Authorization: Bearer ${ADMIN_TOKEN}" \
          "${realm_url}/clients/${oauth_client_uuid}/${scope_assignment_path}/${scope_id}"
      )"
      if [ "${status}" != "204" ] && [ "${status}" != "404" ]; then
        echo "ERROR: failed removing administrative scope from ${scope_assignment_path}; HTTP ${status}"
        cat "${KC_RESPONSE_FILE}"
        exit 1
      fi
    done
    continue
  fi

  echo "Ensuring optional scope ${scope} is assigned to ${MCP_OAUTH_CLIENT_ID}..."
  status="$(
    curl -sS -o "${KC_RESPONSE_FILE}" -w "%{http_code}" \
      -X PUT \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      "${realm_url}/clients/${oauth_client_uuid}/optional-client-scopes/${scope_id}"
  )"
  if [ "${status}" != "204" ] && [ "${status}" != "409" ]; then
    echo "ERROR: failed assigning optional scope ${scope}; HTTP ${status}"
    cat "${KC_RESPONSE_FILE}"
    exit 1
  fi
done

echo "Ensuring audience mapper adds aud=${MCP_AUDIENCE_CLIENT_ID} to the configured OAuth client..."
ensure_audience_mapper "clients/${oauth_client_uuid}" "OAuth client ${MCP_OAUTH_CLIENT_ID}"

if is_true "${MCP_ENABLE_DYNAMIC_REGISTRATION}"; then
  echo "Configuring least-privilege OAuth Dynamic Client Registration for agents..."
  configure_dynamic_registration
fi

echo
echo "Verification:"
echo "OAuth client: ${MCP_OAUTH_CLIENT_ID}"
echo "Expected backend MCP_AUTH_AUDIENCE: ${MCP_AUDIENCE_CLIENT_ID}"
echo "Optional versya.* scopes assigned:"
kc_get "${realm_url}/clients/${oauth_client_uuid}/optional-client-scopes" \
  | jq -r '.[] | select(.name | startswith("versya.")) | " - " + .name' \
  | sort

if ! is_true "${MCP_GRANT_ADMIN_SCOPE}"; then
  echo "Admin scope is intentionally omitted from the tenant OAuth client."
fi

echo "Audience mappers:"
kc_get "${realm_url}/clients/${oauth_client_uuid}/protocol-mappers/models" \
  | jq -r '.[] | select(.protocolMapper == "oidc-audience-mapper") | " - " + .name + " => " + (.config["included.client.audience"] // "")'

echo
echo "Done. Now set production backend env:"
echo "  MCP_ENABLED=true"
echo "  MCP_ADMIN_ENABLED=false"
echo "  MCP_PUBLIC_BASE_URL=https://api.versya.io"
echo "  MCP_AUTH_ISSUER=${KEYCLOAK_BASE}/realms/${REALM}"
echo "  MCP_AUTH_AUDIENCE=${MCP_AUDIENCE_CLIENT_ID}"
echo "  MCP_JWKS_URL=http://customer-keycloak-prod:8080/realms/${REALM}/protocol/openid-connect/certs"
echo "  MCP_ALLOWED_HOSTS=api.versya.io,api.versya.io:*"
echo "  MCP_ALLOWED_ORIGINS=<origins explicitly allowed by each connector installation>"
echo "  MCP_READONLY_SQL_TIMEOUT_MS=5000"
