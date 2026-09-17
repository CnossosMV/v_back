from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
    encoding="utf-8"
)
SCRIPT = (ROOT / "scripts" / "prepare_keycloak_theme.sh").read_text(
    encoding="utf-8"
)
THEME = ROOT / "keycloak" / "themes" / "versya" / "login"


def test_versya_login_theme_is_complete():
    assert (THEME / "theme.properties").is_file()
    assert (THEME / "resources" / "css" / "login.css").is_file()
    assert (THEME / "resources" / "css" / "controls-v2.css").is_file()
    assert (THEME / "resources" / "css" / "locale-v3.css").is_file()
    assert (THEME / "resources" / "img" / "versya.svg").is_file()
    assert (THEME / "messages" / "messages_pt_BR.properties").is_file()
    assert (THEME / "messages" / "messages_en.properties").is_file()


def test_keycloak_mounts_and_deploys_the_versioned_theme():
    assert "./keycloak/themes:/opt/keycloak/themes:ro" in COMPOSE
    assert "scp -r keycloak/themes/versya" in DEPLOY
    assert "find /opt/customer-app/keycloak/themes/versya -type d -exec chmod 755" in DEPLOY
    assert "find /opt/customer-app/keycloak/themes/versya -type f -exec chmod 644" in DEPLOY
    assert "./scripts/prepare_keycloak_theme.sh" in DEPLOY


def test_theme_activation_is_idempotent_and_verified():
    assert "CURRENT_THEME" in SCRIPT
    assert '[ "${CURRENT_THEME}" != "${LOGIN_THEME}" ]' in SCRIPT
    assert "loginTheme: $theme" in SCRIPT
    assert "internationalizationEnabled: true" in SCRIPT
    assert 'KEYCLOAK_DEFAULT_LOCALE:-pt-BR' in SCRIPT
    assert '. + ["en", "pt-BR"] | unique' in SCRIPT
    assert "ACTIVE_THEME" in SCRIPT
    assert '[ "${ACTIVE_THEME}" != "${LOGIN_THEME}" ]' in SCRIPT
    assert "ACTIVE_I18N" in SCRIPT
    assert "ACTIVE_DEFAULT_LOCALE" in SCRIPT
