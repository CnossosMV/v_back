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
LOGIN_THEME="${KEYCLOAK_LOGIN_THEME:-versya}"
DEFAULT_LOCALE="${KEYCLOAK_DEFAULT_LOCALE:-pt-BR}"
THEME_PATH="/opt/customer-app/keycloak/themes/${LOGIN_THEME}/login/theme.properties"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command missing: $1"
    exit 1
  }
}

need_cmd curl
need_cmd jq

if [ ! -f "${THEME_PATH}" ]; then
  echo "ERROR: Keycloak login theme not found: ${THEME_PATH}"
  exit 1
fi

echo "Obtaining Keycloak admin token..."
ADMIN_TOKEN="$(
  curl -fsS \
    -X POST "${KEYCLOAK_BASE}/realms/${ADMIN_REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "client_id=admin-cli" \
    --data-urlencode "username=${ADMIN_USER}" \
    --data-urlencode "password=${ADMIN_PASSWORD}" \
    --data-urlencode "grant_type=password" \
    | jq -er '.access_token'
)"

REALM_URL="${KEYCLOAK_BASE}/admin/realms/${REALM}"
REALM_CONFIG="$(
  curl -fsS \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${REALM_URL}"
)"
CURRENT_THEME="$(jq -r '.loginTheme // ""' <<<"${REALM_CONFIG}")"
I18N_ENABLED="$(jq -r '.internationalizationEnabled // false' <<<"${REALM_CONFIG}")"
CURRENT_SUPPORTED_LOCALES="$(jq -c '.supportedLocales // []' <<<"${REALM_CONFIG}")"
SUPPORTED_LOCALES="$(
  jq -c '. + ["en", "pt-BR"] | unique' <<<"${CURRENT_SUPPORTED_LOCALES}"
)"
CURRENT_DEFAULT_LOCALE="$(jq -r '.defaultLocale // ""' <<<"${REALM_CONFIG}")"

if [ "${CURRENT_THEME}" != "${LOGIN_THEME}" ] \
  || [ "${I18N_ENABLED}" != "true" ] \
  || [ "${CURRENT_DEFAULT_LOCALE}" != "${DEFAULT_LOCALE}" ] \
  || ! jq -e 'index("en") and index("pt-BR")' <<<"${CURRENT_SUPPORTED_LOCALES}" >/dev/null; then
  echo "Activating Keycloak login theme '${LOGIN_THEME}' and Versya locales for realm '${REALM}'..."
  HTTP_STATUS="$(
    curl -sS -o /tmp/keycloak_theme_response.json -w "%{http_code}" \
      -X PUT "${REALM_URL}" \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$(
        jq -cn \
          --arg theme "${LOGIN_THEME}" \
          --arg default_locale "${DEFAULT_LOCALE}" \
          --argjson supported_locales "${SUPPORTED_LOCALES}" \
          '{
            loginTheme: $theme,
            internationalizationEnabled: true,
            supportedLocales: $supported_locales,
            defaultLocale: $default_locale
          }'
      )"
  )"
  if [ "${HTTP_STATUS}" != "204" ]; then
    echo "ERROR: could not activate Keycloak theme (HTTP ${HTTP_STATUS})"
    cat /tmp/keycloak_theme_response.json
    exit 1
  fi
else
  echo "Keycloak login theme and Versya locales are already active."
fi

ACTIVE_CONFIG="$(
  curl -fsS \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${REALM_URL}"
)"
ACTIVE_THEME="$(jq -r '.loginTheme // ""' <<<"${ACTIVE_CONFIG}")"
ACTIVE_I18N="$(jq -r '.internationalizationEnabled // false' <<<"${ACTIVE_CONFIG}")"
ACTIVE_DEFAULT_LOCALE="$(jq -r '.defaultLocale // ""' <<<"${ACTIVE_CONFIG}")"

if [ "${ACTIVE_THEME}" != "${LOGIN_THEME}" ]; then
  echo "ERROR: active Keycloak login theme is '${ACTIVE_THEME}', expected '${LOGIN_THEME}'"
  exit 1
fi

if [ "${ACTIVE_I18N}" != "true" ] || [ "${ACTIVE_DEFAULT_LOCALE}" != "${DEFAULT_LOCALE}" ]; then
  echo "ERROR: Keycloak locale configuration was not applied"
  exit 1
fi

echo "Keycloak realm '${REALM}' now uses theme '${ACTIVE_THEME}' with default locale '${ACTIVE_DEFAULT_LOCALE}'."
