"""
services/auth.py
邮箱 + 密码鉴权服务

技术方案：
  · 存储：SQLite（复用已有 data/vira_history.db 同目录的 vira_users 表）
  · 哈希：hashlib.pbkdf2_hmac("sha256") — 260,000 轮迭代 + 16 字节随机 salt
  · 零第三方依赖（标准库实现，无需安装 bcrypt）
"""

import hashlib
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "users.db"


# ── 建表 ──────────────────────────────────────────────────────────────────────

def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vira_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT    NOT NULL,
                display_name  TEXT    DEFAULT '',
                created_at    TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


_init_db()


# ── 密码哈希 / 验证 ────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """返回 '<salt_hex>:<dk_hex>'，salt 每次随机生成"""
    salt = os.urandom(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        dk   = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
        return dk.hex() == dk_hex
    except Exception:
        return False


# ── 邮箱格式校验 ──────────────────────────────────────────────────────────────

def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", email.strip()))


# ── 公开 API ──────────────────────────────────────────────────────────────────

def register(email: str, password: str, display_name: str = "") -> tuple[bool, str]:
    """注册新用户。返回 (success, message)"""
    email = email.strip().lower()
    if not _valid_email(email):
        return False, "邮箱格式不正确"
    if len(password) < 6:
        return False, "密码至少需要 6 位"
    name = (display_name or "").strip() or email.split("@")[0]
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO vira_users (email, password_hash, display_name) VALUES (?, ?, ?)",
                (email, _hash_password(password), name),
            )
            conn.commit()
        return True, "注册成功"
    except sqlite3.IntegrityError:
        return False, "该邮箱已注册，请直接登录"
    except Exception as e:
        return False, f"注册失败：{e}"


def login(email: str, password: str) -> tuple[bool, str, dict]:
    """登录验证。返回 (success, message, user_info_dict)"""
    email = email.strip().lower()
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM vira_users WHERE email = ?", (email,)
            ).fetchone()
        if not row:
            return False, "该邮箱尚未注册，请先注册", {}
        if not _verify_password(password, row["password_hash"]):
            return False, "密码错误，请重试", {}
        return True, "登录成功", {
            "id":           row["id"],
            "email":        row["email"],
            "display_name": row["display_name"],
        }
    except Exception as e:
        return False, f"登录出错：{e}", {}


def get_stats() -> dict:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM vira_users").fetchone()[0]
        return {"total": total}
    except Exception:
        return {"total": 0}
