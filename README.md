# VIRA · 爆款侦察兵

> 上传竞品截图，30 秒知道为什么它爆 —— 多模态 AI 四 Agent 协同分析系统

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io)
![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 产品简介

VIRA 是面向内容创作者和电商运营团队的竞品分析工具。上传竞品视频截图，四个 AI Agent 并发分析，30 秒内输出完整爆款基因报告。

**核心能力：**

| Agent | 职责 | 输出 |
|-------|------|------|
| Agent 1 · 视觉拆解师 | 多模态 Hook 特征提取 | Hook 评分、视觉质量、情绪基调 |
| Agent 2 · 转化精算师 | RAG 增强商业脚本生成 | 3 套重构脚本、转化潜力评分 |
| Agent 3 · 合规排雷兵 | TikTok/抖音风控红线扫描 | 违规词检测、风险分级 |
| Agent 4 · 策略执行官 | 汇总三路输出，最终裁决 | 成功置信度、A/B Test 方案 |
| Agent 5 · 爆款公式提炼师 | 多样本规律归纳 | 通用爆款公式、方法论文档 |

---

## 功能特性

- **批量分析** — 一次上传多张截图，并发分析全部图片
- **视频口播提取** — 上传视频，Whisper API 自动转录口播文案
- **爆款公式提炼** — 多样本横向对比，提炼可复用内容公式
- **报告导出** — 一键导出完整 Markdown / PDF 分析报告
- **模板库** — 保存品牌知识库配置，下次一键复用
- **RAG 知识库** — 注入品牌法则 / 产品卖点，生成专属脚本
- **邮箱账户系统** — 注册登录，历史分析记录持久化
- **AI 客服助理** — 右下角悬浮助理，随时解答产品问题

---

## 技术架构

```
app.py                          ← Streamlit 主入口 + 鉴权守卫
├── core/
│   ├── agents.py               ← 4 个专家 Agent 类
│   ├── workflow.py             ← asyncio.gather 并发流水线
│   └── synthesis_agent.py     ← Agent 5 爆款公式提炼
├── services/
│   ├── openai_client.py        ← OpenAI 封装（指数退避重试）
│   ├── rag.py                  ← TF-IDF RAG + 历史记录
│   ├── auth.py                 ← 邮箱鉴权（SQLite + PBKDF2）
│   ├── transcript.py           ← Whisper 视频转录
│   ├── report_generator.py     ← Markdown / PDF 报告生成
│   └── template_store.py      ← 分析模板存储
└── prompts/
    └── __init__.py             ← 所有 System Prompt 集中管理
```

**并发架构：**
```
Phase 1 [asyncio.gather]:  Agent1 (视觉) ‖ Agent3 (合规)   ← 真正并发
Phase 2 [串行]:            Agent2 (转化) ← 依赖 Agent1 结果
Phase 3 [串行]:            Agent4 (策略) ← 汇总三路输出
总耗时 ≈ max(T_A1, T_A3) + T_A2 + T_A4
```

---

## 本地运行

### 1. 克隆项目

```bash
git clone https://github.com/Iliana678/Vira-demo.git
cd Vira-demo
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 API Key

复制示例文件并填入真实 Key：

```bash
cp .env.example .env
# 编辑 .env，填入你的 OpenAI API Key
```

### 4. 启动

```bash
streamlit run app.py
```

浏览器访问 `http://localhost:8501`，注册账号后即可使用。

---

## 云端部署（Streamlit Community Cloud）

1. Fork 或 Clone 此仓库到你的 GitHub
2. 登录 [share.streamlit.io](https://share.streamlit.io)
3. New app → 选择此仓库 → `app.py`
4. Settings → Secrets → 填入：

```toml
OPENAI_API_KEY = "sk-你的key"
```

5. Deploy → 2-3 分钟后上线

---

## 环境变量

| 变量名 | 必填 | 说明 |
|--------|:----:|------|
| `OPENAI_API_KEY` | ✅ | OpenAI API Key，用于 GPT-4o Vision + Whisper |

---

## 可选增强依赖

```bash
# PDF 报告导出（不安装自动降级为 Markdown）
pip install fpdf2

# 视频音频提取（不安装自动直传 Whisper）
pip install ffmpeg-python
brew install ffmpeg  # macOS
```

---

## 项目截图

> 上传竞品截图 → 四 Agent 并发分析 → 30 秒出完整报告

---

## License

MIT © 2026 VIRA Team
