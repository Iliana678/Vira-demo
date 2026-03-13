"""
services/rag.py
三个独立服务模块：

  RAGService    — 字符级 TF-IDF 轻量检索（MVP；生产换 Pinecone/BGE-M3）
  FeedbackStore — SQLite 反馈持久化，模拟 SFT bad-case 回流管道
  HistoryStore  — SQLite 完整分析历史存储，支持侧边栏回放与统计看板

数据库位置：data/vira_history.db（单文件，包含三张表）
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

# 统一数据库路径（三个 Store 共用同一个 .db 文件，通过表名隔离）
_DB_PATH = Path(__file__).parent.parent / "data" / "vira_history.db"


# ── 公共连接工厂 ──────────────────────────────────────────────────────────────

def _get_conn(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """返回启用 WAL 模式的 SQLite 连接（高并发写入更友好）"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row  # 支持按列名访问
    return conn


# ── RAG 检索服务 ──────────────────────────────────────────────────────────────

class RAGService:
    """
    基于字符级 TF-IDF 的内存 RAG 检索（中文友好，无需分词库）。

    生产升级路径：
      1. 嵌入模型替换：text-embedding-3-small / BGE-M3
      2. 向量存储迁移：Pinecone / Milvus / pgvector
      接口保持不变，仅需替换 retrieve() 的内部实现。
    """

    def __init__(self, knowledge_text: str = ""):
        self.chunks: List[str] = []
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._matrix = None

        if knowledge_text.strip():
            self.load(knowledge_text)

    def load(self, text: str, chunk_size: int = 180) -> None:
        """
        将知识库文本切分为语义块并建立 TF-IDF 索引。

        切分策略：按标点分句 → 滑动窗口合并至 chunk_size 字符以内。
        字符级 ngram(2,4) 对中文短语匹配效果优于词级 unigram。
        """
        sentences: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for sent in line.replace("！", "。").replace("？", "。").split("。"):
                sent = sent.strip()
                if len(sent) >= 8:
                    sentences.append(sent)

        chunks: List[str] = []
        buf = ""
        for s in sentences:
            if len(buf) + len(s) <= chunk_size:
                buf += s + "。"
            else:
                if buf:
                    chunks.append(buf.strip())
                buf = s + "。"
        if buf:
            chunks.append(buf.strip())

        self.chunks = chunks
        if chunks:
            self._vectorizer = TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(2, 4),
                max_features=8000,
            )
            self._matrix = self._vectorizer.fit_transform(chunks)
            logger.info("RAG index built: %d chunks from %d chars", len(chunks), len(text))

    def retrieve(self, query: str, top_k: int = 3) -> List[str]:
        """余弦相似度检索，返回 top_k 个最相关块"""
        if not self.chunks or self._vectorizer is None:
            return []
        q_vec  = self._vectorizer.transform([query])
        scores = cosine_similarity(q_vec, self._matrix).flatten()
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [self.chunks[i] for i in top_idx if scores[i] > 0.02]

    def build_context(self, query: str) -> str:
        """组装 RAG 上下文字符串，直接注入 Agent Prompt"""
        results = self.retrieve(query)
        if not results:
            return "（品牌知识库暂无内容，基于通用爆款规律分析）"
        header = "【品牌知识库 · RAG 检索结果（按相关度排序）】"
        body   = "\n".join(f"  [{i+1}] {c}" for i, c in enumerate(results))
        return f"{header}\n{body}"


# ── 反馈数据存储 ──────────────────────────────────────────────────────────────

