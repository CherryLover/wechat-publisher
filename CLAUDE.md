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
| `upload_image` | `article_id`, `image_url` 或 `image_base64` | 上传图片到服务端，返回本地图片 URL（供 Markdown 引用） |
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
- **域名**：待分配（通过 Traefik 反代）
- **MCP 端点**：`https://<域名>/mcp`（SSE 传输）

### 需要的配置

- 微信公众号 AppID
- 微信公众号 AppSecret
- （可选）访问密钥，防止 MCP 端点被未授权访问

### 功能规划

**V1 - 基础版（MCP + 预览 + 草稿）**：
1. MCP Server：提供 create/update/upload_image/publish/get/list 工具
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
