"""
services/analytics.py
VIRA 埋点分析模块

记录关键用户行为事件到 SQLite analytics 表，为管理员后台提供数据支撑。

事件类型（event_type）：
  upload            — 用户上传图片（image_count, user_email）
  analysis_complete — 分析完成（total_ms, agent timings, reflection, confidence, json_error）
  export            — 导出报告（user_email）
  auth              — 登录 / 注册（user_email, auth_action: login|register）
"""

import json
import logging
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "vira_history.db"


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_table() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                date                TEXT    NOT NULL DEFAULT (date('now','localtime')),
                event_type          TEXT    NOT NULL,
                user_email          TEXT    DEFAULT '',
                image_count         INTEGER DEFAULT 0,
                total_ms            INTEGER DEFAULT 0,
                agent1_ms           INTEGER DEFAULT 0,
                agent2_ms           INTEGER DEFAULT 0,
                agent3_ms           INTEGER DEFAULT 0,
                agent4_ms           INTEGER DEFAULT 0,
                has_reflection      INTEGER DEFAULT 0,
                success_confidence  INTEGER DEFAULT 0,
                has_json_error      INTEGER DEFAULT 0,
                auth_action         TEXT    DEFAULT '',
                extra_json          TEXT    DEFAULT '{}'
            )
        """)
        conn.commit()


_init_table()


# ── 埋点写入 ──────────────────────────────────────────────────────────────────

def record_upload(user_email: str = "", image_count: int = 1) -> None:
    """用户上传图片时调用"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO analytics (event_type, user_email, image_count) VALUES (?,?,?)",
                ("upload", user_email or "", image_count),
            )
            conn.commit()
    except Exception as e:
        logger.warning("analytics.record_upload failed: %s", e)


def record_analysis(
    user_email: str = "",
    total_ms: int = 0,
    agent1_ms: int = 0,
    agent2_ms: int = 0,
    agent3_ms: int = 0,
    agent4_ms: int = 0,
    has_reflection: bool = False,
    success_confidence: int = 0,
    has_json_error: bool = False,
) -> None:
    """分析完成时调用"""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO analytics
                   (event_type, user_email, total_ms,
                    agent1_ms, agent2_ms, agent3_ms, agent4_ms,
                    has_reflection, success_confidence, has_json_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "analysis_complete",
                    user_email or "",
                    total_ms,
                    agent1_ms, agent2_ms, agent3_ms, agent4_ms,
                    1 if has_reflection else 0,
                    success_confidence,
                    1 if has_json_error else 0,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning("analytics.record_analysis failed: %s", e)


def record_export(user_email: str = "") -> None:
    """用户导出报告时调用"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO analytics (event_type, user_email) VALUES (?,?)",
                ("export", user_email or ""),
            )
            conn.commit()
    except Exception as e:
        logger.warning("analytics.record_export failed: %s", e)


def record_auth(user_email: str = "", action: str = "login") -> None:
    """用户登录 / 注册时调用（action: 'login' | 'register'）"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO analytics (event_type, user_email, auth_action) VALUES (?,?,?)",
                ("auth", user_email or "", action),
            )
            conn.commit()
    except Exception as e:
        logger.warning("analytics.record_auth failed: %s", e)


# ── 查询接口（供管理员后台使用）─────────────────────────────────────────────

def get_summary() -> Dict[str, int]:
    """返回 今日/本周/总 分析次数"""
    today = str(date.today())
    week_start = str(date.today() - timedelta(days=date.today().weekday()))
    try:
        with _get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete'"
            ).fetchone()[0]
            today_cnt = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete' AND date=?",
                (today,),
            ).fetchone()[0]
            week_cnt = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete' AND date>=?",
                (week_start,),
            ).fetchone()[0]
        return {"total": total, "today": today_cnt, "week": week_cnt}
    except Exception as e:
        logger.warning("get_summary failed: %s", e)
        return {"total": 0, "today": 0, "week": 0}


def get_dau_trend(days: int = 14) -> List[Dict[str, Any]]:
    """返回最近 N 天的日活用户数（分析事件去重 user_email）"""
    start = str(date.today() - timedelta(days=days - 1))
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT date, COUNT(DISTINCT CASE WHEN user_email='' THEN NULL ELSE user_email END) AS dau
                   FROM analytics
                   WHERE event_type='analysis_complete' AND date>=?
                   GROUP BY date ORDER BY date""",
                (start,),
            ).fetchall()
        return [{"date": r["date"], "dau": r["dau"]} for r in rows]
    except Exception as e:
        logger.warning("get_dau_trend failed: %s", e)
        return []


def get_avg_time_trend(days: int = 14) -> List[Dict[str, Any]]:
    """返回最近 N 天的平均分析耗时（ms）"""
    start = str(date.today() - timedelta(days=days - 1))
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT date, ROUND(AVG(total_ms)) AS avg_ms
                   FROM analytics
                   WHERE event_type='analysis_complete' AND date>=? AND total_ms>0
                   GROUP BY date ORDER BY date""",
                (start,),
            ).fetchall()
        return [{"date": r["date"], "avg_ms": int(r["avg_ms"] or 0)} for r in rows]
    except Exception as e:
        logger.warning("get_avg_time_trend failed: %s", e)
        return []


