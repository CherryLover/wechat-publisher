# 微信公众号语音发布工具

## 项目目标

解决"手机语音输入 → 微信公众号发文"的全流程问题。

**用户场景**：在手机上跟 AI 对话，AI 通过 MCP 工具直接创建/修改文章，用户在预览页确认后发布到公众号草稿箱。全程不离开 AI 对话界面。

**核心限制**：手机端公众号助手不支持富文本粘贴，因此必须通过微信公众号 API 直接创建草稿。

## 技术方案

### 整体流程

```
用户跟 AI 对话（语音/文字）
  → AI 整理内容，调 MCP 工具 create_article
  → 服务端存储文章（Markdown → 微信兼容 HTML）
  → 返回预览链接给用户
用户点预览链接查看效果
  → 需要修改：跟 AI 说 → AI 调 MCP 工具 update_article → 预览自动更新
  → 确认没问题：跟 AI 说"发布" → AI 调 MCP 工具 publish_to_draft → 推送到公众号草稿箱
用户在公众号助手中发布
```

### 架构

两个组件，部署在同一个 Docker 容器中：

1. **HTTP 服务（FastAPI）**：
   - 存储文章（Markdown + 渲染后的 HTML）
   - 提供预览页面（模拟公众号排版样式）
   - 对接微信公众号 API（获取 token、创建草稿）

2. **MCP Server（SSE 传输）**：
   - 暴露工具给 AI 客户端调用
   - 通过 HTTP 调用本地 FastAPI 服务
   - 任何支持 MCP 的客户端都能接入（Claude Code、Claude Desktop、Cherry Studio 等）

### MCP 工具定义

| 工具名 | 参数 | 作用 |
|---|---|---|
| `create_article` | `title`, `content`(Markdown) | 创建文章，返回预览链接 |
| `update_article` | `article_id`, `title?`, `content?`(Markdown) | 更新文章内容，预览链接不变 |
| `save_image` | `article_id`, `image_url` 或 `image_base64` | 保存图片到服务端，返回本地图片 URL（供 Markdown 引用） |
| `publish_to_draft` | `article_id` | 推送到公众号草稿箱（自动转存图片到微信） |
| `get_article` | `article_id` | 获取文章当前内容和预览链接 |
| `list_articles` | 无 | 列出所有文章 |

### 技术栈

- **部署方式**：Docker 容器，部署在用户服务器（82.180.162.81），通过 Dokploy 管理
- **后端语言**：Python（FastAPI）
- **MCP 传输**：SSE（Streamable HTTP），便于远程客户端连接
- **存储**：SQLite（轻量，单文件，够用）
- **前端**：预览页面（模拟公众号排版样式，纯展示 + 确认按钮）
- **图片存储**：服务器本地临时存储（Docker volume），定期清理过期文件
- **微信 API**：
  - 获取 access_token：`GET https://api.weixin.qq.com/cgi-bin/token`
  - 上传文章内图片：`POST https://api.weixin.qq.com/cgi-bin/media/uploadimg`（返回微信 URL）
  - 上传封面图（永久素材）：`POST https://api.weixin.qq.com/cgi-bin/material/add_material`（返回 media_id）
  - 创建草稿：`POST https://api.weixin.qq.com/cgi-bin/draft/add`

### 部署相关

- **服务器**：82.180.162.81（海外服务器，微信 API 无地区限制）
- **IP 白名单**：需要在微信公众平台「设置与开发 → 基本配置 → IP白名单」中添加 82.180.162.81
- **服务管理**：Docker Swarm + Dokploy + Traefik
- **域名**：`publisher.flyooo.uk`（通过 Traefik 反代）
- **MCP 端点**：`https://publisher.flyooo.uk/mcp/`（Streamable HTTP 传输）

### 需要的配置

- 微信公众号 AppID（`WX_APPID`）
- 微信公众号 AppSecret（`WX_APPSECRET`）
- 访问密钥（`AUTH_TOKEN`）：保护 API 和 MCP 端点，未设置时跳过鉴权

### 功能规划

**V1 - 基础版（MCP + 预览 + 草稿）**：
1. MCP Server：提供 create/update/save_image/publish/get/list 工具
2. HTTP 服务：文章存储 + 预览页面 + 微信 API 对接
3. 预览页：模拟公众号排版样式渲染文章
4. Markdown → 微信公众号兼容 HTML 转换
5. 图片处理：本地暂存 → 预览时直接加载 → 发布时自动转存到微信（media/uploadimg）→ 替换 URL
6. 封面图：通过永久素材 API 上传，获取 media_id
7. access_token 缓存和自动刷新
8. 本地临时文件（图片等）定期清理（保留 7 天）
9. Docker 部署

