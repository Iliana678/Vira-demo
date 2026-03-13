# VIRA Demo — Cloudflare Pages + Workers 部署指南

## 项目结构

```
VIRA_Demo/
├── frontend/
│   └── index.html        # 前端单页应用
├── worker/
│   └── index.js          # Cloudflare Worker（Anthropic API 代理）
├── wrangler.toml          # Cloudflare Wrangler 配置
├── .dev.vars              # 本地开发环境变量（不提交 Git）
└── .gitignore
```

---

## 一、本地开发

### 1. 安装 Wrangler CLI

```bash
npm install -g wrangler
```

### 2. 登录 Cloudflare

```bash
wrangler login
```

### 3. 配置本地 API Key

在项目根目录创建 `.dev.vars`（此文件已被 `.gitignore` 排除）：

```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxx
```

### 4. 启动 Worker 本地开发服务器

```bash
wrangler dev
```

Worker 默认监听 `http://localhost:8787`，前端中将 API 地址指向该地址即可本地联调。

---

## 二、部署 Worker 到 Cloudflare

### 1. 发布 Worker

```bash
wrangler deploy
```

发布后会得到一个类似 `https://vira-demo-worker.<你的子域>.workers.dev` 的地址。

### 2. 设置生产环境变量（重要：密钥不能硬编码）

**方式 A — Wrangler CLI（推荐）**

```bash
wrangler secret put ANTHROPIC_API_KEY
# 执行后按提示粘贴你的 API Key
```

**方式 B — Cloudflare Dashboard**

进入 Workers & Pages → 选择你的 Worker → Settings → Variables and Secrets → 添加 `ANTHROPIC_API_KEY`（选择 Secret 类型）。

---

## 三、部署前端到 Cloudflare Pages

### 方式 A — 通过 Git 仓库（推荐）

1. 将项目推送到 GitHub / GitLab。
2. 进入 Cloudflare Dashboard → Workers & Pages → Create application → Pages → Connect to Git。
3. 选择仓库，配置如下：
   - **Build command**：留空（纯静态，无需构建）
   - **Build output directory**：`frontend`
4. 保存并部署。

### 方式 B — 直接上传

```bash
wrangler pages deploy frontend --project-name=vira-demo
```

---

## 四、配置前端 API 地址

前端 `fetch` 请求发送到你的 Worker URL：

```
POST https://vira-demo-worker.<你的子域>.workers.dev/api/chat
```

如果前端和 Worker 部署在同一个自定义域名下（通过 Routes 绑定），可以使用相对路径 `/api/chat`。

---

## 五、安全注意事项

- `ANTHROPIC_API_KEY` **绝不能**出现在前端代码或 Git 历史中。
- Worker 已通过服务端代理隔离密钥，前端无法直接获取。
- 生产环境建议在 Worker 中增加来源验证或速率限制，防止 API Key 滥用。
