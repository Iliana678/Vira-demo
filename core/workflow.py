"""
core/workflow.py
四 Agent 异步并发流水线编排器。

并发架构（asyncio.gather + asyncio.to_thread）：
  Phase 1 [asyncio.gather]: Agent 1（视觉）‖ Agent 3（合规）
    └─ 两个 I/O-bound API 调用真正并发，运行在独立线程池中
  Phase 2 [串行]:            Agent 2（转化）─ 依赖 Agent 1 的视觉结果
  Phase 3 [串行]:            Agent 4（策略）─ 汇总三路，最终裁决

总耗时 ≈ max(T_A1, T_A3) + T_A2 + T_A4

设计决策：
  · asyncio.to_thread (Python ≥ 3.9) 将同步 Agent.run() 提交到线程池
    并以协程形式被 asyncio.gather 调度，保证 GIL 不阻塞并发 I/O
  · nest_asyncio.apply() 允许 asyncio.run() 在 Streamlit 的同步执行上下文
    中被安全调用（Streamlit 内部不运行 event loop，此行实际多数情况为 no-op，
    但加上后可兼容 Jupyter / 任何嵌套 loop 场景）
  · ProgressCallback 在 asyncio 协程完成后、回到主线程前触发，
    与 Streamlit 的渲染模型兼容
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import nest_asyncio

# 允许嵌套 event loop（Streamlit / Jupyter 兼容）
nest_asyncio.apply()

from core.agents import (
    AgentResult,
    ComplianceAuditorAgent,
    CommerceOptimizerAgent,
    StrategyOptimizerAgent,
    VisualAnalystAgent,
)
from services.openai_client import OpenAIClient
from services.rag import RAGService

logger = logging.getLogger(__name__)

# 进度回调类型：(agent_key: str, result: AgentResult) -> None
ProgressCallback = Callable[[str, AgentResult], None]


# ── 工作流结果容器 ────────────────────────────────────────────────────────────

@dataclass
class WorkflowResult:
    """聚合四个 Agent 的输出与全局执行指标"""

    visual:     Optional[AgentResult] = None
    commerce:   Optional[AgentResult] = None
    compliance: Optional[AgentResult] = None
    strategy:   Optional[AgentResult] = None  # Agent 4 最终裁决

    total_elapsed_ms: int = 0
    total_tokens: int = 0
    success: bool = False
    error: str = ""

    @property
    def all_results(self) -> dict:
        """方便迭代所有 Agent 结果（保序）"""
        return {
            "visual":     self.visual,
            "commerce":   self.commerce,
            "compliance": self.compliance,
            "strategy":   self.strategy,
        }


# ── 流水线编排器 ──────────────────────────────────────────────────────────────

class VIRAWorkflow:
    """
    VIRA 四 Agent 异步并发流水线。

    核心技术：asyncio.gather + asyncio.to_thread
      · asyncio.to_thread 将同步的 OpenAI API 调用卸载到 ThreadPoolExecutor
      · asyncio.gather 协调两个协程并发执行，无需手动管理线程
      · 整体对外接口仍为同步（workflow.run()），对 Streamlit 透明

    用法：
        workflow = VIRAWorkflow(api_key=os.getenv("OPENAI_API_KEY",""), model="gpt-4o", rag_text="")
        result = workflow.run(image_data, on_agent_complete=my_callback)
    """

    def __init__(self, api_key: str, model: str = "gpt-4o", rag_text: str = ""):
        # 单 client 实例，openai SDK 内部 httpx 连接池线程安全
        self.client = OpenAIClient(api_key=api_key, model=model)
        self.rag    = RAGService(knowledge_text=rag_text)

        self.agent_visual     = VisualAnalystAgent(self.client)
        self.agent_commerce   = CommerceOptimizerAgent(self.client, self.rag)
        self.agent_compliance = ComplianceAuditorAgent(self.client)
        self.agent_strategy   = StrategyOptimizerAgent(self.client)

    # ── 异步内核 ──────────────────────────────────────────────────────────────

    async def _phase1_gather(self, image_data: bytes) -> tuple[AgentResult, AgentResult]:
        """
        Phase 1：asyncio.gather 并发调度 Agent 1 & 3。

        asyncio.to_thread 原理：
          1. 将同步函数 agent.run() 提交到默认 ThreadPoolExecutor
          2. 返回一个协程，可被 asyncio.gather 调度
          3. 两个线程同时发出 OpenAI HTTP 请求，等待响应
          4. 任一线程完成后立即更新结果，无需等待另一个

        这与 ThreadPoolExecutor + future.result() 的本质区别：
          · ThreadPoolExecutor：阻塞主线程等待 .result()
          · asyncio.gather：主 event loop 挂起协程，空闲时处理其他事件
        """
        r_visual, r_compliance = await asyncio.gather(
            asyncio.to_thread(self.agent_visual.run,     image_data),
            asyncio.to_thread(self.agent_compliance.run, image_data),
            return_exceptions=False,  # 任一 Agent 抛异常时立即传播
        )
        return r_visual, r_compliance

    # ── 对外同步接口（兼容 Streamlit 同步执行模型）────────────────────────────

    def run(
        self,
        image_data: bytes,
        on_agent_complete: Optional[ProgressCallback] = None,
    ) -> WorkflowResult:
        """
        执行完整四 Agent 流水线，对外暴露同步接口。

        内部通过 asyncio.run() 驱动异步并发，
        nest_asyncio.apply() 确保在已有 event loop 的环境（如 Jupyter）中安全运行。

        Args:
            image_data:          图片字节（JPEG/PNG，来自 Streamlit file_uploader）
            on_agent_complete:   Agent 完成回调，用于实时更新 Streamlit UI 进度槽

        Returns:
            WorkflowResult，包含四个 Agent 的结果与执行指标
        """
        result   = WorkflowResult()
        t_start  = time.perf_counter()

        try:
            # ── Phase 1：asyncio.gather 真正并发（A1 ‖ A3）────────────────────
            logger.info("Phase1 start: asyncio.gather(Agent1, Agent3)")
            r_visual, r_compliance = asyncio.run(self._phase1_gather(image_data))

            result.visual     = r_visual
            result.compliance = r_compliance

            logger.info(
                "Phase1 done | visual.ok=%s compliance.ok=%s",
                r_visual.success, r_compliance.success,
            )

            # 回调顺序：先视觉（通常先完成），再合规
            if on_agent_complete:
                on_agent_complete("visual",     r_visual)
                on_agent_complete("compliance", r_compliance)

            # ── Phase 2：Agent 2 串行（依赖 Agent 1 视觉输出）────────────────
            visual_data       = r_visual.data if r_visual.success else None
            result.commerce   = self.agent_commerce.run(image_data, visual_result=visual_data)

            logger.info("Phase2 done | commerce.ok=%s", result.commerce.success)
            if on_agent_complete:
                on_agent_complete("commerce", result.commerce)

            # ── Phase 3：Agent 4 串行（汇总三路，最终裁决）───────────────────
            result.strategy = self.agent_strategy.run(
                image_data        = image_data,
                visual_result     = result.visual.data     if result.visual.success     else None,
                commerce_result   = result.commerce.data   if result.commerce.success   else None,
                compliance_result = result.compliance.data if result.compliance.success else None,
            )

            logger.info(
                "Phase3 done | strategy.ok=%s confidence=%s",
                result.strategy.success,
                result.strategy.data.get("confidence_score") if result.strategy.success else "—",
            )
            if on_agent_complete:
                on_agent_complete("strategy", result.strategy)

            # ── 汇总执行指标 ───────────────────────────────────────────────────
            result.total_elapsed_ms = round((time.perf_counter() - t_start) * 1000)
            result.total_tokens = sum(
                r.usage.get("total_tokens", 0)
                for r in result.all_results.values()
                if r and r.usage
            )
            # 四个 Agent 全部成功才标记 pipeline 成功
            result.success = all(
                r is not None and r.success
                for r in result.all_results.values()
            )
            logger.info(
                "Workflow complete | success=%s elapsed=%dms tokens=%d",
                result.success, result.total_elapsed_ms, result.total_tokens,
            )

        except Exception as e:
            logger.error("Workflow fatal error: %s", e, exc_info=True)
            result.error             = str(e)
            result.success           = False
            result.total_elapsed_ms  = round((time.perf_counter() - t_start) * 1000)

        return result