**V2 - 增强版（可选）**：
- 预览页支持确认发布按钮（不依赖 AI 对话）
- 支持选择文章风格/排版主题
- 历史记录和文章管理
- 接管现有 wx-notify Worker 的消息处理功能

## 现有资源

### wx-notify（Cloudflare Worker）
已有一个 `wx-notify` Worker 在处理公众号消息：
- 微信服务器验证（SHA1 签名）
- 关注/取关 → Telegram 通知
- 群发结果 → Telegram 通知
- 文字消息 → 查 Notion 数据库关键词自动回复
- 环境变量：WX_TOKEN、NOTION_DB_ID、NOTION_TOKEN、NOTION_VERSION、TG_BOT、TG_CHAT_ID

V2 阶段可以考虑把这个 Worker 的功能迁移到服务器上统一管理。

## 项目结构（规划）

```
wechat-publisher/
├── CLAUDE.md              # 项目说明
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── src/
│   ├── main.py            # FastAPI 入口（HTTP 服务 + MCP 挂载）
│   ├── mcp_server.py      # MCP Server 定义（工具注册）
│   ├── wechat.py          # 微信公众号 API 封装（token、草稿）
│   ├── article.py         # 文章存储和管理（SQLite）
│   └── html_convert.py    # Markdown → 微信兼容 HTML 转换
├── templates/
│   └── preview.html       # 文章预览页模板（模拟公众号样式）
├── static/                # 静态资源（CSS 等）
└── .env                   # 配置（AppID、AppSecret）
```

## 注意事项

- 微信公众号 access_token 有效期 2 小时，需要缓存和自动刷新
- 微信公众号 HTML 有格式限制（不支持外部 CSS、不支持 JS）
- 草稿创建需要 `title` 和 `content` 字段，content 为 HTML
- **图片不能用外部 URL**：微信后台会过滤所有非微信域名的图片链接
- 文章内图片必须通过 `media/uploadimg` 上传到微信，拿到 `mmbiz.qpic.cn` 域名的 URL
- 封面图必须通过 `material/add_material` 上传为永久素材，拿到 `media_id`
- 图片仅支持 jpg/png 格式，大小 1MB 以下
- 图片暂存服务器本地，不需要额外的云存储（OSS 等），用完定期清理即可
- 服务器 IP（82.180.162.81）必须加到微信公众平台的 IP 白名单中
- MCP 端点需要做访问控制，防止未授权调用
- AI 负责内容整理，服务端只做存储、渲染和微信 API 调用，不再额外调 AI API

---

## 开发进度

### 阶段划分

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | API 验证 + HTTP 服务 + 部署 | ✅ 已完成 |
| Phase 2 | 文章排版样式（集成文颜主题） | ✅ 已完成 |
| Phase 3 | MCP Server | ✅ 已完成 |
| Phase 4 | 增强功能（封面图、鉴权、清理等） | 🔄 进行中 |

### Phase 1 - 基础服务（已完成）

**完成时间**：2026-02-06

**完成内容**：

1. **微信 API 验证**
   - ✅ access_token 获取和缓存
   - ✅ 图片上传（media/uploadimg）
   - ✅ 封面图上传（material/add_material）
   - ✅ 草稿创建（draft/add）
   - ✅ 中文编码问题修复（ensure_ascii=False）

2. **FastAPI HTTP 服务**
   - ✅ 文章 CRUD API
   - ✅ 图片上传 API（存本地）
   - ✅ 发布 API（自动转存图片到微信）
   - ✅ 预览页面（模拟公众号样式）
   - ✅ Markdown → 微信兼容 HTML 转换

3. **部署**
   - ✅ Dockerfile
   - ✅ docker-compose.yml（Traefik 标签）
   - ✅ 域名配置：`https://publisher.flyooo.uk`
   - ✅ HTTPS + 反向代理

**当前 API**：

