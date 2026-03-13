"""
app.py — VIRA · 爆款侦察兵 (Viral Scout)
Streamlit 渲染中心 · Session 管理 · 事件路由

工程架构：
  app.py                         — UI 渲染、Session、事件路由
    ├── core/agents.py           — 4 个 Agent 类（含 TikTok 风控字典）
    ├── core/workflow.py         — asyncio.gather 并发流水线
    ├── services/openai_client.py — OpenAI 封装（指数退避重试）
    ├── services/rag.py          — TF-IDF RAG + FeedbackStore + HistoryStore
    └── prompts/__init__.py      — 所有 System Prompt 集中管理

运行：
    pip install -r requirements.txt
    streamlit run app.py
"""

import io
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import streamlit as st
from dotenv import load_dotenv
from PIL import Image

# ── 环境变量（.env 优先，CI/CD 传入的系统变量次之）──────────────────────────
load_dotenv()



# ── 日志（INFO → stdout，方便 Streamlit Cloud 日志面板查看）─────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vira.app")

# ── 页面配置（必须在所有 st.* 之前）─────────────────────────────────────────
st.set_page_config(
    page_title="VIRA · 爆款侦察兵",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)
# --- 权限检查逻辑 ---
def check_password():
    """验证用户密码，并应用高级感渐变 UI"""
    def password_entered():
        # 从 Streamlit Secrets 或环境变量读取，不硬编码
        _correct = (
            st.secrets.get("ACCESS_KEY")
            or os.getenv("ACCESS_KEY", "")
        )
        if _correct and st.session_state["password"] == _correct:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if not st.session_state.get("password_correct"):
        # 注入你选中的“流光紫色”渐变样式
        st.markdown("""
            <style>
            .stApp {
                background: radial-gradient(circle at top left, #E3D2FF 0%, #F8FAFC 50%, #A7C0FF 100%) !important;
            }
            .login-card {
                background: rgba(255, 255, 255, 0.3);
                backdrop-filter: blur(20px);
                padding: 40px;
                border-radius: 24px;
                border: 1px solid rgba(255, 255, 255, 0.5);
                text-align: center;
                margin: 100px auto;
                max-width: 450px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.05);
            }
            </style>
            <div class="login-card">
                <h1 style="color: #1E293B; font-weight: 800;">🔍 VIRA</h1>
                <p style="color: #64748B;">爆款侦察兵正在待命，请输入暗号进入系统</p>
            </div>
        """, unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.text_input("Access Key", type="password", on_change=password_entered, key="password")
            if st.session_state.get("password_correct") == False:
                st.error("😕 暗号不对哦，请重新输入")
        return False
    return True

# 安全拦截闸门：密码不对就地停止，不加载后续任何 Agent 和逻辑
if not check_password():
    st.stop()

# --- 后续就是你原本的业务代码 (无需缩进) ---
# ══════════════════════════════════════════════════════════════════════════════
# HEAD 注入：meta + 字体预连接 + 非阻塞字体加载
# 【Lighthouse 优化】:
#   - 移除 CSS 内 @import（同步阻塞）→ 改用 <link media="print" onload> 异步加载
#   - preconnect 告知浏览器提前与 fonts.googleapis.com / fonts.gstatic.com 建立连接
#   - display=swap 确保字体加载期间使用系统字体兜底，不阻塞首次文本渲染
#   - 精简字重：Jakarta Sans 只保留 700/800，Noto SC 只保留 400/700/900，DM Mono 只保留 400/500
#   - meta description 修复 SEO 82→100 分
# ══════════════════════════════════════════════════════════════════════════════
_FONT_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Plus+Jakarta+Sans:wght@700;800"          # 精简：去掉 500/600
    "&family=Noto+Sans+SC:wght@400;700;900"          # 精简：去掉 300/500
    "&family=DM+Mono:wght@400;500"
    "&display=swap"
)
st.markdown(f"""
<!-- ① preconnect：浏览器提前握手，节省 DNS+TCP+TLS 时间（约 100-300ms）-->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>

<!-- ② 非阻塞字体加载：media=print 技巧，加载完成后 onload 切换为 all -->
<link rel="stylesheet"
      href="{_FONT_URL}"
      media="print"
      onload="this.media='all'">

<!-- ③ noscript 兜底：JS 禁用时仍正常加载字体 -->
<noscript>
  <link rel="stylesheet" href="{_FONT_URL}">
</noscript>

<!-- ④ meta description：修复 Lighthouse SEO 扣分项 -->
<meta name="description"
      content="VIRA · 上传竞品视频截图，30秒解析爆款基因与合规风险。多模态 AI 四 Agent 协同：视觉拆解 · 合规审计 · 转化预测 · 策略裁决。">
""", unsafe_allow_html=True)

