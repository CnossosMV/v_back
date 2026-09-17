from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "prepare_keycloak_mcp.sh"
).read_text(encoding="utf-8")


def test_tenant_oauth_clients_do_not_receive_admin_scope_by_default():
    assert 'MCP_GRANT_ADMIN_SCOPE="${MCP_GRANT_ADMIN_SCOPE:-false}"' in SCRIPT
    assert (
        'if [ "${scope}" = "versya.admin:ops" ] '
        '&& ! is_true "${MCP_GRANT_ADMIN_SCOPE}"; then'
    ) in SCRIPT
    assert "Ensuring administrative scope is absent from tenant OAuth client" in SCRIPT
    assert "-X DELETE" in SCRIPT
    assert "optional-client-scopes default-client-scopes" in SCRIPT


def test_dynamic_registration_keeps_security_gates_and_loopback_callbacks():
    assert "Ensuring anonymous DCR Consent Required policy exists" in SCRIPT
    assert "Ensuring anonymous DCR Max Clients policy exists" in SCRIPT
    assert 'providerId: "consent-required"' in SCRIPT
    assert 'config: {"max-clients": ["200"]}' in SCRIPT
    assert '--data-urlencode "parent=${REALM_ID}"' in SCRIPT
    assert '--arg parent "${REALM_ID}"' in SCRIPT
    assert 'MCP_DYNAMIC_REGISTRATION_TRUSTED_HOSTS:-localhost,127.0.0.1' in SCRIPT
    assert '.config["host-sending-registration-request-must-match"] = ["false"]' in SCRIPT
    assert '.config["client-uris-must-match"] = ["true"]' in SCRIPT


def test_dynamic_registration_allows_standard_oidc_and_offline_agent_sessions():
    assert 'AGENT_SESSION_SCOPES=(' in SCRIPT
    assert '"openid"' in SCRIPT
    assert '"profile"' in SCRIPT
    assert '"email"' in SCRIPT
    assert '"offline_access"' in SCRIPT
    assert '"${TENANT_SCOPES[@]}" "${AGENT_SESSION_SCOPES[@]}"' in SCRIPT
    assert "consented long-running sessions through offline_access" in SCRIPT


def test_project_foundation_write_scope_is_provisioned_for_tenant_agents():
    assert '"versya.projects:write"' in SCRIPT


def test_expected_http_conflicts_are_handled_by_the_script():
    # Calls that inspect HTTP status must not use curl -f: -f exits before the
    # script can accept an idempotent 409 response.
    assert "curl -fsS -o /tmp/kc_response.json" not in SCRIPT
    assert 'curl -sS -o "${KC_RESPONSE_FILE}"' in SCRIPT


def test_http_response_file_is_unique_per_deploy_run_and_cleaned_up():
    assert 'need_cmd mktemp' in SCRIPT
    assert 'KC_RESPONSE_FILE="$(mktemp ' in SCRIPT
    assert 'versya-kc-response.XXXXXX' in SCRIPT
    assert "trap 'rm -f \"${KC_RESPONSE_FILE}\"' EXIT" in SCRIPT
    assert "/tmp/kc_response.json" not in SCRIPT


def test_dynamic_client_scopes_carry_the_mcp_audience():
    assert (
        'ensure_audience_mapper "client-scopes/${scope_id}" "client scope ${scope}"'
        in SCRIPT
    )
    assert '.config["allowed-client-scopes"]' in SCRIPT
    assert '"${realm_url}/default-optional-client-scopes/${scope_id}"' in SCRIPT
