"""Windows DPAPI Credential Vault and Plaintext Migration.

Implements:
1. User-scoped DPAPI encryption/decryption for API key & secret.
2. Secure SQLite storage storing ONLY credential_ref, masked strings, and encrypted blobs.
3. Legacy plaintext credentials detection and safe atomic migration.
4. Absolute barrier: secrets are never returned in public dictionaries, logs, or exports.
"""

from __future__ import annotations
import base64
from datetime import datetime, timezone
import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple
import uuid

logger = logging.getLogger("core.security.credentials")

# Check if running on Windows
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _CryptProtectData = ctypes.windll.crypt32.CryptProtectData
    _CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
    _LocalFree = ctypes.windll.kernel32.LocalFree


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt byte payload using Windows user-scoped DPAPI."""
    if not data:
        return b""
    if not _IS_WINDOWS:
        # Fallback for non-Windows developer environments/CI
        return b"DEV_NON_WIN_" + base64.b64encode(data)

    blob_in = _DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
    )
    blob_out = _DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not _CryptProtectData(
        ctypes.byref(blob_in),
        "AIMA_CREDENTIAL",
        None,
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptProtectData DPAPI encryption failed")

    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    _LocalFree(blob_out.pbData)
    return result


def dpapi_unprotect(encrypted_data: bytes) -> bytes:
    """Decrypt byte payload using Windows user-scoped DPAPI."""
    if not encrypted_data:
        return b""
    if not _IS_WINDOWS:
        if encrypted_data.startswith(b"DEV_NON_WIN_"):
            return base64.b64decode(encrypted_data[len(b"DEV_NON_WIN_"):])
        return encrypted_data

    blob_in = _DATA_BLOB(
        len(encrypted_data),
        ctypes.cast(
            ctypes.create_string_buffer(encrypted_data),
            ctypes.POINTER(ctypes.c_char),
        ),
    )
    blob_out = _DATA_BLOB()
    if not _CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptUnprotectData DPAPI decryption failed")

    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    _LocalFree(blob_out.pbData)
    return result


def mask_api_key(key: str) -> str:
    """Format safe masked API key representation, e.g. 'test***2345'."""
    clean = (key or "").strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "***"
    return f"{clean[:4]}***{clean[-4:]}"


class CredentialVault:
    """Unified secure credential vault backed by DPAPI and SQLite."""

    @staticmethod
    def ensure_tables(store) -> None:
        """Create vault tables if not present."""
        with store._connect() as db:
            db.execute("""
            CREATE TABLE IF NOT EXISTS secure_credentials_vault (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                credential_ref TEXT NOT NULL,
                venue TEXT NOT NULL DEFAULT 'gateio',
                api_key_masked TEXT NOT NULL,
                encrypted_key_blob BLOB NOT NULL,
                encrypted_secret_blob BLOB NOT NULL,
                testnet INTEGER NOT NULL DEFAULT 0,
                permissions_summary TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            """)

    @staticmethod
    def check_legacy_migration_needed(store) -> bool:
        """Check if legacy gate_credentials table contains unmigrated plaintext secrets."""
        with store._connect() as db:
            # Check if table exists
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_credentials'"
            ).fetchone()
            if not table:
                return False

            row = db.execute("SELECT api_key, api_secret FROM gate_credentials WHERE id = 1").fetchone()
            if not row:
                return False

            secret = str(row["api_secret"] or "").strip()
            # If plaintext secret is present and not blanked out
            return bool(secret and secret != "[MIGRATED_TO_DPAPI]")

    @classmethod
    def get_metadata(cls, store) -> Dict[str, Any]:
        """Return public safe metadata (ZERO secret leakage)."""
        cls.ensure_tables(store)
        migration_required = cls.check_legacy_migration_needed(store)
        with store._connect() as db:
            row = db.execute("SELECT * FROM secure_credentials_vault WHERE id = 1").fetchone()
            if not row:
                # If no vault row but legacy row exists with credentials, report migration required
                if migration_required:
                    return {
                        "configured": False,
                        "credential_ref": "",
                        "api_key_masked": "",
                        "testnet": False,
                        "migration_status": "MIGRATION_REQUIRED",
                        "updated_at": None,
                    }
                return {
                    "configured": False,
                    "credential_ref": "",
                    "api_key_masked": "",
                    "testnet": False,
                    "migration_status": "CLEAN",
                    "updated_at": None,
                }

            return {
                "configured": True,
                "credential_ref": row["credential_ref"],
                "api_key_masked": row["api_key_masked"],
                "testnet": bool(row["testnet"]),
                "migration_status": "MIGRATION_REQUIRED" if migration_required else "CLEAN",
                "updated_at": row["updated_at"],
            }

    @classmethod
    def save_credentials(
        cls,
        store,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        venue: str = "gateio",
    ) -> Dict[str, Any]:
        """Encrypt and atomically store credentials via DPAPI."""
        clean_key = (api_key or "").strip()
        clean_secret = (api_secret or "").strip()
        if not clean_key or not clean_secret:
            raise ValueError("API Key and Secret must not be empty.")

        cls.ensure_tables(store)
        enc_key = dpapi_protect(clean_key.encode("utf-8"))
        enc_secret = dpapi_protect(clean_secret.encode("utf-8"))
        masked = mask_api_key(clean_key)
        ref = f"cred_{venue}_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        with store._connect() as db:
            db.execute("""
            INSERT INTO secure_credentials_vault (
                id, credential_ref, venue, api_key_masked,
                encrypted_key_blob, encrypted_secret_blob,
                testnet, permissions_summary, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, '{}', ?)
            ON CONFLICT(id) DO UPDATE SET
                credential_ref=excluded.credential_ref,
                venue=excluded.venue,
                api_key_masked=excluded.api_key_masked,
                encrypted_key_blob=excluded.encrypted_key_blob,
                encrypted_secret_blob=excluded.encrypted_secret_blob,
                testnet=excluded.testnet,
                updated_at=excluded.updated_at;
            """, (ref, venue, masked, enc_key, enc_secret, int(testnet), now_iso))

            # Also clear plaintext in legacy table if it exists
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_credentials'"
            ).fetchone()
            if table:
                db.execute(
                    "UPDATE gate_credentials SET api_secret='[MIGRATED_TO_DPAPI]' WHERE id = 1"
                )

        return cls.get_metadata(store)

    @classmethod
    def migrate_legacy_credentials(cls, store) -> Dict[str, Any]:
        """Atomically migrate plaintext credentials from gate_credentials into DPAPI vault."""
        cls.ensure_tables(store)
        with store._connect() as db:
            row = db.execute(
                "SELECT api_key, api_secret, testnet FROM gate_credentials WHERE id = 1"
            ).fetchone()
            if not row or not row["api_secret"] or row["api_secret"] == "[MIGRATED_TO_DPAPI]":
                return {"migrated": False, "reason": "No legacy plaintext credentials found"}

            api_key = str(row["api_key"]).strip()
            api_secret = str(row["api_secret"]).strip()
            testnet = bool(row["testnet"])

        # Protect and test decrypt
        enc_key = dpapi_protect(api_key.encode("utf-8"))
        enc_secret = dpapi_protect(api_secret.encode("utf-8"))

        dec_key = dpapi_unprotect(enc_key).decode("utf-8")
        dec_secret = dpapi_unprotect(enc_secret).decode("utf-8")
        if dec_key != api_key or dec_secret != api_secret:
            raise RuntimeError("DPAPI self-verification failed during migration")

        ref = f"cred_migrated_{uuid.uuid4().hex[:12]}"
        masked = mask_api_key(api_key)
        now_iso = datetime.now(timezone.utc).isoformat()

        with store._connect() as db:
            db.execute("""
            INSERT INTO secure_credentials_vault (
                id, credential_ref, venue, api_key_masked,
                encrypted_key_blob, encrypted_secret_blob,
                testnet, permissions_summary, updated_at
            ) VALUES (1, ?, 'gateio', ?, ?, ?, ?, '{}', ?)
            ON CONFLICT(id) DO UPDATE SET
                credential_ref=excluded.credential_ref,
                venue=excluded.venue,
                api_key_masked=excluded.api_key_masked,
                encrypted_key_blob=excluded.encrypted_key_blob,
                encrypted_secret_blob=excluded.encrypted_secret_blob,
                testnet=excluded.testnet,
                updated_at=excluded.updated_at;
            """, (ref, masked, enc_key, enc_secret, int(testnet), now_iso))

            # Atomically wipe plaintext secret
            db.execute("UPDATE gate_credentials SET api_secret='[MIGRATED_TO_DPAPI]' WHERE id = 1")

        logger.info("Successfully migrated legacy credentials to DPAPI vault: %s", ref)
        return {"migrated": True, "credential_ref": ref, "api_key_masked": masked}

    @classmethod
    def get_in_memory_keys(cls, store) -> Tuple[Optional[str], Optional[str]]:
        """Internal retrieval of decrypted key pair for execution gateway.
        
        NEVER pass these to public responses or logs.
        """
        cls.ensure_tables(store)
        with store._connect() as db:
            row = db.execute(
                "SELECT encrypted_key_blob, encrypted_secret_blob FROM secure_credentials_vault WHERE id = 1"
            ).fetchone()
            if not row:
                return None, None

            try:
                k = dpapi_unprotect(row["encrypted_key_blob"]).decode("utf-8")
                s = dpapi_unprotect(row["encrypted_secret_blob"]).decode("utf-8")
                return k, s
            except Exception as exc:
                logger.error("Failed to decrypt credentials from DPAPI vault: %s", exc)
                return None, None

    @staticmethod
    def ensure_account_tables(store) -> None:
        """Create the account-scoped vault without changing the legacy vault.

        The original ``secure_credentials_vault`` is intentionally a single
        compatibility slot.  Gate paper and live accounts must never share
        that slot, otherwise saving one account silently rebinds the other.
        This additive table keeps one encrypted key pair per registered
        account while preserving the old global API for existing clients.
        """
        CredentialVault.ensure_tables(store)
        with store._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS secure_account_credentials (
                    account_id TEXT PRIMARY KEY,
                    credential_ref TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT 'gateio',
                    api_key_masked TEXT NOT NULL,
                    encrypted_key_blob BLOB NOT NULL,
                    encrypted_secret_blob BLOB NOT NULL,
                    testnet INTEGER NOT NULL DEFAULT 0,
                    permissions_summary TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                """
            )

    @classmethod
    def get_account_metadata(cls, store, account_id: str) -> Dict[str, Any]:
        """Return safe metadata for one account-scoped credential slot."""
        clean_account_id = str(account_id or "").strip()
        if not clean_account_id:
            raise ValueError("ACCOUNT_REQUIRED")
        cls.ensure_account_tables(store)
        with store._connect() as db:
            row = db.execute(
                "SELECT * FROM secure_account_credentials WHERE account_id=?",
                (clean_account_id,),
            ).fetchone()
        if row is None:
            return {
                "account_id": clean_account_id,
                "configured": False,
                "credential_ref": "",
                "venue": "gateio",
                "api_key_masked": "",
                "testnet": False,
                "migration_status": "CLEAN",
                "updated_at": None,
            }
        return {
            "account_id": clean_account_id,
            "configured": True,
            "credential_ref": row["credential_ref"],
            "venue": row["venue"],
            "api_key_masked": row["api_key_masked"],
            "testnet": bool(row["testnet"]),
            "migration_status": "CLEAN",
            "updated_at": row["updated_at"],
        }

    @classmethod
    def save_account_credentials(
        cls,
        store,
        account_id: str,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = False,
        venue: str = "gateio",
    ) -> Dict[str, Any]:
        """Encrypt and atomically replace only one account's key pair."""
        clean_account_id = str(account_id or "").strip()
        clean_key = (api_key or "").strip()
        clean_secret = (api_secret or "").strip()
        if not clean_account_id:
            raise ValueError("ACCOUNT_REQUIRED")
        if not clean_key or not clean_secret:
            raise ValueError("API Key and Secret must not be empty.")

        cls.ensure_account_tables(store)
        enc_key = dpapi_protect(clean_key.encode("utf-8"))
        enc_secret = dpapi_protect(clean_secret.encode("utf-8"))
        masked = mask_api_key(clean_key)
        ref = f"cred_{venue}_{clean_account_id}_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        with store._connect() as db:
            db.execute(
                """
                INSERT INTO secure_account_credentials (
                    account_id, credential_ref, venue, api_key_masked,
                    encrypted_key_blob, encrypted_secret_blob,
                    testnet, permissions_summary, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    credential_ref=excluded.credential_ref,
                    venue=excluded.venue,
                    api_key_masked=excluded.api_key_masked,
                    encrypted_key_blob=excluded.encrypted_key_blob,
                    encrypted_secret_blob=excluded.encrypted_secret_blob,
                    testnet=excluded.testnet,
                    updated_at=excluded.updated_at;
                """,
                (clean_account_id, ref, venue, masked, enc_key, enc_secret, int(testnet), now_iso),
            )
        return cls.get_account_metadata(store, clean_account_id)

    @classmethod
    def get_account_in_memory_keys(
        cls, store, account_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Decrypt one account's pair for an internal execution adapter only."""
        clean_account_id = str(account_id or "").strip()
        if not clean_account_id:
            return None, None
        cls.ensure_account_tables(store)
        with store._connect() as db:
            row = db.execute(
                """
                SELECT encrypted_key_blob, encrypted_secret_blob
                FROM secure_account_credentials
                WHERE account_id=?
                """,
                (clean_account_id,),
            ).fetchone()
        if row is None:
            return None, None
        try:
            key = dpapi_unprotect(row["encrypted_key_blob"]).decode("utf-8")
            secret = dpapi_unprotect(row["encrypted_secret_blob"]).decode("utf-8")
            return key, secret
        except Exception as exc:
            logger.error(
                "Failed to decrypt account-scoped credentials for %s: %s",
                clean_account_id,
                exc,
            )
            return None, None
