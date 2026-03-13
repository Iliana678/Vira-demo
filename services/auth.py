"""
services/auth.py
邮箱 + 密码鉴权服务 + 积分系统

技术方案：
  · 存储：SQLite（data/users.db）
  · 哈希：hashlib.pbkdf2_hmac("sha256") — 260,000 轮迭代 + 16 字节随机 salt
  · 零第三方依赖（标准库实现，无需安装 bcrypt）

积分规则：
  · 新用户注册赠送 FREE_CREDITS 次免费分析
  · 每次成功完成分析扣除 1 次
  · 用完后引导申请更多或升级
"""

import hashlib
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "users.db"

FREE_CREDITS = 5  # 新用户注册赠送次数


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
                credits       INTEGER DEFAULT 5,
                created_at    TEXT    DEFAULT (datetime('now'))
            )
        """)
        # 兼容旧表（已存在但没有 credits 列）
        try:
            conn.execute("ALTER TABLE vira_users ADD COLUMN credits INTEGER DEFAULT 5")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
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


# ── 公开 API：注册 / 登录 ──────────────────────────────────────────────────────

def register(email: str, password: str, display_name: str = "") -> tuple[bool, str]:
    """注册新用户，自动赠送 FREE_CREDITS 次分析额度。返回 (success, message)"""
    email = email.strip().lower()
    if not _valid_email(email):
        return False, "邮箱格式不正确"
    if len(password) < 6:
        return False, "密码至少需要 6 位"
    name = (display_name or "").strip() or email.split("@")[0]
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO vira_users (email, password_hash, display_name, credits) VALUES (?, ?, ?, ?)",
                (email, _hash_password(password), name, FREE_CREDITS),
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
            "credits":      row["credits"] if row["credits"] is not None else FREE_CREDITS,
        }
    except Exception as e:
        return False, f"登录出错：{e}", {}


# ── 积分操作 ──────────────────────────────────────────────────────────────────

def get_credits(email: str) -> int:
    """获取用户当前剩余分析次数"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
        if row is None:
            return 0
        return row[0] if row[0] is not None else 0
    except Exception:
        return 0


def deduct_credit(email: str) -> tuple[bool, int]:
    """
    扣除 1 次分析额度。
    返回 (success, remaining_credits)
    success=False 表示额度不足，未扣除
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row is None or (row[0] or 0) <= 0:
                return False, 0
            new_credits = (row[0] or 0) - 1
            conn.execute(
                "UPDATE vira_users SET credits = ? WHERE email = ? COLLATE NOCASE",
                (new_credits, email),
            )
            conn.commit()
        return True, new_credits
    except Exception:
        return False, 0


def add_credits(email: str, amount: int) -> tuple[bool, int]:
    """
    增加分析额度（管理员操作 / 用户购买后调用）。
    返回 (success, new_total_credits)
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row is None:
                return False, 0
            new_credits = (row[0] or 0) + amount
            conn.execute(
                "UPDATE vira_users SET credits = ? WHERE email = ? COLLATE NOCASE",
                (new_credits, email),
            )
            conn.commit()
        return True, new_credits
    except Exception:
        return False, 0


# ── 统计 ──────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM vira_users").fetchone()[0]
        return {"total": total}
    except Exception:
        return {"total": 0}
