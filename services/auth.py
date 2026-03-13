"""
services/auth.py
邮箱 + 密码鉴权 + 报告额度 + 每日限额 + Pro 会员 + 礼品码

分层规则：
  免费用户   — 共 5 份报告总额度 ·  每日最多 3 条素材
  礼品码用户 — 每张码 +5 份报告   ·  每日最多 5 条素材
  Pro 会员   — 报告不设总量上限   ·  每日最多 30 条素材
"""

import hashlib
import os
import random
import re
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "users.db"

FREE_CREDITS  = 5    # 新用户注册赠送报告份数
GIFT_CREDITS  = 5    # 每张礼品码赠送报告份数

# 每日素材条数上限（按会员等级）
DAILY_LIMIT_FREE = 3
DAILY_LIMIT_GIFT = 5
DAILY_LIMIT_PRO  = 30


# ── 建表 ──────────────────────────────────────────────────────────────────────

def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vira_users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                password_hash    TEXT    NOT NULL,
                display_name     TEXT    DEFAULT '',
                credits          INTEGER DEFAULT 5,
                is_pro           INTEGER DEFAULT 0,
                daily_used       INTEGER DEFAULT 0,
                last_reset_date  TEXT    DEFAULT '',
                created_at       TEXT    DEFAULT (datetime('now'))
            )
        """)
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
        # 兼容旧表：逐列追加，失败即忽略
        for col_sql in [
            "ALTER TABLE vira_users ADD COLUMN credits INTEGER DEFAULT 5",
            "ALTER TABLE vira_users ADD COLUMN is_pro INTEGER DEFAULT 0",
            "ALTER TABLE vira_users ADD COLUMN daily_used INTEGER DEFAULT 0",
            "ALTER TABLE vira_users ADD COLUMN last_reset_date TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()


_init_db()


# ── 密码哈希 ──────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
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


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", email.strip()))


# ── 注册 / 登录 ───────────────────────────────────────────────────────────────

def register(email: str, password: str, display_name: str = "") -> tuple[bool, str]:
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
        is_pro = bool(row["is_pro"]) if "is_pro" in row.keys() else False
        return True, "登录成功", {
            "id":           row["id"],
            "email":        row["email"],
            "display_name": row["display_name"],
            "credits":      row["credits"] if row["credits"] is not None else FREE_CREDITS,
            "is_pro":       is_pro,
        }
    except Exception as e:
        return False, f"登录出错：{e}", {}


# ── 报告额度操作 ──────────────────────────────────────────────────────────────

def get_credits(email: str) -> int:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
        return (row[0] or 0) if row else 0
    except Exception:
        return 0


def deduct_credit(email: str) -> tuple[bool, int]:
    """扣 1 份报告额度（Pro 用户跳过总额度限制）。返回 (success, remaining)"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT credits, is_pro FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row is None:
                return False, 0
            is_pro = bool(row["is_pro"])
            credits = row["credits"] or 0
            if not is_pro and credits <= 0:
                return False, 0
            new_credits = credits - 1 if not is_pro else credits  # Pro 不扣总额度
            conn.execute(
                "UPDATE vira_users SET credits = ? WHERE email = ? COLLATE NOCASE",
                (new_credits, email),
            )
            conn.commit()
        return True, new_credits
    except Exception:
        return False, 0


def add_credits(email: str, amount: int) -> tuple[bool, int]:
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


# ── 每日素材限额 ──────────────────────────────────────────────────────────────

def get_daily_status(email: str) -> dict:
    """
    返回用户今日用量信息。

    Returns:
        {
          "daily_used":  int,   今日已分析条数
          "daily_limit": int,   今日上限
          "remaining":   int,   今日剩余
          "is_pro":      bool,
          "blocked":     bool,  True 表示今日已达上限
        }
    """
    today = str(date.today())
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT is_pro, daily_used, last_reset_date FROM vira_users "
                "WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
        if row is None:
            return {"daily_used": 0, "daily_limit": DAILY_LIMIT_FREE,
                    "remaining": DAILY_LIMIT_FREE, "is_pro": False, "blocked": False}

        is_pro = bool(row["is_pro"])
        daily_limit = DAILY_LIMIT_PRO if is_pro else DAILY_LIMIT_FREE

        # 跨天自动重置
        last_date = row["last_reset_date"] or ""
        daily_used = row["daily_used"] or 0
        if last_date != today:
            daily_used = 0  # 新的一天，从 0 开始（实际写入在 increment 里）

        remaining = max(0, daily_limit - daily_used)
        return {
            "daily_used":  daily_used,
            "daily_limit": daily_limit,
            "remaining":   remaining,
            "is_pro":      is_pro,
            "blocked":     daily_used >= daily_limit,
        }
    except Exception:
        return {"daily_used": 0, "daily_limit": DAILY_LIMIT_FREE,
                "remaining": DAILY_LIMIT_FREE, "is_pro": False, "blocked": False}


