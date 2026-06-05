"""Secret Store - API Key encryption and storage.

MVP 使用 Windows DPAPI 加密，回退 base64 用于非 Windows 环境。
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

from flowcraft_core.storage.database import Database


def _dpapi_encrypt(data: bytes) -> bytes:
    """Windows DPAPI 加密（需要 pywin32 或 ctypes）。失败则回退 base64。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        blob_in = DATA_BLOB(len(data), ctypes.c_char_p(data))
        blob_out = DATA_BLOB()

        if crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return result
        raise OSError("CryptProtectData failed")
    except Exception:
        return base64.b64encode(data)


def _dpapi_decrypt(data: bytes) -> bytes:
    """Windows DPAPI 解密。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        blob_in = DATA_BLOB(len(data), ctypes.c_char_p(data))
        blob_out = DATA_BLOB()

        if crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            kernel32.LocalFree(blob_out.pbData)
            return result
        raise OSError("CryptUnprotectData failed")
    except Exception:
        return base64.b64decode(data)


class SecretStore:
    """本地密钥安全存储。

    优先使用 Windows DPAPI 加密，回退 base64。
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._use_dpapi = sys.platform == "win32"

    def set(self, key: str, value: str) -> None:
        raw = value.encode("utf-8")
        encoded = base64.b64encode(
            _dpapi_encrypt(raw) if self._use_dpapi else raw
        ).decode("ascii")
        existing = self._db.fetch_one("SELECT key FROM secrets_refs WHERE key = ?", (key,))
        if existing:
            self._db.update("secrets_refs", "key", key, {
                "reference": encoded,
                "provider": "dpapi" if self._use_dpapi else "base64-mvp",
            })
        else:
            self._db.insert_json("secrets_refs", {
                "key": key,
                "provider": "dpapi" if self._use_dpapi else "base64-mvp",
                "reference": encoded,
                "created_at": self._now(),
            })

    def get(self, key: str) -> str | None:
        row = self._db.fetch_one("SELECT reference FROM secrets_refs WHERE key = ?", (key,))
        if row:
            return self._decode(dict(row)["reference"])
        return None

    def _decode(self, value: str) -> str:
        raw = base64.b64decode(value.encode("ascii"))
        if self._use_dpapi:
            try:
                raw = _dpapi_decrypt(raw)
            except Exception:
                # DPAPI 解密失败，可能来自旧版 base64 → 直接当做明文
                pass
        return raw.decode("utf-8", errors="replace")

    def get_masked(self, key: str) -> str | None:
        value = self.get(key)
        if value and len(value) > 4:
            return "*" * (len(value) - 4) + value[-4:]
        return value

    def delete(self, key: str) -> None:
        self._db.update("secrets_refs", "key", key, {"reference": "", "provider": "deleted"})

    # ── 应用设置 ────────────────────────────────────────────
    def set_setting(self, key: str, value: Any) -> None:
        existing = self._db.fetch_one("SELECT key FROM settings WHERE key = ?", (key,))
        now = self._now()
        if existing:
            self._db.update("settings", "key", key, {
                "value_json": json.dumps(value, ensure_ascii=False),
                "updated_at": now,
            })
        else:
            self._db.insert_json("settings", {
                "key": key,
                "value_json": json.dumps(value, ensure_ascii=False),
                "updated_at": now,
            })

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._db.fetch_one("SELECT value_json FROM settings WHERE key = ?", (key,))
        if row:
            return json.loads(dict(row)["value_json"])
        return default

    def all_settings(self) -> dict[str, Any]:
        rows = self._db.fetch_all("SELECT key, value_json FROM settings", ())
        return {dict(r)["key"]: json.loads(dict(r)["value_json"]) for r in rows}

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