# ── 自定义 CSS（VIRA 流光深色系 · Mesh Gradient + Glassmorphism + 物理动效）──
st.markdown("""
<style>
/* ── 系统字体回退栈（字体加载前立即可见，避免 FOIT）────────────────────── */
body, .stApp {
    font-family:
        'Noto Sans SC',
        -apple-system, 'PingFang SC',
        'Microsoft YaHei', '微软雅黑',
        'Noto Sans CJK SC',
        sans-serif !important;
}

/* ══════════════════════════════════════════════════════════════════════════
   设计令牌 — 靛蓝·紫罗兰·深空 调色板
   ══════════════════════════════════════════════════════════════════════════ */
:root {
    /* ─ 主色：靛紫 Periwinkle-Indigo（参考 #7472FE 色系，冷暖交融）*/
    --bl:  #6366F1; --bl2: #818CF8; --bl3: #4F46E5;
    --blD: rgba(99,102,241,.13); --blG: rgba(99,102,241,.24);

    /* ─ 紫罗兰 Amethyst（参考 #A855F7，比纯蓝更暖）*/
    --pu:  #A855F7; --pu2: #C084FC;
    --puD: rgba(168,85,247,.13); --puG: rgba(168,85,247,.24);

    /* ─ 深紫靛（参考 #4338CA，比纯深蓝更暖）*/
    --in:  #4338CA; --in2: #5B5CE6;
    --inD: rgba(67,56,202,.13);  --inG: rgba(67,56,202,.24);

    /* ─ 天蓝（保留作为对比色）*/
    --cy:  #60A5FA; --cyD: rgba(96,165,250,.12);

    /* ─ 功能色（不变）*/
    --gr:  #22D3A0; --grD: rgba(34,211,160,.12);
    --re:  #F43F5E; --reD: rgba(244,63,94,.12);
    --go:  #F59E0B; --goD: rgba(245,158,11,.12);

    /* ─ 文字层级 */
    --t0: #E2E8F0;   /* 主文字 — 冷白 */
    --t1: #7C8FA6;   /* 次文字 — 蓝灰 */
    --t2: #3D4F68;   /* 三级   — 深灰 */

    /* ─ 基底（极深暗紫底，比纯黑多一丝暖调）*/
    --bg: #06021A;

    /* ─ 玻璃材质（边框带薰衣草暖调）*/
    --glass-bg:   rgba(255,255,255,.025);
    --glass-bd:   rgba(139,92,246,.14);    /* 暖紫倾向半透明边框 */
    --glass-bg-h: rgba(255,255,255,.045);
}

/* ══════════════════════════════════════════════════════════════════════════
   全局背景：极深靛蓝 + 左上角紫色弥散光晕 + 右下角靛蓝弥散光晕
   ══════════════════════════════════════════════════════════════════════════ */
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
body {
    background-color: #06021A !important;
    background-image:
        radial-gradient(ellipse 78% 68% at 6%   5%,  rgba(168,85,247,.17)   0%, transparent 65%),
        radial-gradient(ellipse 68% 58% at 96%  94%, rgba(99,102,241,.16)   0%, transparent 65%),
        radial-gradient(ellipse 50% 44% at 50%  48%, rgba(139,92,246,.05)   0%, transparent 60%) !important;
    color: var(--t0) !important;
    font-family: 'Noto Sans SC', sans-serif !important;
}

/* ── 主内容区撑开背景 ─────────────────────────────────────────────────────── */
[data-testid="stMainBlockContainer"] {
    background: transparent !important;
}

/* ── 侧边栏：极深靛蓝 + 带蓝紫右边界 ──────────────────────────────────── */
[data-testid="stSidebar"] {
    background: rgba(5,2,20,.96) !important;
    border-right: 1px solid rgba(139,92,246,.09) !important;
}
[data-testid="stSidebar"] * { color: var(--t1) !important; }
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] strong,
[data-testid="stSidebar"] b { color: var(--t0) !important; }
[data-testid="stSidebar"] .stCaption,
[data-testid="stSidebar"] small { color: var(--t2) !important; }

/* ══════════════════════════════════════════════════════════════════════════
   毛玻璃卡片 — 玻璃悬浮在流光之上的错层感
   backdrop-filter: blur(25px) 产生"玻璃磨砂折射"质感
   ══════════════════════════════════════════════════════════════════════════ */
.glass {
    background: var(--glass-bg);
    border: 1px solid var(--glass-bd);
    border-radius: 14px;
    padding: 20px 24px;
    margin-bottom: 12px;
    margin-top: 4px;
    backdrop-filter: blur(25px);
    -webkit-backdrop-filter: blur(25px);
    box-shadow:
        0 0 0 1px rgba(168,85,247,.06),
        0 4px 24px rgba(67,56,202,.12),
        inset 0 1px 0 rgba(255,255,255,.05);
    transition: background .22s, box-shadow .22s;
    contain: layout style;
}
.glass:hover {
    background: var(--glass-bg-h);
    box-shadow:
        0 0 0 1px rgba(168,85,247,.13),
        0 8px 32px rgba(99,102,241,.18),
        inset 0 1px 0 rgba(255,255,255,.07);
}

/* ══════════════════════════════════════════════════════════════════════════
   Metric 卡 — 数值使用蓝→紫渐变色文字
   ══════════════════════════════════════════════════════════════════════════ */
[data-testid="metric-container"] {
    background: var(--glass-bg) !important;
    border: 1px solid var(--glass-bd) !important;
    border-radius: 12px !important;
    padding: 16px 20px !important;
    backdrop-filter: blur(20px) !important;
    -webkit-backdrop-filter: blur(20px) !important;
    box-shadow: 0 4px 20px rgba(41,79,187,.08) !important;
    transition: background .22s, box-shadow .22s !important;
}
[data-testid="metric-container"]:hover {
    background: var(--glass-bg-h) !important;
    box-shadow: 0 8px 28px rgba(168,85,247,.12) !important;
}
[data-testid="stMetricLabel"] p {
    color: var(--t1) !important;
    font-size: 10px !important;
    letter-spacing: .18em !important;
    text-transform: uppercase !important;
    font-family: 'DM Mono', monospace !important;
}
[data-testid="stMetricValue"] {
    background: linear-gradient(135deg, #818CF8 0%, #C084FC 100%) !important;
    -webkit-background-clip: text !important;
    -webkit-text-fill-color: transparent !important;
    background-clip: text !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-weight: 800 !important;
}
[data-testid="stMetricDelta"] { color: var(--gr) !important; }

/* ══ STREAMLIT 骨架重置 ══════════════════════════════════════════════════ */

/* 1. 隐藏原生工具栏 */
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
}

/* 2. 主内容区 */
section[data-testid="stMain"] {
    padding-top: 0 !important;
}
[data-testid="stMainBlockContainer"] {
    padding-top: 68px !important;
    padding-bottom: 48px !important;
    padding-left: 28px !important;
    padding-right: 28px !important;
    max-width: 980px !important;
    margin: 0 auto !important;
}
[data-testid="block-container"] {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}

/* 3. Hero 区间距处理 */
.vira-hero + div,
.vira-hero ~ .element-container {
    margin-top: 0 !important;
}

/* ── 固定导航栏 — 深靛蓝底 + 蓝紫边界 ─────────────────────────────────── */
.vira-nav {
    position: fixed;
    top: 0; left: 0; right: 0; z-index: 9999;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 44px; height: 56px;
    background: rgba(6,2,26,.92);
    backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
    border-bottom: 1px solid rgba(139,92,246,.10);
}
.vira-nlogo {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 18px; font-weight: 800; letter-spacing: .04em;
    display: flex; align-items: center; gap: 8px; color: var(--t0);
    text-decoration: none;
}
.vira-ndot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--bl); animation: vira-pulse-bl 2s infinite;
    box-shadow: 0 0 10px var(--bl);
}
.vira-nlinks {
    display: flex; gap: 28px; align-items: center;
}
.vira-nlinks a {
    font-size: 13px; color: var(--t1);
    text-decoration: none; transition: color .2s;
}
.vira-nlinks a:hover { color: var(--t0); }
.vira-ncta {
    background: var(--bl); color: #fff; border: none;
    padding: 8px 20px; border-radius: 8px;
    font-size: 13px; font-weight: 700; cursor: pointer;
    font-family: 'Noto Sans SC', sans-serif;
    box-shadow: 0 3px 0 #2D267A, 0 5px 16px var(--blG);
    transition: all .11s; transform: translateY(0);
}
.vira-ncta:hover { background: var(--bl2); transform: translateY(-1px); }
.vira-ncta:active { transform: translateY(2px); box-shadow: 0 1px 0 #2D267A; }

/* ── Hero 区域 ────────────────────────────────────────────────────────── */
.vira-hero {
    text-align: center;
    padding: 96px 24px 72px;
    min-height: 500px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
}
/* Hero 背景光晕 — 紫→靛紫双层弥散，带一丝暖调 */
.vira-hero::before {
    content: '';
    position: absolute;
    top: -10%; left: 50%;
    transform: translateX(-50%);
    width: 900px; height: 520px;
    border-radius: 50%;
    background: radial-gradient(ellipse, rgba(168,85,247,.12) 0%, rgba(99,102,241,.08) 45%, transparent 65%);
    pointer-events: none;
}
/* Hero 底部分隔线 — 薰衣草→靛紫渐变 */
.vira-hero::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(168,85,247,.25), rgba(99,102,241,.22), transparent);
}
.vira-badge {
    display: inline-flex; align-items: center; gap: 8px;
    margin-bottom: 24px;
    font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: .2em;
    color: var(--bl); border: 1px solid rgba(99,102,241,.28);
    background: var(--blD); padding: 5px 16px; border-radius: 20px;
}
.vira-badge-dot {
    width: 5px; height: 5px; border-radius: 50%; background: var(--bl);
    animation: vira-pulse 1.6s infinite;
}
.vira-h1 {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: clamp(40px, 5.5vw, 68px); font-weight: 800;
    line-height: 1.07; letter-spacing: -.025em; margin: 0 0 16px;
    color: var(--t0);
}
/* Hero 主标题（第一行）— 薰衣草→靛紫→浅蓝 */
.vira-h1-acc {
    display: block;
    background: linear-gradient(108deg, #C084FC 0%, #818CF8 48%, #60A5FA 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    filter: drop-shadow(0 0 40px rgba(168,85,247,.35));
}
/* Hero 火焰橙高亮（射中感）— 与蓝紫背景形成冷暖对撞 */
.vira-h1-fire {
    display: block;
    background: linear-gradient(108deg, #FF5F1F 0%, #FF8C00 42%, #FFD166 78%, #FF8C00 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    filter: drop-shadow(0 0 28px rgba(255,100,0,.45));
    animation: fire-glow 3s ease-in-out infinite;
}
@keyframes fire-glow {
    0%,100% { filter: drop-shadow(0 0 24px rgba(255,100,0,.40)); }
    50%      { filter: drop-shadow(0 0 42px rgba(255,165,0,.60)); }
}
.vira-sub {
    font-size: 15px; color: var(--t1); margin: 0 auto 36px;
    max-width: 460px; line-height: 1.9;
}
.vira-sub strong { color: var(--t0); font-weight: 500; }
.vira-proof {
    display: flex; gap: 32px; justify-content: center; flex-wrap: wrap;
    font-size: 13px; color: var(--t1); margin-top: 8px;
}
.vira-proof-num {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 17px; font-weight: 800;
    background: linear-gradient(135deg, #818CF8, #C084FC);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}

/* ── Section 通用 ─────────────────────────────────────────────────────── */
.vira-sec { padding: 56px 0 12px; }
.vira-sec-sh {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: clamp(26px, 3.5vw, 40px); font-weight: 800;
    line-height: 1.15; letter-spacing: -.02em;
    color: var(--t0); margin-bottom: 10px;
}
.vira-sec-sh .cn { font-family: 'Noto Sans SC', sans-serif; font-weight: 900; }
.vira-sec-sp {
    font-size: 14px; color: var(--t1); max-width: 500px;
    line-height: 1.85; margin-bottom: 36px;
}
.vira-hr {
    border: none;
    border-top: 1px solid rgba(100,140,255,.07);
    margin: 0;
}

/* ══════════════════════════════════════════════════════════════════════════
   Agent 卡片组 — 蓝紫边框 + 毛玻璃 + 呼吸光晕动画
   ══════════════════════════════════════════════════════════════════════════ */
.ag3col {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 14px; margin-bottom: 14px; width: 100%;
}
.agcard {
    background: var(--glass-bg);
    border: 1px solid var(--glass-bd);
    border-radius: 14px; padding: 26px 24px 22px;
    position: relative; overflow: hidden;
    backdrop-filter: blur(25px);
    -webkit-backdrop-filter: blur(25px);
    transition: background .22s, transform .22s, box-shadow .22s; cursor: default;
    min-height: 260px;
    will-change: transform;
    contain: layout style;
    animation: agcard-breathe 6s ease-in-out infinite;
}
/* 呼吸边框：box-shadow 用 ::before 伪元素模拟，走合成层 */
@keyframes agcard-breathe {
    0%,100% { box-shadow: 0 4px 24px rgba(67,56,202,.10), 0 0 0 1px rgba(99,102,241,.08); }
    50%      { box-shadow: 0 8px 36px rgba(168,85,247,.16), 0 0 0 1px rgba(168,85,247,.14); }
}
.agcard::after {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    opacity: .9;
}
.ag-cy::after { background: linear-gradient(90deg, #60A5FA, #818CF8); }
.ag-re::after { background: var(--re); }
.ag-pu::after { background: linear-gradient(90deg, #A855F7, #C084FC); }
.agcard:hover {
    background: var(--glass-bg-h);
    transform: translateY(-3px);
    box-shadow: 0 12px 40px rgba(168,85,247,.22), 0 0 0 1px rgba(192,132,252,.18) !important;
    animation: none;
}
.agid {
    font-family: 'DM Mono', monospace; font-size: 9px;
    letter-spacing: .15em; display: flex; align-items: center;
    gap: 5px; margin-bottom: 14px;
}
.agdot { width: 6px; height: 6px; border-radius: 50%; }
.agico { font-size: 26px; margin-bottom: 10px; display: block; }
.agname { font-size: 16px; font-weight: 700; margin-bottom: 4px; color: var(--t0); }
.agsub  { font-size: 11px; color: var(--t1); margin-bottom: 10px; }
.agdesc { font-size: 12px; color: var(--t1); line-height: 1.75; margin-bottom: 12px; }
.agtags { display: flex; gap: 6px; flex-wrap: wrap; }
.agtag  {
    font-size: 10px; padding: 3px 9px; border-radius: 5px;
    border: 1px solid rgba(100,140,255,.14); color: var(--t1);
    background: rgba(99,102,241,.06); font-family: 'DM Mono', monospace;
}

/* ── Agent D（策略执行官）全宽卡 ────────────────────────────────────────── */
.agd {
    background: var(--glass-bg); border: 1px solid var(--glass-bd);
    border-radius: 14px; overflow: hidden; position: relative;
    backdrop-filter: blur(25px); -webkit-backdrop-filter: blur(25px);
    transition: background .22s, box-shadow .22s;
    box-shadow: 0 4px 28px rgba(41,79,187,.10);
}
.agd::after {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, #4338CA, #6366F1, #A855F7, #C084FC);
}
.agd:hover {
    background: var(--glass-bg-h);
    box-shadow: 0 8px 40px rgba(168,85,247,.16);
}
.agd-inner {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 0; min-height: 180px;
}
.agd-left  { padding: 28px 32px; }
.agd-right {
    padding: 26px 28px;
    background: rgba(99,102,241,.025);
    border-left: 1px solid rgba(100,140,255,.08);
}
.dout-lbl {
    font-family: 'DM Mono', monospace; font-size: 9px;
    color: var(--t2); letter-spacing: .18em; margin-bottom: 12px;
}
.dgrade-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.dgrade-box {
    width: 42px; height: 42px; border-radius: 8px; flex-shrink: 0;
    background: var(--grD); border: 1px solid rgba(34,211,160,.3);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 20px; font-weight: 800; color: var(--gr);
}
.dgrade-info { font-size: 12px; color: var(--t1); line-height: 1.5; }
.dgrade-info strong { color: var(--t0); }
.dreco {
    font-size: 12px; color: var(--t1); line-height: 1.7;
    border-top: 1px solid rgba(100,140,255,.07); padding-top: 10px;
}
.dreco strong { color: var(--bl); }

/* ── Agent 状态徽章 ──────────────────────────────────────────────────────── */
.badge {
    border-radius: 6px; padding: 3px 10px; font-size: 11px;
    font-family: 'DM Mono', monospace; display: inline-block; font-weight: 600;
}
.b-run  { color: var(--go); background: var(--goD); border: 1px solid rgba(245,158,11,.3); }
.b-done { color: var(--gr); background: var(--grD); border: 1px solid rgba(34,211,160,.3); }
.b-wait { color: var(--cy); background: var(--cyD); border: 1px solid rgba(56,189,248,.3); }
.b-err  { color: var(--re); background: var(--reD); border: 1px solid rgba(244,63,94,.3); }

/* ── 风险等级 ────────────────────────────────────────────────────────────── */
.risk-low    { color: var(--gr); font-weight: 700; }
.risk-medium { color: var(--go); font-weight: 700; }
.risk-high   { color: var(--re); font-weight: 700; }

/* ── 脚本卡片：蓝色左边界 ───────────────────────────────────────────────── */
.script-card {
    background: rgba(99,102,241,.05);
    border: 1px solid rgba(99,102,241,.18);
    border-left: 3px solid var(--bl);
    border-radius: 10px; padding: 16px 20px; margin-bottom: 10px;
}
.script-card b { color: var(--bl2); }
.script-card p { color: var(--t1); margin: 6px 0 12px; line-height: 1.75; }

/* ── 开发者视图代码块 ────────────────────────────────────────────────────── */
.dev-raw {
    font-family: 'DM Mono', monospace;
    font-size: 11px; line-height: 1.6;
    background: rgba(2,6,24,.6); border: 1px solid var(--glass-bd);
    border-radius: 8px; padding: 12px 14px;
    overflow-x: auto; white-space: pre-wrap; color: var(--t1);
}

/* ── 历史记录行 ──────────────────────────────────────────────────────────── */
.hist-row {
    padding: 8px 12px; border-radius: 8px; margin-bottom: 6px;
    background: var(--glass-bg); border: 1px solid var(--glass-bd);
    font-size: 12px; color: var(--t1); cursor: pointer;
    transition: border-color .2s;
}
.hist-row:hover { border-color: var(--bl); }
.hist-row b { color: var(--t0) !important; }

/* ── 隐藏 Streamlit 冗余元素 ─────────────────────────────────────────────── */
#MainMenu, footer, header {
    display: none !important;
    height: 0 !important;
}
[data-testid="stDeployButton"],
[data-testid="stToolbarActions"],
[data-testid="manage-app-button"] {
    display: none !important;
}

/* ══════════════════════════════════════════════════════════════════════════
   物理点击按钮 — 蓝色 · 机械下压回弹
   active: scale(0.98) + translateY(3px) 双重物理感
   ══════════════════════════════════════════════════════════════════════════ */
.stButton > button {
    background: linear-gradient(135deg, #6366F1 0%, #4F46E5 100%) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 700 !important;
    padding: 10px 22px !important;
    cursor: pointer !important;
    font-family: 'Noto Sans SC', sans-serif !important;
    box-shadow: 0 4px 0 #2D267A, 0 6px 20px rgba(99,102,241,.34) !important;
    transform: translateY(0) scale(1) !important;
    transition: all .11s !important;
}
.stButton > button:hover {
    background: linear-gradient(135deg, #818CF8 0%, #6366F1 100%) !important;
    transform: translateY(-1px) scale(1) !important;
    box-shadow: 0 5px 0 #2D267A, 0 10px 28px rgba(99,102,241,.42) !important;
}
.stButton > button:active {
    transform: translateY(3px) scale(0.98) !important;
    box-shadow: 0 1px 0 #2D267A !important;
    transition: all .08s !important;
}

/* ── Tab bar ────────────────────────────────────────────────────────────── */
hr { border-color: rgba(100,140,255,.07) !important; }
[data-testid="stTabBar"] {
    background: transparent !important;
    border-bottom: 1px solid rgba(100,140,255,.10) !important;
}
[data-testid="stTab"] button {
    color: var(--t1) !important;
    font-family: 'Noto Sans SC', sans-serif !important;
}
[data-testid="stTab"][aria-selected="true"] button {
    color: var(--t0) !important;
    border-bottom-color: var(--bl) !important;
}

/* ── 文本输入框 ──────────────────────────────────────────────────────────── */
.stTextInput input, .stTextArea textarea {
    background: rgba(255,255,255,.03) !important;
    border: 1px solid var(--glass-bd) !important;
    color: var(--t0) !important;
    border-radius: 8px !important;
    font-family: 'Noto Sans SC', sans-serif !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color: var(--bl) !important;
    box-shadow: 0 0 0 2px var(--blD) !important;
}
.stSelectbox > div > div {
    background: rgba(255,255,255,.03) !important;
    border: 1px solid var(--glass-bd) !important;
    color: var(--t0) !important;
    border-radius: 8px !important;
}

/* ── Alert / Expander / Spinner ─────────────────────────────────────────── */
[data-testid="stAlert"] {
    background: var(--glass-bg) !important;
    border: 1px solid var(--glass-bd) !important;
    border-radius: 10px !important;
    color: var(--t0) !important;
}
[data-testid="stExpander"] {
    background: var(--glass-bg) !important;
    border: 1px solid var(--glass-bd) !important;
    border-radius: 10px !important;
}
/* expander 展开内容区：强制深色背景，覆盖 Streamlit 默认白色 */
[data-testid="stExpander"] > details {
    background: transparent !important;
}
[data-testid="stExpander"] > details > div,
[data-testid="stExpanderDetails"] {
    background: rgba(6,2,26,.85) !important;
    border-top: 1px solid var(--glass-bd) !important;
    border-radius: 0 0 10px 10px !important;
}
[data-testid="stExpander"] summary { color: var(--t0) !important; }
[data-testid="stSpinner"] { color: var(--bl) !important; }

/* ── section label（// 前缀）─────────────────────────────────────────────── */
.slbl {
    font-family: 'DM Mono', monospace; font-size: 10px;
    letter-spacing: .22em; color: var(--bl); margin-bottom: 8px;
    display: flex; align-items: center; gap: 6px;
}
.slbl::before { content: '//'; color: var(--t2); }

/* ── 分隔线 section 背景色块 ─────────────────────────────────────────────── */
.sec-alt {
    background: linear-gradient(180deg,
        rgba(99,102,241,.012) 0%,
        rgba(99,102,241,.018) 50%,
        rgba(99,102,241,.012) 100%);
    border-top: 1px solid rgba(100,140,255,.05);
    border-bottom: 1px solid rgba(100,140,255,.05);
    border-radius: 16px; padding: 20px 24px; margin-bottom: 16px;
}

/* ── 上传区域 ────────────────────────────────────────────────────────────── */
[data-testid="stFileUploadDropzone"] {
    background: var(--glass-bg) !important;
    border: 1px dashed rgba(99,102,241,.35) !important;
    border-radius: 12px !important;
    color: var(--t1) !important;
    transition: border-color .2s !important;
}
[data-testid="stFileUploadDropzone"]:hover {
    border-color: var(--bl) !important;
    background: var(--blD) !important;
}

/* ── Chat 区域 ───────────────────────────────────────────────────────────── */
[data-testid="stChatMessage"] {
    background: var(--glass-bg) !important;
    border: 1px solid var(--glass-bd) !important;
    border-radius: 12px !important;
    color: var(--t0) !important;
}
[data-testid="stChatInputContainer"] {
    background: rgba(1,4,18,.9) !important;
    border-top: 1px solid var(--glass-bd) !important;
}
[data-testid="stChatInputTextArea"] {
    background: rgba(255,255,255,.03) !important;
    color: var(--t0) !important;
    border: 1px solid var(--glass-bd) !important;
    border-radius: 8px !important;
}

/* ══════════════════════════════════════════════════════════════════════════
   动画关键帧
   ══════════════════════════════════════════════════════════════════════════ */
@keyframes vira-pulse {
    0%,100% { opacity:1;   transform: scale(1); }
    50%      { opacity:.40; transform: scale(0.82); }
}
@keyframes vira-pulse-bl {
    0%,100% { opacity:1;   transform: scale(1); }
    50%      { opacity:.38; transform: scale(0.80); }
}
.vira-badge-dot,
.vira-ndot,
.agdot {
    will-change: transform, opacity;
}
.pulse-dot-bl {
    width:7px; height:7px; border-radius:50%; background:var(--bl);
    display:inline-block; animation: vira-pulse-bl 2s infinite;
    will-change: transform, opacity;
}

/* ── 中文字体强调 / 工具类 ───────────────────────────────────────────────── */
.cn { font-family: 'Noto Sans SC', sans-serif; font-weight: 900; }
.mt-0  { margin-top: 0 !important; }
.mt-40 { margin-top: 40px !important; }
.mb-40 { margin-bottom: 40px !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Session State 初始化
# ══════════════════════════════════════════════════════════════════════════════

def _init_state() -> None:
    defaults = {
        "session_id":          str(uuid.uuid4())[:8],
        "api_key":             os.getenv("OPENAI_API_KEY", ""),
        "model":               "gpt-4o",
        "rag_text":            "",
        "workflow_result":     None,
        "image_data":          None,
        "image_name":          "",
        "chat_history":        [],
        "feedback_done":       set(),
        "selected_frame_idx":  0,
        "batch_results":       [],
        # ── 鉴权 ──────────────────────────────────────────────
        "authenticated":       False,
        "user_info":           None,
        "auth_mode":           "login",   # "login" | "signup"
        # ── AI 客服 ───────────────────────────────────────────
        "cs_open":             False,
        "cs_history":          [],
        # ── 爆款公式提炼 ──────────────────────────────────────
        "synthesis_result":    None,
        # ── 视频口播提取 ──────────────────────────────────────
        "transcript_result":   None,
        "transcript_filename": "",
        # ── 模板库 ────────────────────────────────────────────
        "template_applied":    "",   # 当前应用的模板名
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ══════════════════════════════════════════════════════════════════════════════
# 鉴权页渲染 + 守卫
# ══════════════════════════════════════════════════════════════════════════════

def _render_auth_page() -> None:
    """
    全屏登录 / 注册页。
    调用方在此函数返回后立即调用 st.stop()，阻止主 App 渲染。

    UI 策略：
      · 隐藏侧边栏 & 导航栏（CSS 覆盖）
      · 将主内容区宽度压缩至 440px，居中显示毛玻璃卡片
      · 使用 st.form 做表单提交，msg_slot 在表单上方展示错误/成功提示
    """
    # ── 鉴权页专属 CSS ─────────────────────────────────────────────────────────
    st.markdown("""
<style>
/* 隐藏侧边栏与顶部导航 */
[data-testid="stSidebar"],[data-testid="stSidebarNav"],
.vira-nav { display:none!important; }

/* 主容器：居中 + 限宽 */
[data-testid="stMainBlockContainer"] {
    max-width:460px!important;
    padding:0 16px 48px!important;
    margin:0 auto!important;
}

/* st.form 毛玻璃卡片 */
[data-testid="stForm"] {
    background:rgba(255,255,255,.04)!important;
    border:1px solid rgba(139,92,246,.22)!important;
    border-radius:16px!important;
    padding:28px 28px 20px!important;
    backdrop-filter:blur(28px)!important;
    -webkit-backdrop-filter:blur(28px)!important;
    box-shadow:0 8px 48px rgba(41,79,187,.18),
               inset 0 1px 0 rgba(255,255,255,.06)!important;
}

/* 表单内 label */
[data-testid="stForm"] label p {
    font-size:12px!important;
    color:#7C8FA6!important;
    letter-spacing:.04em!important;
}
</style>
""", unsafe_allow_html=True)

    mode      = st.session_state.get("auth_mode", "login")
    is_signup = (mode == "signup")
    title     = "创建账户" if is_signup else "欢迎回来"
    subtitle  = "填写信息，开始使用 VIRA 爆款侦察兵" if is_signup else "登录你的 VIRA 账户继续分析"

    # ── Logo + 标题 ────────────────────────────────────────────────────────────
    st.markdown(f"""
<div style="text-align:center;padding:52px 0 28px;">
  <div style="width:60px;height:60px;border-radius:16px;margin:0 auto 18px;
              background:linear-gradient(135deg,#6366F1 0%,#A855F7 100%);
              display:inline-flex;align-items:center;justify-content:center;
              font-size:28px;box-shadow:0 8px 36px rgba(99,102,241,.55);">✦</div>
  <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:23px;
              font-weight:800;color:#E2E8F0;margin-bottom:6px;">{title}</div>
  <div style="font-size:13px;color:#7C8FA6;line-height:1.6;">{subtitle}</div>
</div>
""", unsafe_allow_html=True)

    # ── 消息槽（表单上方，渲染错误/成功提示）─────────────────────────────────
    _msg_slot = st.empty()

    # ── 登录表单 ────────────────────────────────────────────────────────────
    if not is_signup:
        with st.form("vira_login_form", clear_on_submit=False):
            _email = st.text_input("邮箱地址", placeholder="name@example.com")
            _pwd   = st.text_input("密码",     placeholder="输入密码", type="password")
            _sub   = st.form_submit_button(
                "登录 →", use_container_width=True, type="primary"
            )
        if _sub:
            if not _email or not _pwd:
                _msg_slot.error("请填写邮箱和密码")
            else:
                try:
                    from services.auth import login as _auth_login
                    _ok, _msg, _info = _auth_login(_email, _pwd)
                    if _ok:
                        st.session_state.authenticated = True
                        st.session_state.user_info     = _info
                        st.rerun()
                    else:
                        _msg_slot.error(_msg)
                except Exception as _e:
                    _msg_slot.error(f"服务暂不可用：{_e}")

        _, _mid, _ = st.columns([1, 2, 1])
        with _mid:
            if st.button("还没有账户？立即注册", use_container_width=True, key="go_signup"):
                st.session_state.auth_mode = "signup"
                st.rerun()

    # ── 注册表单 ────────────────────────────────────────────────────────────
    else:
        with st.form("vira_signup_form", clear_on_submit=False):
            _name   = st.text_input("昵称（可选）",   placeholder="你的名字")
            _email  = st.text_input("邮箱地址 *",    placeholder="name@example.com")
            _pwd    = st.text_input("密码 *",        placeholder="至少 6 位", type="password")
            _pwd2   = st.text_input("确认密码 *",    placeholder="再输入一次", type="password")
            _invite = st.text_input("邀请码 *",      placeholder="请联系管理员获取")
            _sub    = st.form_submit_button(
                "注册账户 →", use_container_width=True, type="primary"
            )
        if _sub:
            # 校验邀请码（从 Secrets 或环境变量读取，不写死）
            _valid_invite = (
                st.secrets.get("INVITE_CODE")
                or os.getenv("INVITE_CODE", "")
            )
            if not _email or not _pwd:
                _msg_slot.error("请填写邮箱和密码")
            elif _pwd != _pwd2:
                _msg_slot.error("两次密码不一致")
            elif not _valid_invite or _invite.strip() != _valid_invite:
                _msg_slot.error("❌ 邀请码不正确，请联系管理员获取")
            else:
                try:
                    from services.auth import register as _auth_reg
                    _ok, _msg = _auth_reg(_email, _pwd, _name)
                    if _ok:
                        _msg_slot.success("✅ 注册成功，请登录")
                        st.session_state.auth_mode = "login"
                        st.rerun()
                    else:
                        _msg_slot.error(_msg)
                except Exception as _e:
                    _msg_slot.error(f"服务暂不可用：{_e}")

        _, _mid, _ = st.columns([1, 2, 1])
        with _mid:
            if st.button("已有账户？点击登录", use_container_width=True, key="go_login"):
                st.session_state.auth_mode = "login"
                st.rerun()

    # ── 页脚 ──────────────────────────────────────────────────────────────────
    st.markdown("""
<div style="text-align:center;margin-top:28px;font-size:11px;color:#3D4F68;
            padding-bottom:32px;">
  注册即代表同意 VIRA 使用条款与隐私政策
</div>
""", unsafe_allow_html=True)


# ── 鉴权守卫（未登录则展示鉴权页并停止渲染主 App）─────────────────────────────
if not st.session_state.authenticated:
    _render_auth_page()
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# 延迟导入工厂（避免 API Key 缺失时模块级崩溃）
# ══════════════════════════════════════════════════════════════════════════════

def _workflow(rag_text: str = ""):
    from core.workflow import VIRAWorkflow
    return VIRAWorkflow(
        api_key  = st.session_state.api_key,
        model    = st.session_state.model,
        rag_text = rag_text,
    )

def _feedback_store():
    from services.rag import FeedbackStore
    return FeedbackStore()

def _history_store():
    from services.rag import HistoryStore
    return HistoryStore()


# ══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════════════

def _safe(obj, *keys, default="—") -> Any:
    """安全地从嵌套对象/字典中提取值，不抛异常"""
    try:
        for k in keys:
            obj = getattr(obj, k) if hasattr(obj, k) else obj[k]
        return obj if obj not in (None, "") else default
    except Exception:
        return default


def _route_intent(query: str, wf) -> tuple[str, str, float]:
    """
    轻量级意图路由器。
    基于关键词命中率估算置信度，将用户问题映射到对应 Agent 上下文。

    Fallback 规则：confidence < 0.7 时，在回答前注入「仅供参考」提示。
    """
    q = query.lower()
    SCRIPT_KW   = {"脚本","文案","改写","重写","修改","cta","台词","开场"}
    COMPLY_KW   = {"合规","风险","违规","红线","限流","审核","规范","禁词"}
    VISUAL_KW   = {"视觉","画面","颜色","字体","布局","hook","首屏","吸引"}
    STRATEGY_KW = {"策略","ab","实验","建议","方案","置信","复刻","测试"}

    hits = (
        sum(1 for k in SCRIPT_KW   if k in q) +
        sum(1 for k in COMPLY_KW   if k in q) +
        sum(1 for k in VISUAL_KW   if k in q) +
        sum(1 for k in STRATEGY_KW if k in q)
    )
    confidence = min(0.95, 0.50 + hits * 0.15)
    ctx_parts: list[str] = []

    def _add(r):
        if r and r.success:
            ctx_parts.append(f"【{r.agent_name}】\n{json.dumps(r.data, ensure_ascii=False, indent=2)}")

    if   any(k in q for k in SCRIPT_KW):
        intent = "脚本优化 → Agent 2"
        _add(wf.commerce);  _add(wf.visual)
    elif any(k in q for k in COMPLY_KW):
        intent = "合规查询 → Agent 3"
        _add(wf.compliance)
    elif any(k in q for k in VISUAL_KW):
        intent = "视觉分析 → Agent 1"
        _add(wf.visual)
    elif any(k in q for k in STRATEGY_KW):
        intent = "策略决策 → Agent 4"
        _add(wf.strategy)
    else:
        intent = "综合问答 → 全上下文"
        confidence = max(0.50, confidence - 0.10)
        for r in [wf.visual, wf.commerce, wf.compliance, wf.strategy]:
            _add(r)

    if st.session_state.rag_text:
        ctx_parts.append(f"【品牌知识库（节选）】\n{st.session_state.rag_text[:400]}")

    context = "\n\n".join(ctx_parts) or "（请先完成视频分析，再使用智能问答）"
    return intent, context, confidence


# ══════════════════════════════════════════════════════════════════════════════
# Agent 4 专家决策卡片
# ══════════════════════════════════════════════════════════════════════════════

def _render_strategy_card(wf) -> None:
    """Agent 4 · 策略执行官的完整输出，始终置于 Tab 区域上方"""
    if not (wf and wf.strategy and wf.strategy.success):
        return

    d        = wf.strategy.data
    score    = d.get("confidence_score", 0)
    verdict  = d.get("verdict", "—")
    ab       = d.get("ab_test", {})
    summary  = d.get("executive_summary", "—")
    warning  = d.get("risk_warning", "")
    insights = d.get("key_insights", [])

    # 置信度颜色（深色主题语义化：绿/金/红）
    if score >= 75:
        clr, bg, bd = "#00C97A", "rgba(0,201,122,.08)", "rgba(0,201,122,.30)"
    elif score >= 50:
        clr, bg, bd = "#F0A500", "rgba(240,165,0,.08)",  "rgba(240,165,0,.30)"
    else:
        clr, bg, bd = "#FF3D55", "rgba(255,61,85,.08)",  "rgba(255,61,85,.30)"

    st.markdown("---")
    st.markdown(f"""
<div style="background:{bg};border:1px solid {bd};border-top:2px solid {clr};
            border-radius:14px;padding:26px 30px;margin:8px 0 20px;position:relative;overflow:hidden;">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
              background:linear-gradient(90deg,transparent,{clr},transparent);opacity:.5;"></div>
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:18px;">
    <div style="width:68px;height:68px;border-radius:12px;flex-shrink:0;
                background:rgba(255,255,255,.04);border:1px solid {bd};
                display:flex;align-items:center;justify-content:center;
                font-size:26px;font-weight:800;color:{clr};
                font-family:'Plus Jakarta Sans',sans-serif;">
      {score}
    </div>
    <div>
      <div style="font-size:9px;letter-spacing:.18em;color:#7C8FA6;
                  font-family:'DM Mono',monospace;margin-bottom:5px;">
        // AGENT 4 · 策略执行官 · VIRA EXPERT DECISION
      </div>
      <div style="font-size:1.15rem;font-weight:700;color:#E2E8F0;line-height:1.3;">
        {verdict}
      </div>
    </div>
  </div>
  <div style="font-size:13px;color:#7C8FA6;line-height:1.8;
              border-top:1px solid rgba(255,255,255,.06);padding-top:14px;">{summary}</div>
</div>
""", unsafe_allow_html=True)

    # A/B Test 双列
    if ab:
        st.markdown(
            '<div style="font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;'
            'font-size:15px;color:#E2E8F0;margin:12px 0 10px;">🧪 A/B Test 实验设计</div>',
            unsafe_allow_html=True
        )
        ctrl, test = ab.get("control_group", {}), ab.get("test_group", {})
        c1, c2 = st.columns(2)
        with c1:
            keeps = "".join(
                f'<div style="margin:4px 0;color:#E2E8F0;font-size:12px;">✓ {el}</div>'
                for el in ctrl.get("keep_elements", [])
            )
            st.markdown(f"""
<div class="glass" style="border-left:2px solid #38BDF8;">
  <div style="font-size:9px;color:#38BDF8;font-family:'DM Mono',monospace;
              letter-spacing:.14em;margin-bottom:10px;font-weight:700;">
    // CONTROL GROUP · 保留元素</div>
  <div style="font-size:12px;color:#7C8FA6;margin-bottom:8px;">{ctrl.get('description','—')}</div>
  <div>{keeps}</div>
  <div style="font-size:11px;color:#3D4F68;margin-top:10px;
              border-top:1px solid rgba(255,255,255,.06);padding-top:8px;">
    保留原因：{ctrl.get('rationale','—')}</div>
</div>""", unsafe_allow_html=True)

        with c2:
            changes = "".join(
                f'<div style="margin:4px 0;color:#E2E8F0;font-size:12px;">→ {el}</div>'
                for el in test.get("change_elements", [])
            )
            st.markdown(f"""
<div class="glass" style="border-left:2px solid #6366F1;">
  <div style="font-size:9px;color:#6366F1;font-family:'DM Mono',monospace;
              letter-spacing:.14em;margin-bottom:10px;font-weight:700;">
    // TEST GROUP · 改动假设</div>
  <div style="font-size:12px;color:#7C8FA6;margin-bottom:8px;">{test.get('description','—')}</div>
  <div>{changes}</div>
  <div style="font-size:11px;color:#3D4F68;margin-top:10px;
              border-top:1px solid rgba(100,140,255,.08);padding-top:8px;">
    效果假设：{test.get('hypothesis','—')}</div>
</div>""", unsafe_allow_html=True)

        mc1, mc2 = st.columns(2)
        mc1.info(f"📊 **成功指标：** {ab.get('success_metric','—')}")
        mc2.info(f"⏱ **测试周期：** {ab.get('test_duration','—')}")

    # 关键洞察
    if insights:
        st.markdown(
            '<div style="font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;'
            'font-size:15px;color:#E2E8F0;margin:12px 0 10px;">💡 关键战略洞察</div>',
            unsafe_allow_html=True
        )
        for i, ins in enumerate(insights, 1):
            st.markdown(
                f'<div class="glass" style="padding:12px 18px;margin-bottom:8px;">'
                f'<span style="color:#6366F1;font-family:\'DM Mono\',monospace;'
                f'font-size:11px;font-weight:700;">#{i:02d}</span>'
                f'&nbsp;&nbsp;<span style="color:#7C8FA6;font-size:13px;">{ins}</span></div>',
                unsafe_allow_html=True,
            )

    if warning:
        st.warning(f"⚠️ **风险提示：** {warning}")


# ══════════════════════════════════════════════════════════════════════════════
# 侧边栏
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    # ── 用户信息 + 退出登录 ────────────────────────────────────────────────────
    _u = st.session_state.get("user_info") or {}
    _display = _u.get("display_name") or _u.get("email", "用户")
    _email_s = _u.get("email", "")
    st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
            padding:8px 0 12px;border-bottom:1px solid rgba(139,92,246,.10);
            margin-bottom:12px;">
  <div style="display:flex;align-items:center;gap:8px;">
    <div style="width:30px;height:30px;border-radius:8px;flex-shrink:0;
                background:linear-gradient(135deg,#6366F1,#A855F7);
                display:flex;align-items:center;justify-content:center;
                font-size:13px;font-weight:700;color:#fff;">
      {_display[:1].upper()}
    </div>
    <div>
      <div style="font-size:12px;font-weight:700;color:#E2E8F0;
                  line-height:1.3;">{_display}</div>
      <div style="font-size:10px;color:#3D4F68;">{_email_s}</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)
    if st.button("退出登录", use_container_width=True, key="sidebar_logout"):
        for _k in ("authenticated", "user_info", "workflow_result", "image_data",
                   "chat_history", "feedback_done", "batch_results"):
            st.session_state[_k] = (
                False if _k == "authenticated" else
                None  if _k in ("user_info", "workflow_result", "image_data") else
                []    if _k in ("chat_history", "batch_results") else
                set()
            )
        st.session_state.auth_mode = "login"
        st.rerun()

    st.markdown("""
<div style="display:flex;align-items:center;gap:8px;padding:4px 0 14px;">
  <div style="width:7px;height:7px;border-radius:50%;background:#6366F1;
              animation:vira-pulse-bl 2s infinite;box-shadow:0 0 10px #6366F1;"></div>
  <span style="font-family:'Plus Jakarta Sans',sans-serif;font-size:16px;
               font-weight:800;letter-spacing:.04em;color:#E2E8F0;">VIRA</span>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.12em;
               color:#3D4F68;">· 爆款侦察兵</span>
</div>
""", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.18em;'
        'color:#3D4F68;margin-bottom:8px;">// API 配置</div>',
        unsafe_allow_html=True
    )
    api_in = st.text_input(
        "OpenAI API Key", type="password",
        value=st.session_state.api_key, placeholder="sk-...",
        help="platform.openai.com 获取，或写入 .env 文件",
    )
    if api_in:
        st.session_state.api_key = api_in

    st.session_state.model = st.selectbox(
        "模型", ["gpt-4o", "gpt-4o-mini"], index=0,
        help="gpt-4o 视觉最强；gpt-4o-mini 更快省 Token",
    )

    st.divider()

    st.markdown(
        '<div style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.18em;'
        'color:#3D4F68;margin-bottom:4px;">// 品牌知识库</div>'
        '<div style="font-size:13px;font-weight:600;color:#E2E8F0;margin-bottom:4px;">'
        'RAG 知识库注入</div>',
        unsafe_allow_html=True
    )
    st.caption("粘贴品牌法则/产品卖点，Agent 2 将交叉比对后生成专属脚本。")
    rag_in = st.text_area(
        "知识库内容", value=st.session_state.rag_text, height=150,
        placeholder="例：\n- 核心用户：25-35岁职场女性\n- 爆款公式：痛点→解决→证明→CTA\n- 禁用词：最好、第一",
        label_visibility="collapsed",
    )
    if rag_in != st.session_state.rag_text:
        st.session_state.rag_text = rag_in
        st.toast("知识库已更新 ✓", icon="📚")

    # ── 保存为模板 ────────────────────────────────────────────────────────
    st.markdown(
        '<div style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.18em;'
        'color:#3D4F68;margin:8px 0 6px;">// 模板操作</div>',
        unsafe_allow_html=True,
    )
    _tpl_c1, _tpl_c2 = st.columns(2)
    with _tpl_c1:
        if st.button("💾 保存为模板", use_container_width=True, key="save_template_btn"):
            if not st.session_state.rag_text.strip():
                st.toast("请先在知识库中填写内容", icon="⚠️")
            else:
                st.session_state["_show_save_tpl"] = True

    with _tpl_c2:
        if st.button("📂 应用模板", use_container_width=True, key="load_template_btn"):
            st.session_state["_show_load_tpl"] = True

    # 保存模板弹出表单
    if st.session_state.get("_show_save_tpl"):
        with st.form("save_tpl_form"):
            _tpl_name = st.text_input("模板名称", placeholder="如：美妆竞品分析")
            _tpl_desc = st.text_input("简介（可选）", placeholder="适用场景说明")
            _tpl_tags = st.text_input("标签（逗号分隔）", placeholder="美妆,口红,护肤")
            _tpl_sub  = st.form_submit_button("保存", use_container_width=True, type="primary")
        if _tpl_sub:
            if _tpl_name.strip():
                from services.template_store import save_template as _save_tpl
                _syn = st.session_state.get("synthesis_result")
                _save_tpl(
                    name            = _tpl_name,
                    rag_text        = st.session_state.rag_text,
                    description     = _tpl_desc,
                    tags            = [t for t in _tpl_tags.split(",") if t.strip()],
                    viral_formula   = _syn.data.get("viral_formula", "") if _syn and _syn.success else "",
                    created_by      = (st.session_state.get("user_info") or {}).get("email", ""),
                )
                st.session_state["_show_save_tpl"] = False
                st.toast(f"✅ 模板「{_tpl_name}」已保存", icon="💾")
                st.rerun()
            else:
                st.warning("请填写模板名称")

    # 加载模板面板
    if st.session_state.get("_show_load_tpl"):
        from services.template_store import list_templates as _list_tpl
        _all_tpls = _list_tpl()
        if not _all_tpls:
            st.caption("暂无模板，先保存一个吧")
            st.session_state["_show_load_tpl"] = False
        else:
            st.markdown(
                '<div style="font-size:11px;color:#7C8FA6;margin-bottom:6px;">选择要应用的模板：</div>',
                unsafe_allow_html=True,
            )
            for _t in _all_tpls[:6]:
                _tc1, _tc2 = st.columns([3, 1])
                with _tc1:
                    st.markdown(
                        f'<div style="font-size:11px;color:#E2E8F0;font-weight:600;">'
                        f'{_t["name"]}</div>'
                        f'<div style="font-size:10px;color:#3D4F68;">'
                        f'{_t.get("description","") or " · ".join(_t.get("tags",[])[:3])}</div>',
                        unsafe_allow_html=True,
                    )
                with _tc2:
                    if st.button("应用", key=f"apply_tpl_{_t['id']}"):
                        st.session_state.rag_text         = _t["rag_text"]
                        st.session_state.template_applied  = _t["name"]
                        st.session_state["_show_load_tpl"] = False
                        st.toast(f"✅ 已应用模板「{_t['name']}」", icon="📂")
                        st.rerun()
            if st.button("关闭", key="close_load_tpl"):
                st.session_state["_show_load_tpl"] = False
                st.rerun()

    # 当前应用模板提示
    if st.session_state.get("template_applied"):
        st.markdown(
            f'<div style="font-size:10px;color:#818CF8;margin-top:4px;">'
            f'📂 已应用：{st.session_state.template_applied}</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # 历史记录（侧边栏）
    st.markdown(
        '<div style="font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.18em;'
        'color:#3D4F68;margin-bottom:4px;">// 历史记录</div>'
        '<div style="font-size:13px;font-weight:600;color:#E2E8F0;margin-bottom:8px;">'
        '历史分析记录</div>',
        unsafe_allow_html=True
    )
    try:
        recent = _history_store().get_recent(8)
        stats  = _history_store().get_stats()
        st.caption(f"共 {stats['total']} 条 · 平均置信度 {stats['avg_confidence']}")
        if recent:
            for rec in recent:
                risk_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(
                    rec.get("compliance_risk", ""), "⚪"
                )
                ts = (rec.get("created_at") or "")[:16].replace("T", " ")
                st.markdown(
                    f'<div class="hist-row">'
                    f'{risk_emoji} <b style="color:#111827;">{rec.get("image_name","—")[:20]}</b><br>'
                    f'<span style="color:#6B7280;font-size:10px;">{ts} · '
                    f'置信度 {rec.get("confidence_score","—")} · '
                    f'{rec.get("total_elapsed_ms","—")}ms</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无记录，完成首次分析后将自动保存")
    except Exception as e:
        st.caption(f"历史记录不可用：{e}")

    st.divider()

    try:
        fb_stats = _feedback_store().get_stats()
        st.markdown(
            f"**反馈统计** 👍 {fb_stats['good']} · "
            f"👎 {fb_stats['bad_cases']} Bad Cases"
        )
    except Exception:
        pass

    st.caption(f"Session `{st.session_state.session_id}`")
    if st.button("🗑 清除当前分析"):
        for k in ("workflow_result", "image_data", "chat_history", "feedback_done"):
            st.session_state[k] = None if k not in ("chat_history", "feedback_done") else ([] if k == "chat_history" else set())
        st.session_state.batch_results = []
        st.session_state.selected_frame_idx = 0
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# 主界面
# ══════════════════════════════════════════════════════════════════════════════

# ── 固定导航栏（position:fixed，始终置顶）────────────────────────────────────
st.markdown("""
<nav class="vira-nav">
  <a class="vira-nlogo" href="#">
    <div class="vira-ndot"></div>VIRA
  </a>
  <div class="vira-nlinks">
    <a href="#">工作原理</a>
    <a href="#">AI智能体</a>
    <a href="#">定价</a>
  </div>
  <button class="vira-ncta">免费试用 →</button>
</nav>
""", unsafe_allow_html=True)

st.markdown("""
<div class="vira-hero">
  <div class="vira-badge">
    <div class="vira-badge-dot"></div>
    多模态 · RAG知识库 · 4个AI智能体协同
  </div>
  <h1 class="vira-h1">
    <span class="cn" style="opacity:.85;font-size:.72em;letter-spacing:.01em;">上传竞品截图，</span><br>
    <span class="vira-h1-fire cn">30秒知道为什么它爆</span>
  </h1>
  <p class="vira-sub">
    不是让AI帮你<strong>写内容</strong>——<br>
    而是真正看懂竞品，告诉你爆款密码在哪，<strong>你的版本怎么改</strong>。
  </p>
  <div class="vira-proof">
    <div>🔥 <span class="vira-proof-num">2,400+</span> 创作者在用</div>
    <div>⚡ 平均 <span class="vira-proof-num">25秒</span> 出完整报告</div>
    <div>📈 完播率平均 <span class="vira-proof-num">+28%</span> 改版后</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── 指标大屏（仅在有分析结果时显示）─────────────────────────────────────────
wf = st.session_state.workflow_result
if wf:
    st.markdown("""
<div style="padding:28px 0 14px;">
  <div class="slbl">实时分析仪表盘</div>
</div>""", unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("HOOK 吸睛指数",   f"{_safe(wf,'visual','data','hook_score')}/100",    help="Agent 1 · Hook 质量评分")
    m2.metric("带货转化潜力",     f"{_safe(wf,'commerce','data','conversion_potential')}/100", help="Agent 2 · 商业转化潜力")
    m3.metric("合规风险等级",     str(_safe(wf,"compliance","data","risk_level")),     help="Agent 3 · LOW / MEDIUM / HIGH")
    m4.metric("成功置信度",       f"{_safe(wf,'strategy','data','confidence_score')}/100", help="Agent 4 · 复刻成功综合置信度")
    st.markdown('<hr class="vira-hr" style="margin:20px 0;">', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# 上传区域（未分析时显示）
# ══════════════════════════════════════════════════════════════════════════════

ALLOWED_TYPES  = ["jpg", "jpeg", "png", "webp"]
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB 硬限制

if not st.session_state.workflow_result:
    # ── Agent 专家卡片展示组 ──────────────────────────────────────────────────
    st.markdown("""
<div class="vira-sec">
  <div class="slbl">AI 智能体</div>
  <div class="vira-sec-sh cn">4个专家Agent<br>各司其职，协同出击</div>
  <p class="vira-sec-sp">每个Agent都有专属的知识库和分析视角，结合在一起才是完整的判断。</p>
</div>

<div class="ag3col">
  <!-- Agent 1 · 视觉拆解师 -->
  <div class="agcard ag-cy">
    <div class="agid" style="color:var(--cy)">
      <div class="agdot" style="background:var(--cy)"></div>AGENT 1
    </div>
    <span class="agico">👁</span>
    <div class="agname">视觉拆解师</div>
    <div class="agsub">逐帧分析画面，比人眼更精准</div>
    <div class="agdesc">分析前3秒Hook类型、画面色彩情绪、文字布局质量——和爆款规律知识库对比，给出 Hook 评分与视觉质量分。</div>
    <div class="agtags">
      <span class="agtag" style="border-color:rgba(56,189,248,.3);color:var(--cy)">Hook评分</span>
      <span class="agtag" style="border-color:rgba(56,189,248,.3);color:var(--cy)">色彩情绪</span>
      <span class="agtag" style="border-color:rgba(56,189,248,.3);color:var(--cy)">视觉质量</span>
    </div>
  </div>

  <!-- Agent 3 · 合规排雷兵 -->
  <div class="agcard ag-re">
    <div class="agid" style="color:var(--re)">
      <div class="agdot" style="background:var(--re)"></div>AGENT 3
    </div>
    <span class="agico">🛡</span>
    <div class="agname">合规排雷兵</div>
    <div class="agsub">发布前的最后一道防线</div>
    <div class="agdesc">比对平台合规规则库（TikTok/抖音），精确识别极限用语、医疗声称、金融承诺等高风险内容，给出风险级别和修改建议。</div>
    <div class="agtags">
      <span class="agtag" style="border-color:rgba(255,61,85,.3);color:var(--re)">违规词检测</span>
      <span class="agtag" style="border-color:rgba(255,61,85,.3);color:var(--re)">风险分级</span>
      <span class="agtag" style="border-color:rgba(255,61,85,.3);color:var(--re)">修改建议</span>
    </div>
  </div>

  <!-- Agent 2 · 转化精算师 -->
  <div class="agcard ag-pu">
    <div class="agid" style="color:var(--pu)">
      <div class="agdot" style="background:var(--pu)"></div>AGENT 2
    </div>
    <span class="agico">📈</span>
    <div class="agname">转化精算师</div>
    <div class="agsub">RAG知识库增强，生成3套改版脚本</div>
    <div class="agdesc">结合视觉分析结果与品牌知识库，评估病毒传播潜力与商业转化潜力，输出3套可直接执行的重构脚本。</div>
    <div class="agtags">
      <span class="agtag" style="border-color:rgba(168,85,247,.3);color:var(--pu)">病毒预测</span>
      <span class="agtag" style="border-color:rgba(168,85,247,.3);color:var(--pu)">RAG增强</span>
      <span class="agtag" style="border-color:rgba(168,85,247,.3);color:var(--pu)">脚本重构</span>
    </div>
  </div>
</div>

<!-- Agent 4 · 策略执行官 全宽卡 -->
<div class="agd" style="margin-bottom:24px;">
  <div class="agd-inner">
    <div class="agd-left">
      <div class="agid" style="color:var(--bl)">
        <div class="agdot" style="background:var(--bl)"></div>AGENT 4 · 综合汇总
      </div>
      <span class="agico">✦</span>
      <div class="agname">策略执行官</div>
      <div class="agsub" style="color:var(--t1)">读取 A1+A2+A3 全部结果，输出最终战略裁决</div>
      <div class="agdesc">综合三个专家Agent的分析，给出成功置信度评分（0-100）、A/B Test 实验方案设计，以及前3秒改法、视觉升级点、文案优化方向的完整改版指令。</div>
    </div>
    <div class="agd-right">
      <div class="dout-lbl">OUTPUT EXAMPLE</div>
      <div class="dgrade-row">
        <div class="dgrade-box">A</div>
        <div class="dgrade-info"><strong>置信度 88/100 · 强烈建议复刻</strong><br>改版后预计 +24% 完播率</div>
      </div>
      <div class="dreco">A/B Test 方案：将第2秒文字换为反常识结论式开场，删除<strong>"最佳"等极限词</strong>，在视频第8秒增加利益点强化留存……</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    st.markdown("""
<hr class="vira-hr">
<div class="vira-sec">
  <div class="slbl">开始分析</div>
  <div class="vira-sec-sh cn">上传截图或视频<br>启动四 Agent 并发分析</div>
  <p class="vira-sec-sp">
    支持上传竞品截图（批量）或视频文件（自动提取口播文案）。
    上传视频后 AI 自动用 Whisper 转录口播内容，辅助脚本分析。
  </p>
</div>
""", unsafe_allow_html=True)

    # ── 视频口播提取区（独立模块，不影响图片分析流程）────────────────────────
    with st.expander("🎬 视频口播提取（上传视频 → Whisper 自动转录）", expanded=False):
        _vid_col, _vid_tip = st.columns([2, 1])
        with _vid_col:
            _vid_file = st.file_uploader(
                "上传视频文件（MP4 · MOV · WebM · ≤ 50 MB）",
                type=["mp4", "mov", "webm", "avi", "m4a", "mp3", "wav"],
                label_visibility="collapsed",
                key="video_uploader",
            )
        with _vid_tip:
            st.markdown("""
<div class="glass" style="border-left:2px solid var(--pu);padding:14px 16px;">
  <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;
              color:var(--pu);margin-bottom:8px;">// 视频口播提取</div>
  <div style="font-size:12px;color:var(--t1);line-height:1.8;">
    · 上传竞品视频，AI 自动转录口播文案<br>
    · 转录结果可作为脚本参考<br>
    · 支持中文/英文自动识别<br>
    · 需要 OpenAI API Key（Whisper）
  </div>
</div>""", unsafe_allow_html=True)

        if _vid_file:
            _vid_raw = _vid_file.read()
            _vid_size_mb = len(_vid_raw) / 1024 / 1024
            if _vid_size_mb > 50:
                st.warning(f"⚠️ 文件过大（{_vid_size_mb:.1f} MB），建议压缩后上传")
            else:
                st.markdown(
                    f'<div style="font-size:12px;color:#7C8FA6;margin:6px 0;">'
                    f'已载入：{_vid_file.name} · {_vid_size_mb:.1f} MB</div>',
                    unsafe_allow_html=True,
                )
                if not st.session_state.api_key:
                    st.warning("⚠️ 请先在侧边栏填入 OpenAI API Key")
                else:
                    _lang_opt = st.selectbox(
                        "语言（留空自动检测）",
                        ["自动检测", "中文 (zh)", "英文 (en)", "日文 (ja)"],
                        key="whisper_lang",
                    )
                    _lang_map = {
                        "自动检测": None, "中文 (zh)": "zh",
                        "英文 (en)": "en", "日文 (ja)": "ja",
                    }
                    if st.button("🎙 提取口播文案", key="run_transcript", type="primary"):
                        with st.spinner("Whisper 转录中..."):
                            from services.transcript import extract_transcript
                            _tr = extract_transcript(
                                file_bytes=_vid_raw,
                                filename=_vid_file.name,
                                api_key=st.session_state.api_key,
                                language=_lang_map[_lang_opt],
                            )
                            st.session_state.transcript_result   = _tr
                            st.session_state.transcript_filename = _vid_file.name

                    _tr_res = st.session_state.get("transcript_result")
                    if _tr_res:
                        if _tr_res["error"]:
                            st.error(f"转录失败：{_tr_res['error']}")
                        else:
                            _dur = (
                                f"时长 {_tr_res['duration_s']}s · "
                                if _tr_res["duration_s"] > 0 else ""
                            )
                            st.success(
                                f"✅ 转录完成 · {_dur}语言：{_tr_res['language']} · "
                                f"方式：{_tr_res['method']}"
                            )
                            st.markdown(
                                f'<div class="glass" style="padding:14px 18px;margin-top:8px;">'
                                f'<div style="font-family:\'DM Mono\',monospace;font-size:9px;'
                                f'letter-spacing:.15em;color:#A855F7;margin-bottom:8px;">'
                                f'// 口播文案 · {st.session_state.transcript_filename}</div>'
                                f'<div style="font-size:13px;color:#E2E8F0;line-height:1.85;'
                                f'white-space:pre-wrap;">{_tr_res["transcript"]}</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            # 提供复制 / 写入 RAG 两个操作
                            _ta1, _ta2 = st.columns(2)
                            with _ta1:
                                st.download_button(
                                    "⬇️ 下载转录文本",
                                    data=_tr_res["transcript"],
                                    file_name=Path(st.session_state.transcript_filename).stem + "_transcript.txt",
                                    mime="text/plain",
                                    use_container_width=True,
                                )
                            with _ta2:
                                if st.button("📚 写入 RAG 知识库", use_container_width=True, key="transcript_to_rag"):
                                    st.session_state.rag_text += (
                                        f"\n\n【竞品口播 · {st.session_state.transcript_filename}】\n"
                                        + _tr_res["transcript"]
                                    )
                                    st.toast("已追加到 RAG 知识库 ✓", icon="📚")

    up_col, tip_col = st.columns([2, 1])

    with up_col:
        # ── 多图上传（accept_multiple_files=True）────────────────────────────
        uploaded_files = st.file_uploader(
            "支持 JPG · PNG · WebP（≤ 20 MB · 可多选）",
            type=ALLOWED_TYPES,
            accept_multiple_files=True,
            label_visibility="collapsed",
        )

    with tip_col:
        st.markdown("""
<div class="glass" style="border-left:2px solid var(--bl);">
  <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;
              color:var(--bl);margin-bottom:10px;">// 使用建议</div>
  <div style="font-size:12px;color:var(--t1);line-height:1.85;">
    · 可上传多张截图，点击选中一张分析<br>
    · 建议截取视频开场、高潮、CTA 等节点<br>
    · 截图需清晰可见文字和主体画面<br>
    · 分辨率越高，AI 分析越精准
  </div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.06);
              font-size:11px;color:var(--t2);">
    支持 JPG · PNG · WebP · 单张 ≤ 20 MB
  </div>
</div>""", unsafe_allow_html=True)

    # ── 多图校验、缩略图网格、帧选择 ────────────────────────────────────────
    if uploaded_files:
        # 初始化帧选择 session state
        if "selected_frame_idx" not in st.session_state:
            st.session_state.selected_frame_idx = 0

        # 逐一校验所有上传文件
        valid_frames: list[tuple[str, bytes, Any]] = []
        for uf in uploaded_files:
            ext = Path(uf.name).suffix.lower().lstrip(".")
            if ext not in ALLOWED_TYPES:
                st.warning(f"⚠️ 已跳过不支持的文件：{uf.name}")
                continue
            raw = uf.read()
            if len(raw) > MAX_FILE_BYTES:
                st.warning(f"⚠️ 已跳过超大文件：{uf.name}（{len(raw)//1024//1024}MB）")
                continue
            try:
                img_obj = Image.open(io.BytesIO(raw))
                img_obj.verify()
                img_obj = Image.open(io.BytesIO(raw))
                valid_frames.append((uf.name, raw, img_obj))
            except Exception:
                st.warning(f"⚠️ 已跳过损坏文件：{uf.name}")

        if not valid_frames:
            st.error("❌ 没有可用的图片，请重新上传。")
            st.stop()

        # ── 帧缩略图网格（仿 HTML 里的 Mock Screen 帧选择区）────────────────
        n = len(valid_frames)
        # 确保 selected_frame_idx 不越界
        if st.session_state.selected_frame_idx >= n:
            st.session_state.selected_frame_idx = 0

        st.markdown(f"""
<div style="display:flex;align-items:center;gap:12px;margin:16px 0 10px;flex-wrap:wrap;">
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:var(--t2);">
    // 已载入 {n} 帧
  </span>
  <span style="font-size:11px;color:var(--t1);">
    · 点击按钮将一键分析全部帧
  </span>
  <span style="font-family:'DM Mono',monospace;font-size:9px;padding:2px 8px;border-radius:4px;
               background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.28);
               color:#818CF8;letter-spacing:.08em;">批量并发分析 · 每帧独立出报告</span>
</div>""", unsafe_allow_html=True)

        # 每行最多 4 列
        cols_per_row = min(n, 4)
        frame_cols = st.columns(cols_per_row)
        for i, (fname, raw_b, img_obj) in enumerate(valid_frames):
            col = frame_cols[i % cols_per_row]
            with col:
                is_selected = (i == st.session_state.selected_frame_idx)
                # 选中帧用橙色边框高亮（冷暖对撞，强视觉焦点）
                border_style = (
                    "border:2px solid #FF6000;box-shadow:0 0 18px rgba(255,96,0,.45);"
                    if is_selected else
                    "border:1px solid rgba(139,92,246,.14);"
                )
                st.markdown(
                    f'<div style="{border_style}border-radius:10px;overflow:hidden;'
                    f'margin-bottom:6px;cursor:pointer;transition:all .2s;">',
                    unsafe_allow_html=True
                )
                st.image(img_obj, use_container_width=True)
                st.markdown("</div>", unsafe_allow_html=True)
                frame_label = f"F{i+1} · {fname[:14]}{'…' if len(fname)>14 else ''}"
                if is_selected:
                    st.markdown(
                        f'<div style="text-align:center;font-family:\'DM Mono\',monospace;'
                        f'font-size:10px;color:#FF6000;font-weight:700;margin-bottom:4px;">'
                        f'▶ {frame_label}</div>',
                        unsafe_allow_html=True
                    )
                else:
                    if st.button(f"选 F{i+1}", key=f"sel_frame_{i}", use_container_width=True):
                        st.session_state.selected_frame_idx = i
                        st.rerun()

        # 当前选中帧
        sel_name, sel_bytes, sel_img = valid_frames[st.session_state.selected_frame_idx]
        st.session_state.image_data = sel_bytes
        st.session_state.image_name = sel_name

        # 待分析帧列表信息行
        _names_preview = "、".join(f[0][:12] for f in valid_frames[:4])
        if n > 4:
            _names_preview += f" 等{n}张"
        st.markdown(
            f'<div style="background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.22);'
            f'border-radius:10px;padding:10px 16px;margin:10px 0 16px;'
            f'font-size:12px;color:#E2E8F0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
            f'<span style="color:#818CF8;font-weight:700;">▶ 待分析：</span>'
            f'<span style="font-family:\'DM Mono\',monospace;color:#7C8FA6;">{_names_preview}</span>'
            f'<span style="color:#3D4F68;">· 共 {n} 张，点击下方按钮开始</span>'
            f'</div>',
            unsafe_allow_html=True
        )

        # ── 兼容单图路径：把 uploaded 变量指向选中帧 ────────────────────────
        uploaded = True   # 保持后续逻辑正常触发

        if not st.session_state.api_key:
            st.warning("⚠️ 请在左侧侧边栏填入 OpenAI API Key")
        else:
            # ========== 🚀 批量分析核心执行逻辑 ==========
            if st.button(f"🚀 启动 VIRA 四 Agent 分析（全部 {n} 张）", type="primary", use_container_width=True):
                _batch_new: list[dict] = []

                with st.status(f"🤖 VIRA 批量分析中（共 {n} 张）...", expanded=True) as status_ctx:
                    _progress = st.progress(0, text="准备中...")

                    for _img_idx, (_fname, _raw_b, _img_obj) in enumerate(valid_frames):
                        # ── 当前帧标题 ────────────────────────────────────────
                        st.markdown(
                            f'<div style="font-size:11px;color:#818CF8;font-family:\'DM Mono\','
                            f'monospace;margin:10px 0 4px;letter-spacing:.06em;">'
                            f'▶ 第 {_img_idx+1}/{n} 张 · <span style="color:#E2E8F0;">{_fname}</span></div>',
                            unsafe_allow_html=True,
                        )

                        _sv  = st.empty()
                        _sc  = st.empty()
                        _sco = st.empty()
                        _sst = st.empty()

                        _sv.markdown(
                            '<span class="badge b-run">AGENT 1 · 视觉拆解师 → 分析中</span>',
                            unsafe_allow_html=True)
                        _sc.markdown(
                            '<span class="badge b-run">AGENT 3 · 合规排雷兵 → 分析中</span>',
                            unsafe_allow_html=True)
                        _sco.markdown(
                            '<span class="badge b-wait">AGENT 2 · 转化精算师 → 等待视觉结果...</span>',
                            unsafe_allow_html=True)
                        _sst.markdown(
                            '<span class="badge b-wait">AGENT 4 · 策略执行官 → 等待三路汇总...</span>',
                            unsafe_allow_html=True)

                        # 利用默认参数将当前帧的槽位绑定到回调（避免闭包陷阱）
                        def _on_done_batch(key: str, r,
                                           sv=_sv, sc=_sc, sco=_sco, sst=_sst):
                            ok  = r.success
                            cls = "b-done" if ok else "b-err"
                            tag = "✅ 完成" if ok else "❌ 失败"
                            _labels = {
                                "visual":     f"AGENT 1 · 视觉拆解师 → {tag}",
                                "compliance": f"AGENT 3 · 合规排雷兵 → {tag}",
                                "commerce":   f"AGENT 2 · 转化精算师 → {tag}",
                                "strategy":   f"AGENT 4 · 策略执行官 → {tag}",
                            }
                            _slots = {
                                "visual": sv, "compliance": sc,
                                "commerce": sco, "strategy": sst,
                            }
                            if key in _slots:
                                _slots[key].markdown(
                                    f'<span class="badge {cls}">{_labels[key]}</span>',
                                    unsafe_allow_html=True)
                            if key == "commerce" and ok:
                                sst.markdown(
                                    '<span class="badge b-run">AGENT 4 · 策略执行官 → 汇总决策中...</span>',
                                    unsafe_allow_html=True)

                        try:
                            _wfl = _workflow(st.session_state.rag_text)
                            _wf_result = _wfl.run(_raw_b, on_agent_complete=_on_done_batch)
                            _batch_new.append({
                                "name":       _fname,
                                "image_data": _raw_b,
                                "result":     _wf_result,
                            })

                            # 保存历史记录
                            if _wf_result and (_wf_result.success or _wf_result.visual):
                                try:
                                    _history_store().save(
                                        session_id = st.session_state.session_id,
                                        image_name = _fname,
                                        wf_result  = _wf_result,
                                    )
                                except Exception as _he:
                                    logger.warning("History save failed: %s", _he)

                        except Exception as _e:
                            logger.error("Batch workflow error for %s: %s", _fname, _e, exc_info=True)
                            _batch_new.append({
                                "name":       _fname,
                                "image_data": _raw_b,
                                "result":     None,
                            })
                            _sv.markdown('<span class="badge b-err">AGENT 1 · 视觉拆解师 → ❌ 异常</span>', unsafe_allow_html=True)
                            _sc.markdown('<span class="badge b-err">AGENT 3 · 合规排雷兵 → ❌ 异常</span>', unsafe_allow_html=True)

                        _progress.progress(
                            (_img_idx + 1) / n,
                            text=f"{_img_idx + 1}/{n} 完成"
                        )

                    _total_ok = sum(1 for b in _batch_new if b["result"] and b["result"].success)
                    status_ctx.update(
                        label=f"✅ 批量分析完成！{_total_ok}/{n} 成功",
                        state="complete" if _total_ok > 0 else "error",
                    )

                # ── 写入 session state，跳转到结果页 ─────────────────────────
                st.session_state.batch_results = _batch_new
                if _batch_new:
                    _first = next(
                        (b for b in _batch_new if b["result"] and b["result"].success),
                        _batch_new[0],
                    )
                    st.session_state.workflow_result = _first["result"]
                    st.session_state.image_name      = _first["name"]
                    st.session_state.image_data      = _first["image_data"]
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# 结果展示区域
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state.workflow_result:
    wf = st.session_state.workflow_result

    # ── 结果页顶部操作栏（返回按钮 + 分析信息）──────────────────────────────
    st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
            padding:16px 0 8px;border-bottom:1px solid rgba(255,255,255,.06);
            margin-bottom:4px;">
  <div style="display:flex;align-items:center;gap:12px;">
    <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;
                color:#3D4F68;">// 分析结果</div>
    <div style="font-size:13px;color:#E2E8F0;font-weight:600;">
      {st.session_state.image_name or '未知文件'}
    </div>
    <div style="font-family:'DM Mono',monospace;font-size:10px;color:#3D4F68;">
      {wf.total_tokens:,} tokens · {wf.total_elapsed_ms}ms
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    back_col, export_col, _ = st.columns([1, 1, 4])
    with back_col:
        if st.button("← 重新分析", key="back_to_upload"):
            for k in ("workflow_result", "image_data", "chat_history", "feedback_done"):
                st.session_state[k] = (
                    None if k not in ("chat_history", "feedback_done")
                    else ([] if k == "chat_history" else set())
                )
            st.session_state.batch_results    = []
            st.session_state.synthesis_result  = None
            st.session_state.selected_frame_idx = 0
            st.rerun()

    with export_col:
        # ── 导出完整报告 ─────────────────────────────────────────────────────
        from services.report_generator import build_markdown, build_pdf
        _md_report = build_markdown(
            wf           = wf,
            image_name   = st.session_state.image_name,
            synthesis    = st.session_state.get("synthesis_result"),
            transcript   = (
                st.session_state.get("transcript_result", {}) or {}
            ).get("transcript", ""),
            user_email   = (st.session_state.get("user_info") or {}).get("email", ""),
        )
        _pdf_bytes = build_pdf(_md_report)
        _report_stem = Path(st.session_state.image_name or "report").stem

        if _pdf_bytes:
            st.download_button(
                "⬇️ 导出 PDF",
                data=_pdf_bytes,
                file_name=f"VIRA_{_report_stem}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key="export_pdf",
            )
        else:
            st.download_button(
                "⬇️ 导出 Markdown",
                data=_md_report.encode("utf-8"),
                file_name=f"VIRA_{_report_stem}.md",
                mime="text/markdown",
                use_container_width=True,
                key="export_md",
            )

    # ── 批量结果导航（仅当批量分析了多张时显示）────────────────────────────────
    _batch_stored = st.session_state.get("batch_results", [])
    if len(_batch_stored) > 1:
        st.markdown(f"""
<div style="margin:16px 0 10px;">
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.18em;color:#818CF8;">
    // 批量结果导航 · {len(_batch_stored)} 张图片
  </span>
  <span style="font-size:11px;color:#7C8FA6;margin-left:10px;">点击切换查看各图详细报告</span>
</div>""", unsafe_allow_html=True)

        _nav_cols = st.columns(min(len(_batch_stored), 4))
        for _bi, _bitem in enumerate(_batch_stored):
            _br       = _bitem.get("result")
            _is_cur   = (_bitem["name"] == st.session_state.image_name)
            _conf     = (
                _br.strategy.data.get("confidence_score", "?")
                if _br and _br.strategy and _br.strategy.success else "?"
            )
            _risk     = (
                _br.compliance.data.get("risk_level", "?")
                if _br and _br.compliance and _br.compliance.success else "?"
            )
            _risk_clr = {"LOW": "#00C97A", "MEDIUM": "#F0A500", "HIGH": "#FF3D55"}.get(
                str(_risk), "#7C8FA6"
            )
            _border   = (
                "border:2px solid #6366F1;box-shadow:0 0 16px rgba(99,102,241,.35);"
                if _is_cur else
                "border:1px solid rgba(139,92,246,.18);"
            )
            _short = _bitem["name"][:16] + ("…" if len(_bitem["name"]) > 16 else "")

            with _nav_cols[_bi % 4]:
                try:
                    _nav_img = Image.open(io.BytesIO(_bitem["image_data"]))
                    st.markdown(
                        f'<div style="{_border}border-radius:8px;overflow:hidden;margin-bottom:4px;">',
                        unsafe_allow_html=True,
                    )
                    st.image(_nav_img, use_container_width=True)
                    st.markdown("</div>", unsafe_allow_html=True)
                except Exception:
                    pass

                st.markdown(
                    f'<div style="font-size:10px;font-family:\'DM Mono\',monospace;'
                    f'color:{"#818CF8" if _is_cur else "#7C8FA6"};'
                    f'margin-bottom:2px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;">'
                    f'{"▶ " if _is_cur else ""}{_short}</div>'
                    f'<div style="font-size:10px;color:#3D4F68;margin-bottom:6px;">'
                    f'置信 <span style="color:#818CF8;">{_conf}</span> · '
                    f'<span style="color:{_risk_clr};">{_risk}</span></div>',
                    unsafe_allow_html=True,
                )
                if not _is_cur:
                    if st.button(f"查看", key=f"batch_nav_{_bi}", use_container_width=True):
                        if _bitem["result"]:
                            st.session_state.workflow_result = _bitem["result"]
                            st.session_state.image_name      = _bitem["name"]
                            st.session_state.image_data      = _bitem["image_data"]
                            st.session_state.chat_history    = []
                            st.session_state.feedback_done   = set()
                            st.rerun()
                        else:
                            st.toast(f"⚠️ {_bitem['name']} 分析失败，无法查看", icon="❌")

        # ── 爆款公式提炼按钮 ──────────────────────────────────────────────────
        _syn_done = st.session_state.get("synthesis_result") is not None
        _syn_label = "✅ 已提炼爆款公式" if _syn_done else f"🔬 提炼爆款公式（{len(_batch_stored)} 个样本）"
        _s1, _s2, _s3 = st.columns([1, 2, 1])
        with _s2:
            if st.button(_syn_label, use_container_width=True, key="run_synthesis",
                         disabled=(not st.session_state.api_key)):
                with st.spinner("Agent 5 正在归纳爆款公式..."):
                    try:
                        from core.synthesis_agent import SynthesisAgent
                        _syn_client = _workflow(st.session_state.rag_text).client
                        _syn_agent  = SynthesisAgent(_syn_client)
                        st.session_state.synthesis_result = _syn_agent.run(_batch_stored)
                        st.rerun()
                    except Exception as _se:
                        st.error(f"提炼失败：{_se}")

        st.markdown('<hr class="vira-hr" style="margin:16px 0 8px;">', unsafe_allow_html=True)

    # Agent 4 决策卡片（始终可见，置于 Tab 之上）
    _render_strategy_card(wf)

    st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
    _has_synthesis = st.session_state.get("synthesis_result") is not None
    _tab_labels    = ["📊 分析报告", "📝 重构脚本", "🔧 开发者视图", "💬 智能问答"]
    if _has_synthesis:
        _tab_labels.insert(1, "🔬 爆款公式")
    _tabs = st.tabs(_tab_labels)

    if _has_synthesis:
        tab_report, tab_synthesis, tab_scripts, tab_dev, tab_chat = _tabs
    else:
        tab_synthesis = None
        tab_report, tab_scripts, tab_dev, tab_chat = _tabs

    # ── Tab 1：分析报告 ────────────────────────────────────────────────────────
    with tab_report:
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        col_v, col_c = st.columns([1, 1], gap="large")

        with col_v:
            st.markdown("""
<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;">
  <div style="width:3px;height:20px;border-radius:2px;background:#38BDF8;flex-shrink:0;"></div>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:#38BDF8;">AGENT 1</span>
  <span style="font-size:15px;font-weight:700;color:#E2E8F0;">视觉拆解师</span>
</div>""", unsafe_allow_html=True)
            if wf.visual and wf.visual.success:
                d = wf.visual.data
                st.metric("Hook 类型", d.get("hook_type", "—"))
                s1, s2 = st.columns(2)
                s1.metric("Hook 评分", f"{d.get('hook_score','—')}/100")
                s2.metric("视觉质量", f"{d.get('visual_score','—')}/100")
                st.markdown(
                    f'<div class="glass" style="margin-top:10px;">'
                    f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                    f'letter-spacing:.14em;color:#7C8FA6;margin-bottom:8px;">// 情绪基调</div>'
                    f'<div style="color:#E2E8F0;font-size:13px;">{d.get("emotional_tone","—")}</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
                st.markdown(
                    f'<div class="glass">'
                    f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                    f'letter-spacing:.14em;color:#7C8FA6;margin-bottom:8px;">// 前3秒分析</div>'
                    f'<div style="color:#7C8FA6;font-size:13px;line-height:1.75;">'
                    f'{d.get("first_3s_analysis","—")}</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
                if d.get("key_visual_elements"):
                    els = "".join(
                        f'<div style="display:flex;align-items:center;gap:7px;'
                        f'margin-bottom:5px;font-size:12px;color:#7C8FA6;">'
                        f'<span style="color:#38BDF8;">·</span> {el}</div>'
                        for el in d["key_visual_elements"]
                    )
                    st.markdown(
                        f'<div class="glass"><div style="font-size:9px;font-family:\'DM Mono\','
                        f'monospace;letter-spacing:.14em;color:#7C8FA6;margin-bottom:8px;">'
                        f'// 关键视觉元素</div>{els}</div>',
                        unsafe_allow_html=True
                    )
                fk = f"v_{st.session_state.session_id}"
                if fk not in st.session_state.feedback_done:
                    fa, fb, _ = st.columns([1,1,6])
                    if fa.button("👍", key="fv_up"):
                        _feedback_store().save(1,"视觉拆解师",st.session_state.image_name,
                            json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                        st.session_state.feedback_done.add(fk); st.toast("感谢！",icon="👍")
                    if fb.button("👎", key="fv_dn"):
                        _feedback_store().save(0,"视觉拆解师",st.session_state.image_name,
                            json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                        st.session_state.feedback_done.add(fk); st.toast("Bad Case 已记录",icon="📌")
            else:
                st.error(f"Agent 1 失败: {wf.visual.error if wf.visual else '未运行'}")

        with col_c:
            st.markdown("""
<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;">
  <div style="width:3px;height:20px;border-radius:2px;background:#FF3D55;flex-shrink:0;"></div>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:#FF3D55;">AGENT 3</span>
  <span style="font-size:15px;font-weight:700;color:#E2E8F0;">合规排雷兵</span>
</div>""", unsafe_allow_html=True)
            if wf.compliance and wf.compliance.success:
                d    = wf.compliance.data
                risk = d.get("risk_level","—")
                risk_clr = {"LOW":"#00C97A","MEDIUM":"#F0A500","HIGH":"#FF3D55"}.get(risk,"#7C8FA6")
                risk_bg  = {"LOW":"rgba(0,201,122,.1)","MEDIUM":"rgba(240,165,0,.1)",
                            "HIGH":"rgba(255,61,85,.1)"}.get(risk,"rgba(255,255,255,.04)")
                risk_bd  = {"LOW":"rgba(0,201,122,.3)","MEDIUM":"rgba(240,165,0,.3)",
                            "HIGH":"rgba(255,61,85,.3)"}.get(risk,"rgba(255,255,255,.08)")
                st.markdown(
                    f'<div style="display:inline-flex;align-items:center;gap:8px;'
                    f'background:{risk_bg};border:1px solid {risk_bd};border-radius:8px;'
                    f'padding:8px 16px;margin-bottom:12px;">'
                    f'<span style="font-family:\'DM Mono\',monospace;font-size:10px;'
                    f'letter-spacing:.1em;color:#7C8FA6;">风险等级</span>'
                    f'<span style="font-weight:800;font-size:14px;color:{risk_clr};">{risk}</span>'
                    f'</div>',
                    unsafe_allow_html=True
                )
                st.metric("合规评分", f"{d.get('compliance_score','—')}/100")

                if d.get("_risk_dict_categories"):
                    st.markdown(
                        f'<div style="font-size:10px;color:#3D4F68;font-family:\'DM Mono\','
                        f'monospace;margin:6px 0 10px;">📖 已扫描 {d.get("_total_rules",0)} 条规则 · '
                        f'{len(d["_risk_dict_categories"])} 个类别</div>',
                        unsafe_allow_html=True
                    )

                violations = d.get("violations", [])
                if violations:
                    st.markdown(
                        '<div style="font-size:11px;font-family:\'DM Mono\',monospace;'
                        'letter-spacing:.12em;color:#FF3D55;margin-bottom:8px;">// 命中风险项</div>',
                        unsafe_allow_html=True
                    )
                    for v in violations:
                        sev   = v.get("severity","LOW")
                        vclr  = {"HIGH":"#FF3D55","MEDIUM":"#F0A500","LOW":"#F0A500"}.get(sev,"#7C8FA6")
                        vbg   = {"HIGH":"rgba(255,61,85,.07)","MEDIUM":"rgba(240,165,0,.07)",
                                 "LOW":"rgba(240,165,0,.07)"}.get(sev,"rgba(255,255,255,.03)")
                        vbd   = {"HIGH":"rgba(255,61,85,.3)","MEDIUM":"rgba(240,165,0,.3)",
                                 "LOW":"rgba(240,165,0,.25)"}.get(sev,"rgba(255,255,255,.08)")
                        st.markdown(
                            f'<div style="background:{vbg};border:1px solid {vbd};'
                            f'border-left:3px solid {vclr};border-radius:10px;'
                            f'padding:12px 16px;margin-bottom:8px;">'
                            f'<div style="color:{vclr};font-family:\'DM Mono\',monospace;'
                            f'font-size:10px;margin-bottom:4px;">[{v.get("type","—")}] · {sev}</div>'
                            f'<div style="color:#E2E8F0;font-size:13px;margin-bottom:4px;">'
                            f'{v.get("text","")}</div>'
                            f'<div style="color:#7C8FA6;font-size:11px;">建议：{v.get("suggestion","—")}</div>'
                            f'</div>',
                            unsafe_allow_html=True
                        )
                else:
                    st.markdown(
                        '<div style="background:rgba(0,201,122,.08);border:1px solid rgba(0,201,122,.25);'
                        'border-radius:10px;padding:12px 16px;color:#00C97A;font-size:13px;">'
                        '✓ 未命中任何风控规则，可安全发布</div>',
                        unsafe_allow_html=True
                    )

                pn = d.get("platform_notes",{})
                if any(pn.values()):
                    with st.expander("📋 平台专项说明"):
                        if pn.get("tiktok"):
                            st.markdown(f"**TikTok：** {pn['tiktok']}")
                        if pn.get("douyin"):
                            st.markdown(f"**抖音：** {pn['douyin']}")

                fk = f"c_{st.session_state.session_id}"
                if fk not in st.session_state.feedback_done:
                    fa, fb, _ = st.columns([1,1,6])
                    if fa.button("👍", key="fc_up"):
                        _feedback_store().save(1,"合规排雷兵",st.session_state.image_name,
                            json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                        st.session_state.feedback_done.add(fk); st.toast("感谢！",icon="👍")
                    if fb.button("👎", key="fc_dn"):
                        _feedback_store().save(0,"合规排雷兵",st.session_state.image_name,
                            json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                        st.session_state.feedback_done.add(fk); st.toast("Bad Case 已记录",icon="📌")
            else:
                st.error(f"Agent 3 失败: {wf.compliance.error if wf.compliance else '未运行'}")

    # ── Tab 爆款公式（仅批量分析后可见）──────────────────────────────────────
    if tab_synthesis is not None:
        with tab_synthesis:
            st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
            _sr = st.session_state.synthesis_result
            if _sr and _sr.success:
                _sd = _sr.data
                # 顶部公式大卡
                st.markdown(f"""
<div style="background:linear-gradient(135deg,rgba(99,102,241,.12),rgba(168,85,247,.10));
            border:1px solid rgba(168,85,247,.28);border-radius:16px;
            padding:24px 28px;margin-bottom:20px;position:relative;overflow:hidden;">
  <div style="position:absolute;top:0;left:0;right:0;height:2px;
              background:linear-gradient(90deg,#6366F1,#A855F7,#C084FC);"></div>
  <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.18em;
              color:#818CF8;margin-bottom:10px;">
    // AGENT 5 · 爆款公式提炼师 · {_sd.get('sample_count','?')} 个样本
  </div>
  <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:20px;
              font-weight:800;color:#E2E8F0;margin-bottom:10px;">
    {_sd.get('viral_formula','—')}
  </div>
  <div style="font-size:13px;color:#7C8FA6;line-height:1.8;">
    {_sd.get('executive_summary','—')}
  </div>
</div>""", unsafe_allow_html=True)

                # Hook 规律
                _hp = _sd.get("hook_patterns", [])
                if _hp:
                    st.markdown(
                        '<div style="font-size:15px;font-weight:700;color:#E2E8F0;'
                        'margin-bottom:10px;">🎣 Hook 规律</div>',
                        unsafe_allow_html=True,
                    )
                    _hcols = st.columns(min(len(_hp), 3))
                    for _hi, _h in enumerate(_hp):
                        with _hcols[_hi % 3]:
                            st.markdown(f"""
<div class="glass" style="text-align:center;padding:16px;">
  <div style="font-size:22px;font-weight:800;color:#818CF8;
              font-family:'Plus Jakarta Sans',sans-serif;">{_h.get('frequency','?')}次</div>
  <div style="font-size:12px;font-weight:700;color:#E2E8F0;margin:4px 0;">
    {_h.get('pattern','—')}</div>
  <div style="font-size:11px;color:#7C8FA6;">{_h.get('example','—')}</div>
</div>""", unsafe_allow_html=True)

                # 三列：视觉规律 / 转化洞察 / 合规注意
                st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
                _c1, _c2, _c3 = st.columns(3)
                _col_data = [
                    ("👁 视觉规律",    "#38BDF8", _sd.get("visual_rules", [])),
                    ("📈 转化洞察",    "#A855F7", _sd.get("conversion_insights", [])),
                    ("🛡 合规注意",    "#F43F5E", _sd.get("compliance_watch", [])),
                ]
                for _col, (_title, _clr, _items) in zip([_c1, _c2, _c3], _col_data):
                    with _col:
                        st.markdown(
                            f'<div style="font-size:13px;font-weight:700;color:#E2E8F0;'
                            f'margin-bottom:8px;">{_title}</div>',
                            unsafe_allow_html=True,
                        )
                        for _item in _items:
                            st.markdown(
                                f'<div class="glass" style="padding:8px 12px;margin-bottom:6px;">'
                                f'<span style="color:{_clr};font-size:10px;">·</span> '
                                f'<span style="font-size:12px;color:#7C8FA6;">{_item}</span>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                # 优先建议
                _recs = _sd.get("top_recommendations", [])
                if _recs:
                    st.markdown(
                        '<div style="font-size:15px;font-weight:700;color:#E2E8F0;'
                        'margin:16px 0 10px;">⚡ 优先行动建议</div>',
                        unsafe_allow_html=True,
                    )
                    for _r in sorted(_recs, key=lambda x: x.get("priority", 9)):
                        _pri = _r.get("priority", "?")
                        st.markdown(f"""
<div class="glass" style="padding:14px 18px;margin-bottom:8px;
     border-left:3px solid #6366F1;">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
    <span style="font-family:'DM Mono',monospace;font-size:9px;color:#818CF8;
                 background:rgba(99,102,241,.12);padding:2px 8px;border-radius:4px;">
      P{_pri}</span>
    <span style="font-size:13px;font-weight:700;color:#E2E8F0;">
      {_r.get('action','—')}</span>
  </div>
  <div style="font-size:11px;color:#7C8FA6;">{_r.get('reason','—')}</div>
</div>""", unsafe_allow_html=True)

                # 方法论文档
                _md_doc = _sd.get("methodology_doc", "")
                if _md_doc:
                    with st.expander("📄 完整方法论文档（可复制给团队）"):
                        st.markdown(_md_doc)
            else:
                st.info("请先在上方点击「提炼爆款公式」按钮，至少需要分析 2 张以上图片。")
                if _sr and not _sr.success:
                    st.error(f"提炼失败：{_sr.error}")

    # ── Tab 2：重构脚本 ────────────────────────────────────────────────────────
    with tab_scripts:
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        st.markdown("""
<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;">
  <div style="width:3px;height:20px;border-radius:2px;background:#A855F7;flex-shrink:0;"></div>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:#A855F7;">AGENT 2</span>
  <span style="font-size:15px;font-weight:700;color:#E2E8F0;">转化精算师</span>
</div>""", unsafe_allow_html=True)
        if wf.commerce and wf.commerce.success:
            d = wf.commerce.data
            s1, s2 = st.columns(2)
            s1.metric("病毒传播潜力", f"{d.get('virality_score','—')}/100")
            s2.metric("商业转化潜力", f"{d.get('conversion_potential','—')}/100")
            if d.get("rag_references"):
                with st.expander("📚 已调用品牌知识库片段"):
                    for ref in d["rag_references"]:
                        st.markdown(
                            f'<div style="background:rgba(168,85,247,.06);border-left:2px solid #A855F7;'
                            f'border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:12px;'
                            f'color:#7C8FA6;">{ref}</div>',
                            unsafe_allow_html=True
                        )
            st.markdown(
                f'<div class="glass" style="margin-bottom:16px;">'
                f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                f'letter-spacing:.14em;color:#7C8FA6;margin-bottom:6px;">// 优化逻辑</div>'
                f'<div style="color:#7C8FA6;font-size:13px;line-height:1.75;">'
                f'{d.get("optimization_summary","—")}</div></div>',
                unsafe_allow_html=True
            )
            st.markdown(
                '<div style="font-size:9px;font-family:\'DM Mono\',monospace;letter-spacing:.18em;'
                'color:#A855F7;margin-bottom:12px;">// 三套商业重构脚本</div>',
                unsafe_allow_html=True
            )
            for i, s in enumerate(d.get("scripts",[])):
                accent = ["#818CF8","#C084FC","#60A5FA"][i % 3]
                with st.expander(f"方案 {i+1}：{s.get('title',f'脚本{i+1}')}", expanded=(i==0)):
                    st.markdown(
                        f'<div style="background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.07);'
                        f'border-left:3px solid {accent};border-radius:10px;padding:18px 20px;">'
                        f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                        f'letter-spacing:.14em;color:{accent};margin-bottom:6px;">🎬 前3秒 HOOK</div>'
                        f'<div style="color:#E2E8F0;font-size:13px;line-height:1.75;margin-bottom:14px;">'
                        f'{s.get("hook","—")}</div>'
                        f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                        f'letter-spacing:.14em;color:{accent};margin-bottom:6px;">📖 正文内容</div>'
                        f'<div style="color:#7C8FA6;font-size:13px;line-height:1.75;margin-bottom:14px;">'
                        f'{s.get("body","—")}</div>'
                        f'<div style="font-size:9px;font-family:\'DM Mono\',monospace;'
                        f'letter-spacing:.14em;color:{accent};margin-bottom:6px;">🎯 CTA</div>'
                        f'<div style="color:#E2E8F0;font-size:13px;line-height:1.75;">'
                        f'{s.get("cta","—")}</div>'
                        f'</div>',
                        unsafe_allow_html=True
                    )
            fk = f"co_{st.session_state.session_id}"
            if fk not in st.session_state.feedback_done:
                fa, fb, _ = st.columns([1,1,6])
                if fa.button("👍 脚本有用", key="fco_up"):
                    _feedback_store().save(1,"转化精算师",st.session_state.image_name,
                        json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                    st.session_state.feedback_done.add(fk); st.toast("感谢！",icon="👍")
                if fb.button("👎 脚本无用", key="fco_dn"):
                    _feedback_store().save(0,"转化精算师",st.session_state.image_name,
                        json.dumps(d,ensure_ascii=False),st.session_state.session_id)
                    st.session_state.feedback_done.add(fk); st.toast("Bad Case 已记录",icon="📌")
        else:
            st.error(f"Agent 2 失败: {wf.commerce.error if wf.commerce else '未运行'}")

    # ── Tab 3：开发者视图 ──────────────────────────────────────────────────────
    with tab_dev:
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        st.markdown("""
<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
  <div style="width:3px;height:20px;border-radius:2px;background:#6366F1;flex-shrink:0;"></div>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:#6366F1;">DEVELOPER</span>
  <span style="font-size:15px;font-weight:700;color:#E2E8F0;">Trace &amp; Raw Output</span>
</div>
<div style="font-size:11px;color:#3D4F68;margin-bottom:16px;font-family:'DM Mono',monospace;">
原始 API 响应 · Token 消耗 · 后端执行延迟 · 并发架构说明
</div>""", unsafe_allow_html=True)

        dv1, dv2, dv3 = st.columns(3)
        dv1.metric("总 Token 消耗", f"{wf.total_tokens:,}")
        dv2.metric("端到端延迟",    f"{wf.total_elapsed_ms}ms")
        dv3.metric("并发架构",      "asyncio.gather (A1‖A3) → A2 → A4")

        st.markdown("""
<div class="glass" style="margin-top:12px;border-left:2px solid #6366F1;">
  <div style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;
              color:#6366F1;margin-bottom:10px;">// asyncio.gather 并发策略</div>
  <div style="font-size:12px;color:#7C8FA6;line-height:1.9;">
    <span style="color:#E2E8F0;">Phase 1：</span>
    <code style="background:rgba(99,102,241,.10);color:#6366F1;padding:1px 6px;
                 border-radius:4px;font-family:'DM Mono',monospace;">
      asyncio.gather(asyncio.to_thread(A1), asyncio.to_thread(A3))
    </code><br>
    A1/A3 各自在独立线程发出 OpenAI HTTP 请求，event loop 挂起等待，无阻塞。<br>
    <span style="color:#E2E8F0;">Phase 2：</span>A2 串行等待 A1 视觉结果（RAG 上下文依赖）<br>
    <span style="color:#E2E8F0;">Phase 3：</span>A4 串行汇总三路输出，输出最终战略裁决
  </div>
</div>""", unsafe_allow_html=True)

        st.markdown("---")

        agent_map = [
            ("Agent 1 · 视觉拆解师", wf.visual,     "#60A5FA"),
            ("Agent 2 · 转化精算师", wf.commerce,   "#A855F7"),
            ("Agent 3 · 合规排雷兵", wf.compliance, "#F43F5E"),
            ("Agent 4 · 策略执行官", wf.strategy,   "#6366F1"),
        ]
        for label, r, color in agent_map:
            if r:
                state = "✅ SUCCESS" if r.success else "❌ FAILED"
                with st.expander(f"📡 {label} — {state}"):
                    if r.usage:
                        u = r.usage
                        c1,c2,c3,c4 = st.columns(4)
                        c1.metric("Prompt Tokens",     u.get("prompt_tokens","—"))
                        c2.metric("Completion Tokens", u.get("completion_tokens","—"))
                        c3.metric("Total Tokens",      u.get("total_tokens","—"))
                        c4.metric("API 延迟",           f"{u.get('elapsed_ms','—')}ms")
                    st.markdown(
                        f'<div style="font-family:\'DM Mono\',monospace;font-size:9px;'
                        f'letter-spacing:.14em;color:{color};margin:10px 0 6px;">// RAW RESPONSE</div>',
                        unsafe_allow_html=True
                    )
                    st.markdown(
                        f'<div class="dev-raw">{r.raw_response or "(空)"}</div>',
                        unsafe_allow_html=True)

        # 风控字典展示
        st.markdown("---")
        st.markdown(
            '<div style="font-size:9px;font-family:\'DM Mono\',monospace;letter-spacing:.15em;'
            'color:#FF3D55;margin-bottom:4px;">// RISK DICTIONARY</div>'
            '<div style="font-size:15px;font-weight:700;color:#E2E8F0;margin-bottom:4px;">'
            'Agent 3 内置风控字典</div>'
            '<div style="font-size:11px;color:#3D4F68;font-family:\'DM Mono\',monospace;'
            'margin-bottom:12px;">TikTok Brand Risk Dictionary v1.0 · 每次扫描完整注入 Prompt</div>',
            unsafe_allow_html=True
        )
        try:
            from core.agents import TIKTOK_RISK_DICT
            for cat, terms in TIKTOK_RISK_DICT.items():
                with st.expander(f"▸ {cat}（{len(terms)} 条）"):
                    st.markdown("、".join(f"`{t}`" for t in terms))
        except Exception:
            pass

        # Bad Case 导出
        st.markdown("---")
        st.markdown(
            '<div style="font-size:9px;font-family:\'DM Mono\',monospace;letter-spacing:.15em;'
            'color:#7C8FA6;margin-bottom:4px;">// EXPORT</div>'
            '<div style="font-size:15px;font-weight:700;color:#E2E8F0;margin-bottom:12px;">'
            'Bad Case 导出（SFT 数据回流）</div>',
            unsafe_allow_html=True
        )
        if st.button("生成 bad_cases.json"):
            try:
                bad = _feedback_store().export_bad_cases()
                st.download_button(
                    "⬇️ 下载 bad_cases.json",
                    data=json.dumps(bad, ensure_ascii=False, indent=2),
                    file_name="bad_cases.json", mime="application/json",
                )
            except Exception as e:
                st.error(f"导出失败：{e}")

        # 历史统计
        st.markdown("---")
        st.markdown(
            '<div style="font-size:9px;font-family:\'DM Mono\',monospace;letter-spacing:.15em;'
            'color:#7C8FA6;margin-bottom:4px;">// HISTORY STATS</div>'
            '<div style="font-size:15px;font-weight:700;color:#E2E8F0;margin-bottom:12px;">'
            '分析历史统计</div>',
            unsafe_allow_html=True
        )
        try:
            hs = _history_store().get_stats()
            h1,h2,h3 = st.columns(3)
            h1.metric("总分析次数", hs["total"])
            h2.metric("平均置信度", f"{hs['avg_confidence']}/100")
            h3.metric("风险分布",
                      " · ".join(f"{k}: {v}" for k,v in hs["risk_distribution"].items()) or "—")
        except Exception:
            pass

    # ── Tab 4：智能问答（Intent Router）──────────────────────────────────────
    with tab_chat:
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        st.markdown("""
<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
  <div style="width:3px;height:20px;border-radius:2px;background:#00C97A;flex-shrink:0;"></div>
  <span style="font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.15em;color:#00C97A;">INTENT ROUTER</span>
  <span style="font-size:15px;font-weight:700;color:#E2E8F0;">智能问答</span>
</div>
<div style="font-size:11px;color:#3D4F68;font-family:'DM Mono',monospace;margin-bottom:14px;">
自动路由到对应 Agent 上下文 · confidence &lt; 70% 时显示「仅供参考」提示
</div>""", unsafe_allow_html=True)

        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        if user_q := st.chat_input("输入问题，例如：帮我把脚本 Hook 改得更有冲击力"):
            st.session_state.chat_history.append({"role":"user","content":user_q})
            with st.chat_message("user"):
                st.markdown(user_q)

            if not st.session_state.api_key:
                with st.chat_message("assistant"):
                    st.warning("请先在侧边栏填入 OpenAI API Key")
            else:
                with st.chat_message("assistant"):
                    with st.spinner("意图路由中..."):
                        intent, context, confidence = _route_intent(user_q, wf)

                        from prompts import INTENT_ROUTER
                        import openai

                        sys_prompt = INTENT_ROUTER.format(context=context)
                        history    = st.session_state.chat_history[-10:]
                        messages   = [{"role":"system","content":sys_prompt}]
                        for h in history[:-1]:
                            messages.append({"role":h["role"],"content":h["content"]})
                        messages.append({"role":"user","content":user_q})

                        try:
                            oa   = openai.OpenAI(api_key=st.session_state.api_key)
                            resp = oa.chat.completions.create(
                                model=st.session_state.model,
                                messages=messages, max_tokens=900, temperature=0.5,
                            )
                            answer = resp.choices[0].message.content
                            if confidence < 0.7:
                                answer = f"⚠️ **结果仅供参考，请结合人工复核**\n\n{answer}"
                            full = answer + (
                                f"\n\n---\n<small>路由：{intent} · "
                                f"置信度 {confidence:.0%}</small>"
                            )
                            st.markdown(full, unsafe_allow_html=True)
                            st.session_state.chat_history.append(
                                {"role":"assistant","content":full})
                        except Exception as e:
                            err = f"❌ API 请求失败：{e}"
                            st.error(err)
                            st.session_state.chat_history.append(
                                {"role":"assistant","content":err})
                            logger.error("Chat error: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# AI 客服助理（右下角悬浮 · 仿 Intercom 风格）
# ══════════════════════════════════════════════════════════════════════════════

_CS_FAQ = """
你是 VIRA 爆款侦察兵的专属 AI 客服助理，名字叫 VIRA Assistant。
你熟悉 VIRA 的所有功能，友善、简洁地回答用户问题。

【VIRA 产品介绍】
- VIRA 是一款面向内容创作者的竞品分析工具
- 上传竞品截图，30秒内由 4 个 AI Agent 协同分析：视觉拆解师、转化精算师、合规排雷兵、策略执行官
- 支持批量上传多张图片同时分析
- 内置 TikTok/抖音合规风控字典
- 支持 RAG 知识库注入（品牌法则/产品卖点）
- 内置 A/B Test 方案设计
- 所有分析结果支持导出

【常见问题】
Q: 如何开始？A: 在侧边栏填入 OpenAI API Key，上传竞品截图，点击「启动分析」即可。
Q: 支持哪些格式？A: JPG、PNG、WebP，每张 ≤ 20MB。
Q: 一次能分析几张？A: 支持批量上传，一次分析全部图片。
Q: 什么是 RAG 知识库？A: 在侧边栏粘贴你的品牌法则、产品卖点，Agent 2 会参考这些内容生成更贴合品牌的脚本。
Q: 分析结果如何保存？A: 自动保存到侧边栏历史记录，也可在「开发者视图」导出 bad_cases.json。

请用中文回复，回答简洁专业，长度控制在 150 字以内。
"""

# ── 悬浮按钮（纯 HTML，点击触发 Streamlit 的 query param 来切换面板）────────
st.markdown("""
<style>
/* 悬浮按钮 */
#vira-cs-fab {
    position: fixed;
    bottom: 28px; right: 28px; z-index: 99999;
    width: 52px; height: 52px; border-radius: 50%;
    background: linear-gradient(135deg, #6366F1, #A855F7);
    box-shadow: 0 4px 20px rgba(99,102,241,.55), 0 2px 0 rgba(0,0,0,.2);
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; border: none; transition: transform .18s, box-shadow .18s;
    color: #fff; font-size: 22px;
}
#vira-cs-fab:hover {
    transform: translateY(-2px) scale(1.06);
    box-shadow: 0 8px 28px rgba(168,85,247,.65);
}
/* 未读气泡 */
#vira-cs-badge {
    position: absolute; top: -2px; right: -2px;
    width: 14px; height: 14px; border-radius: 50%;
    background: #F43F5E; border: 2px solid #06021A;
    font-size: 8px; color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-family: 'DM Mono', monospace;
}
</style>
<div id="vira-cs-fab" title="VIRA 客服助理">
  💬
  <div id="vira-cs-badge">1</div>
</div>
""", unsafe_allow_html=True)

# ── 客服面板（用 st.sidebar-like expander 实现，通过 session state 控制开关）──
# 在页面右下角用固定定位的 HTML 面板 + Streamlit 聊天组件组合实现
if st.session_state.get("cs_open", False):
    # 面板外层（fixed 定位，覆盖在 Streamlit 内容之上）
    st.markdown("""
<style>
/* 客服面板 */
#vira-cs-panel {
    position: fixed;
    bottom: 90px; right: 28px; z-index: 99998;
    width: 360px;
    background: rgba(6,2,26,.97);
    border: 1px solid rgba(139,92,246,.22);
    border-radius: 18px;
    box-shadow: 0 16px 60px rgba(41,79,187,.35), 0 0 0 1px rgba(168,85,247,.08);
    backdrop-filter: blur(28px);
    overflow: hidden;
}
/* 面板顶部 */
#vira-cs-header {
    background: linear-gradient(135deg, #6366F1 0%, #A855F7 100%);
    padding: 18px 20px 16px;
}
/* 聊天区域需要额外 max-height 防止撑开页面 */
#vira-cs-body {
    max-height: 360px;
    overflow-y: auto;
    padding: 12px 16px;
}
</style>
<div id="vira-cs-panel">
  <div id="vira-cs-header">
    <div style="display:flex;align-items:center;justify-content:space-between;">
      <div style="display:flex;align-items:center;gap:10px;">
        <div style="width:36px;height:36px;border-radius:50%;
                    background:rgba(255,255,255,.2);
                    display:flex;align-items:center;justify-content:center;
                    font-size:18px;">✦</div>
        <div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:14px;
                      font-weight:800;color:#fff;">VIRA Assistant</div>
          <div style="font-size:10px;color:rgba(255,255,255,.7);display:flex;
                      align-items:center;gap:4px;">
            <span style="width:6px;height:6px;border-radius:50%;
                         background:#00C97A;display:inline-block;"></span>
            在线 · AI 全天候服务
          </div>
        </div>
      </div>
    </div>
    <div style="margin-top:12px;font-family:'Plus Jakarta Sans',sans-serif;
                font-size:18px;font-weight:800;color:#fff;line-height:1.3;">
      Hi 👋<br>有什么可以帮你？
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    # 聊天消息历史
    cs_history = st.session_state.get("cs_history", [])
    if not cs_history:
        cs_history = [{
            "role": "assistant",
            "content": "你好！我是 VIRA 客服助理 ✦\n\n有任何关于使用 VIRA、功能介绍或遇到问题，直接告诉我～"
        }]
        st.session_state.cs_history = cs_history

    for _m in cs_history:
        with st.chat_message(_m["role"]):
            st.markdown(_m["content"])

    # 输入框
    if _cs_q := st.chat_input("输入问题…", key="cs_chat_input"):
        cs_history.append({"role": "user", "content": _cs_q})
        with st.chat_message("user"):
            st.markdown(_cs_q)

        if not st.session_state.api_key:
            _cs_ans = "请先在侧边栏填入 OpenAI API Key 才能使用 AI 客服哦～"
            cs_history.append({"role": "assistant", "content": _cs_ans})
            with st.chat_message("assistant"):
                st.markdown(_cs_ans)
        else:
            with st.chat_message("assistant"):
                with st.spinner("思考中..."):
                    try:
                        import openai as _oa
                        _cs_client = _oa.OpenAI(api_key=st.session_state.api_key)
                        _cs_msgs   = [{"role": "system", "content": _CS_FAQ}]
                        for _h in cs_history[-8:]:
                            _cs_msgs.append({"role": _h["role"], "content": _h["content"]})
                        _cs_resp = _cs_client.chat.completions.create(
                            model    = st.session_state.model,
                            messages = _cs_msgs,
                            max_tokens = 300,
                            temperature = 0.5,
                        )
                        _cs_ans = _cs_resp.choices[0].message.content
                    except Exception as _ce:
                        _cs_ans = f"抱歉，暂时无法回答：{_ce}"
                    st.markdown(_cs_ans)
                    cs_history.append({"role": "assistant", "content": _cs_ans})
                    st.session_state.cs_history = cs_history

    # 关闭按钮
    st.markdown('<div style="height:4px;"></div>', unsafe_allow_html=True)
    _cc1, _cc2 = st.columns([3, 1])
    with _cc2:
        if st.button("关闭 ✕", key="cs_close", use_container_width=True):
            st.session_state.cs_open = False
            st.rerun()
else:
    # 悬浮按钮点击 → 打开面板（用一个不可见按钮接管点击事件）
    st.markdown("""
<style>
/* 让 Streamlit 的打开按钮透明叠加在悬浮按钮上 */
div[data-testid="stButton"][id="cs_open_wrapper"] > button {
    position: fixed !important;
    bottom: 28px !important; right: 28px !important;
    width: 52px !important; height: 52px !important;
    border-radius: 50% !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    z-index: 999999 !important;
    opacity: 0 !important;
    cursor: pointer !important;
}
</style>
""", unsafe_allow_html=True)
    with st.container():
        if st.button("💬", key="cs_open_btn", help="打开 VIRA 客服助理"):
            st.session_state.cs_open = True
            st.rerun()