| 接口 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/api/preview-token` | POST | Bearer Token | 换取临时预览 token |
| `/api/images` | POST | Bearer Token | 保存图片到本地 |
| `/api/articles` | POST | Bearer Token | 创建文章 |
| `/api/articles` | GET | Bearer Token | 列出所有文章 |
| `/api/articles/{id}` | GET | Bearer Token | 获取文章 |
| `/api/articles/{id}` | PUT | Bearer / 临时 Token | 更新文章 |
| `/api/articles/{id}/publish` | POST | Bearer / 临时 Token | 发布到草稿箱 |
| `/preview/{id}` | GET | 临时 Token (query) | 预览页面 |
| `/` | GET | 临时 Token (query) | 文章列表页 |
| `/images/{filename}` | GET | 无 | 获取本地图片 |

**图片处理流程**：
```
上传图片 → 存本地 → 预览显示本地图片 → 发布时自动转存到微信 → 创建草稿
```

### Phase 2 - 文章排版样式（下一步）

**目标**：集成文颜（Wenyan）主题，提升文章排版质量

**参考项目**：
- [caol64/wenyan-core](https://github.com/caol64/wenyan-core) — 核心样式库
- 12 个主题 CSS 文件，每个 2-9KB

**待实现**：
- [ ] 复制文颜 CSS 主题文件到项目
- [ ] CSS → 内联样式转换（微信不支持外部 CSS）
- [ ] 创建/更新文章时支持选择主题
- [ ] 预览页直接加载 CSS 渲染
- [ ] 发布时转为内联样式

### Phase 3 - MCP Server（已完成）

**完成时间**：2026-02-09

**完成内容**：

1. **MCP Server（Streamable HTTP 传输）**
   - ✅ FastMCP 实例（stateless_http 模式）
   - ✅ 挂载到 FastAPI（/mcp/ 端点）
   - ✅ session_manager 集成到 lifespan

2. **6 个 MCP 工具**
   - ✅ `create_article(title, content, theme_id?)` — 创建文章
   - ✅ `update_article(article_id, title?, content?, theme_id?)` — 更新文章
   - ✅ `get_article(article_id)` — 获取文章详情
   - ✅ `list_articles()` — 列出所有文章
   - ✅ `save_image(image_url?, image_base64?)` — 保存图片
   - ✅ `publish_to_draft(article_id)` — 发布到草稿箱

3. **重构**
   - ✅ 提取 `publish_article()` 共享函数（HTTP API + MCP 共用）
   - ✅ 添加 `BASE_URL` 环境变量支持
   - ✅ 延迟导入避免循环引用

**MCP 客户端配置**：
```json
{
  "mcpServers": {
    "wechat-publisher": {
      "type": "streamable-http",
      "url": "https://publisher.flyooo.uk/mcp/",
      "headers": {
        "Authorization": "Bearer <AUTH_TOKEN>"
      }
    }
  }
}
```

**新增文件**：`src/mcp_server.py`
**修改文件**：`src/main.py`

### Phase 4 - 增强功能（进行中）

**已完成**：
- [x] API 和 MCP 端点鉴权（Bearer Token，环境变量 `AUTH_TOKEN` 配置）
- [x] 网页预览鉴权（临时 Token，8 小时有效，JSON 文件存储）
- [x] MCP 工具返回的预览链接自动带临时 Token
- [x] 预览页操作（主题切换、发布）自动传递 Token

**鉴权方案说明**：
- **主 Token**（`AUTH_TOKEN` 环境变量）：保护 `/api/*` 和 `/mcp/*` 端点，Bearer Token 方式
- **临时 Token**：保护网页预览（`/` 和 `/preview/{id}`），8 小时有效，通过 `POST /api/preview-token` 换取
- **预览页操作**（主题切换、发布到草稿箱）同时接受主 Token 和临时 Token
- **图片路由** `/images/{filename}` 不做鉴权（文件名随机，无安全风险）
- 未设置 `AUTH_TOKEN` 时所有鉴权跳过（本地开发兼容）

**新增文件**：`src/auth.py`
**修改文件**：`src/main.py`, `src/mcp_server.py`, `templates/preview.html`, `templates/articles.html`

**待实现**：
- [ ] 封面图支持指定（目前用随机图片）
- [ ] 本地图片定期清理（7 天过期）

---

## 快速命令

**本地开发**：
```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn src.main:app --reload
```

**服务器部署**：
```bash
# SSH 到服务器后
cd /root/wechat-publisher
git pull
docker compose up -d --build

# 查看日志
docker logs -f wechat-publisher
```

**测试 API**：
```bash
# 创建文章
curl -X POST https://publisher.flyooo.uk/api/articles \
  -H "Content-Type: application/json" \
  -d '{"title": "测试", "content": "# 标题\n\n内容"}'

# 发布到草稿箱
curl -X POST https://publisher.flyooo.uk/api/articles/{id}/publish
```
