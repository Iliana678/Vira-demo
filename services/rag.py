"""
services/rag.py
三个独立服务模块：

  RAGService    — 基于 OpenAI text-embedding-3-small 的向量语义检索
                  · 首次 load() 时批量计算并缓存全部 chunk embedding
                  · 检索时用 numpy 纯实现余弦相似度，无外部向量数据库
                  · retrieve_with_scores() 返回 top-3 条目 + 相似度分数
  FeedbackStore — SQLite 反馈持久化，模拟 SFT bad-case 回流管道
  HistoryStore  — SQLite 完整分析历史存储，支持侧边栏回放与统计看板

数据库位置：data/vira_history.db（单文件，包含三张表）
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# 统一数据库路径（三个 Store 共用同一个 .db 文件，通过表名隔离）
_DB_PATH = Path(__file__).parent.parent / "data" / "vira_history.db"

# ── 模块级 Embedding 缓存 ─────────────────────────────────────────────────────
# key: md5(text) → np.ndarray (float32, dim=1536)
# 进程内持久，跨 RAGService 实例共享，避免对相同文本重复调用 API
_EMBED_CACHE: Dict[str, np.ndarray] = {}


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
    基于 OpenAI text-embedding-3-small 的内存向量 RAG 检索。

    设计要点：
      · 无外部向量数据库依赖，全部用 numpy 实现余弦相似度计算
      · 首次 load() 批量请求 Embedding API，结果缓存在模块级 _EMBED_CACHE
      · retrieve_with_scores() 返回 List[{text, score}]，供 UI 展示
      · retrieve() / build_context() 接口不变，下游 Agent 无需改动

    升级路径（生产）：
      · 将 _embed_batch() 替换为 BGE-M3 本地推理 / Pinecone upsert
      · 接口保持不变
    """

    EMBEDDING_MODEL = "text-embedding-3-small"
    SIMILARITY_THRESHOLD = 0.30  # 低于此阈值的结果不返回

    def __init__(self, knowledge_text: str = "", api_key: str = ""):
        self.chunks: List[str] = []
        self._embeddings: Optional[np.ndarray] = None  # shape (N, 1536), float32
        self._api_key = api_key
        # 最近一次检索的带分数结果，供 workflow 采集后透传给 UI
        self.last_hits: List[Dict[str, Any]] = []

        if knowledge_text.strip() and api_key:
            self.load(knowledge_text)

    # ── 文本切分 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _split_chunks(text: str, chunk_size: int = 200) -> List[str]:
        """
        按标点分句 → 滑动窗口合并至 chunk_size 字符以内。
        保留与原 TF-IDF 版本相同的切分逻辑，确保中文短文本友好。
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
        return chunks

    # ── Embedding API ─────────────────────────────────────────────────────────

    def _embed_batch(self, texts: List[str]) -> Optional[np.ndarray]:
        """
        批量计算 texts 的 embedding，优先命中模块级缓存。

        未命中的文本分批（≤100条/次）调用 OpenAI Embeddings API，
        结果写回 _EMBED_CACHE 并按原始顺序返回 (N, D) float32 数组。
        """
        if not texts or not self._api_key:
            return None

        result_vecs: List[np.ndarray] = [None] * len(texts)  # type: ignore
        uncached_indices: List[int] = []
        uncached_texts:   List[str] = []

        for i, text in enumerate(texts):
            key = hashlib.md5(text.encode()).hexdigest()
            if key in _EMBED_CACHE:
                result_vecs[i] = _EMBED_CACHE[key]
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=self._api_key)
                batch_size = 100
                new_vecs: List[np.ndarray] = []
                for start in range(0, len(uncached_texts), batch_size):
                    batch = uncached_texts[start : start + batch_size]
                    resp = client.embeddings.create(
                        input=batch,
                        model=self.EMBEDDING_MODEL,
                    )
                    # API 不保证顺序，按 index 排序
                    sorted_data = sorted(resp.data, key=lambda d: d.index)
                    new_vecs.extend(
                        np.array(d.embedding, dtype=np.float32) for d in sorted_data
                    )
                # 写入缓存并填回结果槽
                for idx, text, vec in zip(uncached_indices, uncached_texts, new_vecs):
                    key = hashlib.md5(text.encode()).hexdigest()
                    _EMBED_CACHE[key] = vec
                    result_vecs[idx] = vec

                logger.info(
                    "Embedding API: %d new vectors fetched (cache size=%d)",
                    len(uncached_texts), len(_EMBED_CACHE),
                )
            except Exception as e:
                logger.error("Embedding API failed: %s", e)
                return None

        if any(v is None for v in result_vecs):
            return None
        return np.stack(result_vecs)  # (N, D) float32

    def _embed_query(self, query: str) -> Optional[np.ndarray]:
        """单条 query 的 embedding，同样走缓存。"""
        result = self._embed_batch([query])
        return result[0] if result is not None else None

    # ── 余弦相似度（纯 numpy）────────────────────────────────────────────────

    @staticmethod
    def _cosine_sim(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        """
        计算 query_vec 与 matrix 每行的余弦相似度。

        Args:
            query_vec: 形状 (D,)
            matrix:    形状 (N, D)

        Returns:
            形状 (N,) 的 float32 数组，值域 [-1, 1]
        """
        q_norm = np.linalg.norm(query_vec)
        if q_norm == 0:
            return np.zeros(len(matrix), dtype=np.float32)
        m_norms = np.linalg.norm(matrix, axis=1)
        m_norms = np.where(m_norms == 0, 1e-8, m_norms)
        return (matrix @ query_vec) / (m_norms * q_norm)

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def load(self, text: str, chunk_size: int = 200) -> None:
        """
        切分知识库文本并预计算所有 chunk 的 embedding（首次加载时调用）。

        Embedding 结果写入模块级 _EMBED_CACHE，下次加载相同内容时直接命中缓存。
        """
        chunks = self._split_chunks(text, chunk_size)
        self.chunks = chunks
        if not chunks:
            return

        logger.info("RAG: loading %d chunks, computing embeddings...", len(chunks))
        embeddings = self._embed_batch(chunks)
        if embeddings is not None:
            self._embeddings = embeddings
            logger.info(
                "RAG index ready: %d chunks · dim=%d · model=%s",
                len(chunks), embeddings.shape[1], self.EMBEDDING_MODEL,
            )
        else:
            logger.warning("RAG: embedding failed, index empty (will return no results)")

    def retrieve_with_scores(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """
        语义检索，返回 top_k 个最相似条目及其相似度分数。

        Returns:
            List of {"text": str, "score": float}，按分数从高到低排列。
            相似度低于 SIMILARITY_THRESHOLD 的条目不返回。
        """
        self.last_hits = []
        if not self.chunks or self._embeddings is None:
            return []

        q_emb = self._embed_query(query)
        if q_emb is None:
            return []

        scores = self._cosine_sim(q_emb, self._embeddings)
        top_idx = np.argsort(scores)[::-1][:top_k]
        hits = [
            {"text": self.chunks[i], "score": round(float(scores[i]), 3)}
            for i in top_idx
            if scores[i] >= self.SIMILARITY_THRESHOLD
        ]
        self.last_hits = hits
        return hits

    def retrieve(self, query: str, top_k: int = 3) -> List[str]:
        """兼容旧接口：返回 top_k 条文本（不含分数）"""
        return [h["text"] for h in self.retrieve_with_scores(query, top_k)]

    def build_context(self, query: str) -> str:
        """
        组装 RAG 上下文字符串，直接注入 Agent Prompt。
        同时将检索结果存入 self.last_hits 供 UI 采集。
        """
        hits = self.retrieve_with_scores(query)
        if not hits:
            return "（品牌知识库暂无内容，基于通用爆款规律分析）"
        header = "【品牌知识库 · 向量语义检索结果（text-embedding-3-small，按相似度排序）】"
        body = "\n".join(
            f"  [{i+1}] (相似度:{h['score']:.2f}) {h['text']}"
            for i, h in enumerate(hits)
        )
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
                    None,  # visual_score removed from new schema
                    _d("commerce",   "conversion_score"),
                    _d("commerce",   "conversion_score"),  # maps to both legacy columns
                    _d("compliance", "risk_level"),
                    None,  # compliance_score removed from new schema
                    _d("strategy",   "success_confidence"),
                    _d("strategy",   "final_verdict"),
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
        # remap legacy column names to new schema names for UI compatibility
        results = []
        for r in rows:
            d = dict(r)
            if "confidence_score" in d:
                d.setdefault("success_confidence", d.pop("confidence_score"))
            if "verdict" in d:
                d.setdefault("final_verdict", d.pop("verdict"))
            results.append(d)
        return results

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
                "SELECT AVG(COALESCE(confidence_score, 0)) FROM analysis_history"
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