class FeedbackStore:
    """
    SQLite 反馈持久化（单次 Agent 输出的好/坏评价）。
    👎 差评自动标记 bad_case=1，可批量导出用于 SFT 数据标注。
    """

    def __init__(self, db_path: Path = _DB_PATH):
        self.db_path = db_path
        _get_conn(db_path).execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                session_id  TEXT,
                agent_name  TEXT    NOT NULL,
                rating      INTEGER NOT NULL,
                bad_case    INTEGER NOT NULL DEFAULT 0,
                input_ref   TEXT,
                output_json TEXT,
                comment     TEXT
            )
            """
        ).connection.commit()

    def save(
        self,
        rating:      int,
        agent_name:  str,
        input_ref:   str,
        output_json: str,
        session_id:  str = "",
        comment:     str = "",
    ) -> None:
        with _get_conn(self.db_path) as conn:
            conn.execute(
                "INSERT INTO feedback VALUES (NULL,?,?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(),
                    session_id, agent_name, rating,
                    1 if rating == 0 else 0,
                    input_ref, output_json, comment,
                ),
            )
        logger.info("Feedback saved | agent=%s rating=%d", agent_name, rating)

    def export_bad_cases(self, limit: int = 50) -> List[Dict]:
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE bad_case=1 ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> Dict[str, int]:
        with _get_conn(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            bad   = conn.execute("SELECT COUNT(*) FROM feedback WHERE bad_case=1").fetchone()[0]
        return {"total": total, "bad_cases": bad, "good": total - bad}


# ── 分析历史存储 ──────────────────────────────────────────────────────────────

class HistoryStore:
    """
    完整分析历史持久化，存储每次 Workflow 的关键指标与全量 JSON。

    表结构设计考量：
      · 关键标量字段（score/risk/verdict）单独列存储，支持快速统计查询
      · full_result_json 存储四个 Agent 的完整输出，供回放和导出
      · 索引建在 session_id 和 created_at 上，保证 UI 翻页查询性能

    生产升级路径：
      · 将 full_result_json 迁移至对象存储（S3/R2）
      · 关键字段迁移至 PostgreSQL 支持分析型查询
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS analysis_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at        TEXT    NOT NULL,
            session_id        TEXT,
            image_name        TEXT,
            -- Agent 1 关键指标
            hook_type         TEXT,
            hook_score        INTEGER,
            visual_score      INTEGER,
            -- Agent 2 关键指标
            virality_score    INTEGER,
            conversion_score  INTEGER,
            -- Agent 3 关键指标
            compliance_risk   TEXT,
            compliance_score  INTEGER,
            -- Agent 4 关键指标
            confidence_score  INTEGER,
            verdict           TEXT,
            -- 完整输出（用于回放/导出）
            full_result_json  TEXT    NOT NULL,
            -- 执行指标
            total_elapsed_ms  INTEGER,
            total_tokens      INTEGER
        )
    """
    _IDX = "CREATE INDEX IF NOT EXISTS idx_history_time ON analysis_history(created_at DESC)"

    def __init__(self, db_path: Path = _DB_PATH):
        self.db_path = db_path
        with _get_conn(db_path) as conn:
            conn.execute(self._SCHEMA)
            conn.execute(self._IDX)
            conn.commit()

    def save(self, session_id: str, image_name: str, wf_result: Any) -> int:
        """
        持久化一次完整分析结果。

        Args:
            session_id:  Streamlit session ID（8位缩写）
            image_name:  上传文件名
            wf_result:   WorkflowResult 对象

        Returns:
            新插入记录的 rowid
        """
        # 安全提取各 Agent 的关键字段
        def _d(agent_key: str, field: str, default=None):
            r = getattr(wf_result, agent_key, None)
            if r and r.success and r.data:
                return r.data.get(field, default)
            return default

        # 序列化完整结果（排除 raw_response 以节省空间）
        full = {}
        for key in ("visual", "commerce", "compliance", "strategy"):
            r = getattr(wf_result, key, None)
            if r:
                full[key] = {
                    "agent_name": r.agent_name,
                    "success":    r.success,
                    "data":       r.data,
                    "error":      r.error,
                    "usage":      r.usage,
                }
        full["meta"] = {
            "total_elapsed_ms": wf_result.total_elapsed_ms,
            "total_tokens":     wf_result.total_tokens,
        }

        with _get_conn(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO analysis_history
                   (created_at, session_id, image_name,
                    hook_type, hook_score, visual_score,
                    virality_score, conversion_score,
                    compliance_risk, compliance_score,
                    confidence_score, verdict,
                    full_result_json, total_elapsed_ms, total_tokens)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(),
                    session_id, image_name,
                    _d("visual",     "hook_type"),
                    _d("visual",     "hook_score"),
                    _d("visual",     "visual_score"),
                    _d("commerce",   "virality_score"),
                    _d("commerce",   "conversion_potential"),
                    _d("compliance", "risk_level"),
                    _d("compliance", "compliance_score"),
                    _d("strategy",   "confidence_score"),
                    _d("strategy",   "verdict"),
                    json.dumps(full, ensure_ascii=False),
                    wf_result.total_elapsed_ms,
                    wf_result.total_tokens,
                ),
            )
            rowid = cur.lastrowid
        logger.info("History saved | id=%d session=%s image=%s", rowid, session_id, image_name)
        return rowid

    def get_recent(self, limit: int = 10) -> List[Dict]:
        """返回最近 N 条分析记录（轻量摘要，不含 full_result_json）"""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                """SELECT id, created_at, session_id, image_name,
                          hook_type, hook_score, compliance_risk,
                          confidence_score, verdict,
                          total_elapsed_ms, total_tokens
                   FROM analysis_history
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_by_id(self, record_id: int) -> Optional[Dict]:
        """按 ID 加载完整分析记录（含 full_result_json，用于回放）"""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_history WHERE id=?", (record_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> Dict[str, Any]:
        """返回分析历史统计摘要"""
        with _get_conn(self.db_path) as conn:
            total    = conn.execute("SELECT COUNT(*) FROM analysis_history").fetchone()[0]
            avg_conf = conn.execute(
                "SELECT AVG(confidence_score) FROM analysis_history WHERE confidence_score IS NOT NULL"
            ).fetchone()[0]
            risk_dist = conn.execute(
                "SELECT compliance_risk, COUNT(*) as cnt FROM analysis_history "
                "WHERE compliance_risk IS NOT NULL GROUP BY compliance_risk"
            ).fetchall()
        return {
            "total":     total,
            "avg_confidence": round(avg_conf or 0, 1),
            "risk_distribution": {r["compliance_risk"]: r["cnt"] for r in risk_dist},
        }
