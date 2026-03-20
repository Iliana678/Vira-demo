"""
services/brand_kb.py — 品牌知识库服务层

数据库表：brand_profiles
每个用户可拥有多个品牌 Profile，同一时间只有一个 is_active=1。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "users.db"

# ── 建表 ──────────────────────────────────────────────────────────────────────

def _init_table() -> None:
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS brand_profiles (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_email      TEXT    NOT NULL,
                    profile_name    TEXT    NOT NULL DEFAULT '默认品牌',
                    brand_name      TEXT    DEFAULT '',
                    category        TEXT    DEFAULT '',
                    core_sku        TEXT    DEFAULT '',
                    target_audience TEXT    DEFAULT '',
                    tone            TEXT    DEFAULT '[]',
                    forbidden_words TEXT    DEFAULT '[]',
                    hit_keywords    TEXT    DEFAULT '[]',
                    collab_style    TEXT    DEFAULT '',
                    is_active       INTEGER DEFAULT 0,
                    created_at      TEXT,
                    updated_at      TEXT
                )
            """)
    except Exception as exc:
        logger.error("brand_kb init error: %s", exc)


def _migrate_table() -> None:
    """向已存在的表追加新列（幂等执行）。"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(brand_profiles)").fetchall()
            }
            if "target_platforms" not in existing_cols:
                conn.execute(
                    "ALTER TABLE brand_profiles ADD COLUMN target_platforms TEXT DEFAULT '[]'"
                )
    except Exception as exc:
        logger.error("brand_kb migrate error: %s", exc)


_init_table()
_migrate_table()

# ── 内部工具 ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("tone", "forbidden_words", "hit_keywords", "target_platforms"):
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except Exception:
            d[field] = []
    return d


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# ── 公开 API ──────────────────────────────────────────────────────────────────

def get_brand_profiles(email: str) -> list[dict]:
    """返回该用户全部品牌 Profile，active 优先排列。"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM brand_profiles WHERE user_email=? "
                "ORDER BY is_active DESC, id ASC",
                (email.lower().strip(),),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_brand_profiles error: %s", exc)
        return []


def get_active_brand(email: str) -> Optional[dict]:
    """返回当前激活的品牌 Profile，若无则返回 None。"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM brand_profiles "
                "WHERE user_email=? AND is_active=1 LIMIT 1",
                (email.lower().strip(),),
            ).fetchone()
        return _row_to_dict(row) if row else None
    except Exception as exc:
        logger.error("get_active_brand error: %s", exc)
        return None


def save_brand_profile(
    email: str, data: dict, profile_id: Optional[int] = None
) -> tuple[bool, str, int]:
    """
    创建或更新品牌 Profile。

    Returns:
        (success, message, profile_id)
    """
    try:
        tone_j = json.dumps(data.get("tone") or [], ensure_ascii=False)
        fw_j   = json.dumps(data.get("forbidden_words") or [], ensure_ascii=False)
        hk_j   = json.dumps(data.get("hit_keywords") or [], ensure_ascii=False)
        tp_j   = json.dumps(data.get("target_platforms") or [], ensure_ascii=False)
        now    = _now()

        with sqlite3.connect(str(DB_PATH)) as conn:
            if profile_id:
                conn.execute(
                    """UPDATE brand_profiles SET
                        profile_name=?, brand_name=?, category=?, core_sku=?,
                        target_audience=?, tone=?, forbidden_words=?, hit_keywords=?,
                        collab_style=?, target_platforms=?, updated_at=?
                       WHERE id=? AND user_email=?""",
                    (
                        data.get("profile_name", "默认品牌"),
                        data.get("brand_name", ""),
                        data.get("category", ""),
                        data.get("core_sku", ""),
                        data.get("target_audience", ""),
                        tone_j, fw_j, hk_j,
                        data.get("collab_style", ""),
                        tp_j,
                        now, profile_id, email.lower().strip(),
                    ),
                )
                return True, "品牌知识库已更新", profile_id

            # 新建：若是第一个 profile 则自动激活
            count = conn.execute(
                "SELECT COUNT(*) FROM brand_profiles WHERE user_email=?",
                (email.lower().strip(),),
            ).fetchone()[0]
            is_active = 1 if count == 0 else 0

            cur = conn.execute(
                """INSERT INTO brand_profiles
                    (user_email, profile_name, brand_name, category, core_sku,
                     target_audience, tone, forbidden_words, hit_keywords,
                     collab_style, target_platforms, is_active, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    email.lower().strip(),
                    data.get("profile_name", "默认品牌"),
                    data.get("brand_name", ""),
                    data.get("category", ""),
                    data.get("core_sku", ""),
                    data.get("target_audience", ""),
                    tone_j, fw_j, hk_j,
                    data.get("collab_style", ""),
                    tp_j,
                    is_active, now, now,
                ),
            )
            return True, "品牌知识库已保存", cur.lastrowid

    except Exception as exc:
        logger.error("save_brand_profile error: %s", exc)
        return False, f"保存失败：{exc}", -1


