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
import random
import re
import sqlite3
import string
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "users.db"

FREE_CREDITS = 5        # 新用户注册赠送次数
GIFT_CREDITS = 5        # 每张礼品码兑换的次数


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
        # 礼品码表：每个 6 位码一次性使用，兑换固定 credits
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_codes (
                code        TEXT    PRIMARY KEY,
                credits     INTEGER NOT NULL DEFAULT 5,
                used        INTEGER NOT NULL DEFAULT 0,
                used_by     TEXT    DEFAULT '',
                used_at     TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        # 兼容旧表（已存在但没有 credits 列）
        try:
            conn.execute("ALTER TABLE vira_users ADD COLUMN credits INTEGER DEFAULT 5")
        except sqlite3.OperationalError:
            pass
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


# ── 礼品码 ────────────────────────────────────────────────────────────────────

def _random_code(length: int = 6) -> str:
    """生成大写字母 + 数字的随机码，去除易混淆字符（0/O/1/I）"""
    charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(charset, k=length))


def generate_gift_codes(count: int = 1, credits: int = GIFT_CREDITS) -> list[str]:
    """
    批量生成礼品码（管理员调用）。

    用法示例（在终端或脚本中）：
        from services.auth import generate_gift_codes
        codes = generate_gift_codes(count=10)
        print(codes)  # ['A3F9K2', 'XM7R4N', ...]

    Args:
        count:   生成数量
        credits: 每张码兑换的次数（默认 GIFT_CREDITS=5）

    Returns:
        生成的礼品码列表
    """
    codes = []
    with sqlite3.connect(str(DB_PATH)) as conn:
        for _ in range(count):
            for _attempt in range(20):  # 最多重试 20 次避免碰撞
                code = _random_code()
                try:
                    conn.execute(
                        "INSERT INTO gift_codes (code, credits) VALUES (?, ?)",
                        (code, credits),
                    )
                    codes.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue  # 极低概率碰撞，重新生成
        conn.commit()
    return codes


def redeem_gift_code(email: str, code: str) -> tuple[bool, str, int]:
    """
    用户兑换礼品码。

    Args:
        email: 用户邮箱
        code:  6 位礼品码（大小写不敏感）

    Returns:
        (success, message, new_credits)
        success=True  → 兑换成功，new_credits 为兑换后的剩余次数
        success=False → 兑换失败，new_credits=0
    """
    email = email.strip().lower()
    code  = code.strip().upper()
    if not code:
        return False, "请输入礼品码", 0
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM gift_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return False, "礼品码不存在，请检查后重试", 0
            if row["used"]:
                return False, "该礼品码已被使用过了", 0

            # 标记已使用
            conn.execute(
                "UPDATE gift_codes SET used=1, used_by=?, used_at=datetime('now') WHERE code=?",
                (email, code),
            )
            # 给用户加积分
            gift_credits = row["credits"]
            conn.execute(
                "UPDATE vira_users SET credits = credits + ? WHERE email = ? COLLATE NOCASE",
                (gift_credits, email),
            )
            conn.commit()

            # 返回最新积分
            new_row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            new_credits = new_row["credits"] if new_row else gift_credits
        return True, f"🎉 兑换成功！已获得 {gift_credits} 次分析额度", new_credits
    except Exception as e:
        return False, f"兑换出错：{e}", 0


# ── 统计 ──────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM vira_users").fetchone()[0]
        return {"total": total}
    except Exception:
        return {"total": 0}