def increment_daily(email: str) -> tuple[bool, dict]:
    """
    分析成功后调用：日计数 +1，同时处理跨天重置。
    返回 (success, new_daily_status)
    """
    today = str(date.today())
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT is_pro, daily_used, last_reset_date FROM vira_users "
                "WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row is None:
                return False, {}

            is_pro = bool(row["is_pro"])
            daily_limit = DAILY_LIMIT_PRO if is_pro else DAILY_LIMIT_FREE
            last_date   = row["last_reset_date"] or ""
            daily_used  = (row["daily_used"] or 0) if last_date == today else 0

            if daily_used >= daily_limit:
                return False, get_daily_status(email)  # 已超限，拒绝

            new_used = daily_used + 1
            conn.execute(
                "UPDATE vira_users SET daily_used=?, last_reset_date=? "
                "WHERE email=? COLLATE NOCASE",
                (new_used, today, email),
            )
            conn.commit()

        return True, get_daily_status(email)
    except Exception:
        return False, {}


# ── Pro 会员管理（管理员操作）────────────────────────────────────────────────

def set_pro(email: str, is_pro: bool = True) -> tuple[bool, str]:
    """
    设置 / 取消 Pro 会员（手动管理，收款后调用）。

    用法：
        from services.auth import set_pro
        set_pro("user@example.com", True)   # 开通
        set_pro("user@example.com", False)  # 取消
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            affected = conn.execute(
                "UPDATE vira_users SET is_pro=? WHERE email=? COLLATE NOCASE",
                (1 if is_pro else 0, email.strip().lower()),
            ).rowcount
            conn.commit()
        if affected == 0:
            return False, "用户不存在"
        action = "开通" if is_pro else "取消"
        return True, f"✅ 已{action} Pro 会员：{email}"
    except Exception as e:
        return False, f"操作失败：{e}"


# ── 礼品码 ────────────────────────────────────────────────────────────────────

def _random_code(length: int = 6) -> str:
    charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(charset, k=length))


def generate_gift_codes(count: int = 1, credits: int = GIFT_CREDITS) -> list[str]:
    """
    批量生成礼品码（管理员调用）。

    用法：
        from services.auth import generate_gift_codes
        codes = generate_gift_codes(count=10)
        print(codes)
    """
    codes = []
    with sqlite3.connect(str(DB_PATH)) as conn:
        for _ in range(count):
            for _attempt in range(20):
                code = _random_code()
                try:
                    conn.execute(
                        "INSERT INTO gift_codes (code, credits) VALUES (?, ?)",
                        (code, credits),
                    )
                    codes.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    return codes


def redeem_gift_code(email: str, code: str) -> tuple[bool, str, int]:
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
            conn.execute(
                "UPDATE gift_codes SET used=1, used_by=?, used_at=datetime('now') WHERE code=?",
                (email, code),
            )
            gift_credits = row["credits"]
            conn.execute(
                "UPDATE vira_users SET credits = credits + ? WHERE email = ? COLLATE NOCASE",
                (gift_credits, email),
            )
            conn.commit()
            new_row = conn.execute(
                "SELECT credits FROM vira_users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            new_credits = new_row["credits"] if new_row else gift_credits
        return True, f"🎉 兑换成功！已获得 {gift_credits} 份竞品报告额度", new_credits
    except Exception as e:
        return False, f"兑换出错：{e}", 0


# ── 统计 ──────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM vira_users").fetchone()[0]
            pro   = conn.execute("SELECT COUNT(*) FROM vira_users WHERE is_pro=1").fetchone()[0]
        return {"total": total, "pro": pro}
    except Exception:
        return {"total": 0, "pro": 0}