def set_active_brand(email: str, profile_id: int) -> bool:
    """切换激活品牌。"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "UPDATE brand_profiles SET is_active=0 WHERE user_email=?",
                (email.lower().strip(),),
            )
            conn.execute(
                "UPDATE brand_profiles SET is_active=1 WHERE id=? AND user_email=?",
                (profile_id, email.lower().strip()),
            )
        return True
    except Exception as exc:
        logger.error("set_active_brand error: %s", exc)
        return False


def delete_brand_profile(email: str, profile_id: int) -> bool:
    """删除品牌 Profile。"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "DELETE FROM brand_profiles WHERE id=? AND user_email=?",
                (profile_id, email.lower().strip()),
            )
        return True
    except Exception as exc:
        logger.error("delete_brand_profile error: %s", exc)
        return False


def format_brand_context(profile: Optional[dict]) -> str:
    """
    将品牌 Profile 格式化为可注入 prompt 的纯文本。
    若 profile 为 None 则返回空字符串（通用模式）。
    包含 target_platforms 时，自动附加各平台特征说明，
    并指示 Agent 为每个平台分别生成一套脚本。
    """
    if not profile:
        return ""

    from prompts import PLATFORM_PROFILES

    tone_str = "、".join(profile.get("tone") or []) or "通用"
    fw_str   = "、".join(profile.get("forbidden_words") or []) or "无"
    hk_str   = "、".join(profile.get("hit_keywords") or []) or "无"

    base = (
        "【品牌知识库信息，请在生成脚本时严格遵守】\n"
        f"- 品牌名称：{profile.get('brand_name') or '[未填写]'}\n"
        f"- 主营品类：{profile.get('category') or '[未填写]'}\n"
        f"- 核心产品：{profile.get('core_sku') or '[未填写]'}\n"
        f"- 目标人群：{profile.get('target_audience') or '[未填写]'}\n"
        f"- 品牌调性：{tone_str}\n"
        f"- 禁用词（绝对不能出现）：{fw_str}\n"
        f"- 过往有效关键词（优先使用）：{hk_str}\n"
        f"- 达人合作风格：{profile.get('collab_style') or '[未填写]'}"
    )

    target_platforms = profile.get("target_platforms") or []
    if not target_platforms:
        return base

    platform_block = "\n\n【平台专属要求】\n"
    platform_block += (
        f"本次需要为以下 {len(target_platforms)} 个目标平台分别生成脚本，"
        f"每套脚本的 title 字段请以【平台名】开头（例：「【抖音】稳健型」），"
        f"platforms 字段仅填写该脚本对应的平台名称。\n"
        f"目标平台：{' / '.join(target_platforms)}\n"
    )
    for platform in target_platforms:
        if platform in PLATFORM_PROFILES:
            platform_block += f"\n▸ {platform}：{PLATFORM_PROFILES[platform]}"

    return base + platform_block