def get_agent_timings() -> Dict[str, float]:
    """返回各 Agent 平均耗时（ms），仅统计 >0 的有效记录"""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                """SELECT
                     AVG(CASE WHEN agent1_ms>0 THEN agent1_ms END) AS a1,
                     AVG(CASE WHEN agent2_ms>0 THEN agent2_ms END) AS a2,
                     AVG(CASE WHEN agent3_ms>0 THEN agent3_ms END) AS a3,
                     AVG(CASE WHEN agent4_ms>0 THEN agent4_ms END) AS a4
                   FROM analytics WHERE event_type='analysis_complete'""",
            ).fetchone()
        return {
            "Agent1 视觉": round(row["a1"] or 0),
            "Agent2 转化": round(row["a2"] or 0),
            "Agent3 合规": round(row["a3"] or 0),
            "Agent4 策略": round(row["a4"] or 0),
        }
    except Exception as e:
        logger.warning("get_agent_timings failed: %s", e)
        return {}


def get_json_error_rate() -> float:
    """JSON 解析失败率（0~1）"""
    try:
        with _get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete'"
            ).fetchone()[0]
            errs = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete' AND has_json_error=1"
            ).fetchone()[0]
        return round(errs / total, 4) if total else 0.0
    except Exception as e:
        logger.warning("get_json_error_rate failed: %s", e)
        return 0.0


def get_reflection_rate() -> float:
    """Reflection 触发率（0~1）"""
    try:
        with _get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete'"
            ).fetchone()[0]
            reflected = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete' AND has_reflection=1"
            ).fetchone()[0]
        return round(reflected / total, 4) if total else 0.0
    except Exception as e:
        logger.warning("get_reflection_rate failed: %s", e)
        return 0.0


def get_confidence_distribution() -> List[int]:
    """返回所有 success_confidence 值列表（用于绘制直方图）"""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT success_confidence FROM analytics "
                "WHERE event_type='analysis_complete' AND success_confidence>0"
            ).fetchall()
        return [r["success_confidence"] for r in rows]
    except Exception as e:
        logger.warning("get_confidence_distribution failed: %s", e)
        return []


def get_user_rankings(limit: int = 20) -> List[Dict[str, Any]]:
    """每个用户的分析次数排行（过滤匿名）"""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT user_email, COUNT(*) AS cnt
                   FROM analytics
                   WHERE event_type='analysis_complete' AND user_email!=''
                   GROUP BY user_email ORDER BY cnt DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [{"email": r["user_email"], "count": r["cnt"]} for r in rows]
    except Exception as e:
        logger.warning("get_user_rankings failed: %s", e)
        return []


def get_export_rate() -> Dict[str, Any]:
    """导出率 = 导出次数 / 分析次数"""
    try:
        with _get_conn() as conn:
            analyses = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='analysis_complete'"
            ).fetchone()[0]
            exports = conn.execute(
                "SELECT COUNT(*) FROM analytics WHERE event_type='export'"
            ).fetchone()[0]
        rate = round(exports / analyses, 4) if analyses else 0.0
        return {"analyses": analyses, "exports": exports, "rate": rate}
    except Exception as e:
        logger.warning("get_export_rate failed: %s", e)
        return {"analyses": 0, "exports": 0, "rate": 0.0}


def get_raw_logs(
    limit: int = 50,
    date_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """返回最近 N 条原始事件（按 created_at 降序），可按日期筛选"""
    try:
        with _get_conn() as conn:
            if date_filter:
                rows = conn.execute(
                    "SELECT * FROM analytics WHERE date=? ORDER BY id DESC LIMIT ?",
                    (date_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM analytics ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("get_raw_logs failed: %s", e)
        return []


def get_total_users() -> int:
    """从 users.db 获取注册用户总数"""
    try:
        from services.auth import DB_PATH as _user_db
        with sqlite3.connect(str(_user_db)) as conn:
            return conn.execute("SELECT COUNT(*) FROM vira_users").fetchone()[0]
    except Exception:
        return 0
