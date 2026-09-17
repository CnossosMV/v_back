"""
Read-only audit: count secret columns whose values are NOT Fernet ciphertext.

Rows written while ENCRYPTION_KEY was unset were stored in plaintext by the
old code; after the fail-closed encryption change those rows can no longer be
decrypted and the affected connections must be reconnected. Run this BEFORE
deploying to know the blast radius.

Usage (inside the backend container):
    python -m scripts.audit_plaintext_secrets

Prints per-column totals only — never prints secret values.
"""
import base64
import binascii
import sys

from app.database import SessionLocal
from sqlalchemy import text


# (table, id column, secret column)
SECRET_COLUMNS = [
    ("meta_page_connections", "id", "page_access_token_enc"),
    ("meta_oauth_sessions", "id", "user_token_enc"),
    ("whatsapp_instances", "id", "meta_access_token_enc"),
    ("whatsapp_instances", "id", "meta_app_secret_enc"),
]


def looks_like_fernet(value: str) -> bool:
    """Fernet tokens are urlsafe-base64 and start with version byte 0x80 ('gAAAA...')."""
    if not value or not value.startswith("gAAAA"):
        return False
    try:
        base64.urlsafe_b64decode(value.encode())
        return True
    except (binascii.Error, ValueError):
        return False


def main() -> int:
    db = SessionLocal()
    exit_code = 0
    try:
        for table, id_col, col in SECRET_COLUMNS:
            rows = db.execute(
                text(f"SELECT {id_col}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''")
            ).fetchall()
            plaintext_ids = [row[0] for row in rows if not looks_like_fernet(row[1])]
            status = "OK" if not plaintext_ids else "PLAINTEXT FOUND"
            print(f"{table}.{col}: {len(rows)} non-empty, {len(plaintext_ids)} plaintext — {status}")
            if plaintext_ids:
                exit_code = 1
                print(f"  affected {id_col}s: {plaintext_ids}")
    finally:
        db.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
