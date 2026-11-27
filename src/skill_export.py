"""
租户专属 Skill 生成与导出（SKILL.md / scripts/publish.py / wechat-publish.zip）

按每个租户自己的 mcp_token 与 BASE_URL 动态渲染，确保多租户凭证完全隔离。
"""

import io
import json
import zipfile

from .html_convert import list_themes


def build_mcp_url(base_url: str, mcp_token: str) -> str:
    base = (base_url or "").rstrip("/")
    tok = (mcp_token or "").strip()
    return f"{base}/mcp/{tok}" if tok else ""


def build_mcp_snippet(base_url: str, mcp_token: str) -> str:
    mcp_url = build_mcp_url(base_url, mcp_token)
    return json.dumps(
        {
            "mcpServers": {
                "wechat-publisher": {
                    "url": mcp_url or f"{(base_url or '').rstrip('/')}/mcp/<请先完成公众号配置>",
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )


def build_skill_urls(base_url: str, mcp_token: str) -> dict[str, str]:
    base = (base_url or "").rstrip("/")
    tok = (mcp_token or "").strip()
    if not tok:
        return {"skill_url": "", "zip_url": "", "script_url": "", "install_cmd": ""}
    skill_url = f"{base}/skill/{tok}/SKILL.md"
    zip_url = f"{base}/skill/{tok}/wechat-publish.zip"
    script_url = f"{base}/skill/{tok}/scripts/publish.py"
    install_cmd = (
        f'mkdir -p ~/.claude/skills/wechat-publish/scripts && '
        f'curl -fsSL "{skill_url}" -o ~/.claude/skills/wechat-publish/SKILL.md && '
        f'curl -fsSL "{script_url}" -o ~/.claude/skills/wechat-publish/scripts/publish.py'
    )
    return {
        "skill_url": skill_url,
        "zip_url": zip_url,
        "script_url": script_url,
        "install_cmd": install_cmd,
    }


def render_skill_md(base_url: str, mcp_token: str, author: str = "") -> str:
    base = (base_url or "").rstrip("/")
    tok = (mcp_token or "").strip()
    mcp_url = build_mcp_url(base, tok)
    mcp_snippet = build_mcp_snippet(base, tok)
    urls = build_skill_urls(base, tok)
    themes_str = "、".join(f"`{t['id']}`（{t['name']}）" for t in list_themes())
    author_line = f"- 默认公众号作者：`{author}`\n" if (author or "").strip() else ""

    return f"""---
name: wechat-publish
description: 将 Markdown 文章或图文笔记发布到微信公众号预览与草稿箱（专属服务：{base}）。当用户说「发公众号」「推送草稿」「发布文章」「保存微信草稿」时触发。支持 MCP 远程工具调用，也支持通过 REST API 与内置脚本上传本地图片、创建文章、切换排版主题、生成预览链接并推送到微信草稿箱。
---

# 微信公众号发布台（WeChat Publisher）Skill

本 Skill 已绑定你的**专属私有凭证**，所有操作仅作用于你的公众号空间，与平台其他用户完全隔离。

## 一、核心接入信息

- 服务地址：`{base}`
{author_line}- 专属 MCP Server（Streamable HTTP）：`{mcp_url}`
- REST API 鉴权请求头：`Authorization: Bearer {tok}`
- Skill 在线地址（可直接发给 AI 读取安装）：`{urls["skill_url"]}`
- Skill 完整包下载地址：`{urls["zip_url"]}`

### 客户端 MCP 配置（Claude Desktop / Cursor / Antigravity / Cherry Studio）

```json
{mcp_snippet}
```

---

## 二、MCP 工具说明（推荐 AI 优先使用）

连接上述 MCP Server 后，可直接调用以下 7 个工具：

| 工具名 | 参数 | 功能说明与返回值 |
|---|---|---|
| `create_article` | `title`(必填), `content`(Markdown 必填), `theme_id`(可选，默认 `default`) | 创建文章并转为微信兼容排版，返回 JSON：`article_id`、`title`、`theme_id`、`preview_url`、`created_at` |
| `update_article` | `article_id`(必填), `title?`, `content?`, `theme_id?` | 更新已有文章的标题、正文或排版主题，预览链接保持可用 |
| `get_article` | `article_id`(必填) | 获取单篇文章详情、Markdown 原文、预览链接及发布状态 |
| `list_articles` | 无 | 列出当前账号下全部文章（含标题、ID、主题、预览链接与推送时间） |
| `save_image` | `image_url?` 或 `image_base64?` | 把公网图片或 Base64 图片转存到服务端，返回可在 Markdown 中引用的图片 URL |
| `generate_image` | `prompt`(必填), `aspect_ratio?` | 根据提示词生成配图并返回图片 URL（需平台开启生图模型） |
| `publish_to_draft` | `article_id`(必填) | 将文章内图片转存微信图床并推送到你的微信公众号草稿箱，返回 `draft_media_id` |

### 支持的排版主题 `theme_id`

{themes_str}

---

## 三、REST API 参考（脚本与无 MCP 客户端可用）

所有 `/api/*` 接口均需携带请求头：`Authorization: Bearer {tok}`

| 接口 | 方法 | 请求体 / 参数 | 含义与返回 |
|---|---|---|---|
| `/api/themes` | `GET` | 无 | 查询全部可用排版主题 ID 与名称 |
| `/api/images` | `POST` | `multipart/form-data` 字段 `file` | 上传本地图片，返回 `{{"url", "filename"}}` |
| `/api/articles` | `POST` | JSON `{{"title", "content", "theme_id"}}` | 创建文章，返回完整文章对象及 `id` |
| `/api/articles` | `GET` | 无 | 获取当前账号的文章列表 |
| `/api/articles/{{article_id}}` | `GET` | 无 | 获取单篇文章详情 |
| `/api/articles/{{article_id}}` | `PUT` | JSON 可选 `title` / `content` / `theme_id` | 更新文章内容或切换主题 |
| `/api/articles/{{article_id}}/publish` | `POST` | 无 | 推送到微信公众号草稿箱，返回 `{{"success", "draft_media_id", "message"}}` |
| `/api/preview-token` | `POST` | Query 可选 `article_id` | 生成 8 小时有效的临时预览令牌 `{{"token", "expires_at"}}`，用于拼接 `/preview/{{article_id}}?token=...` |

---

## 四、下载、安装与本地脚本用法

### 方式 A：在本地终端一键安装到 Agent Skills 目录

```bash
{urls["install_cmd"]}
```

（若使用 Antigravity / Codex，也可把下载目录改为 `~/.gemini/config/skills/wechat-publish/` 或 `~/.agents/skills/wechat-publish/`。）

### 方式 B：直接发送 Skill 链接让 AI 自动学习与安装

把以下地址粘贴给任何支持联网读取的 AI 助手：
- `{urls["skill_url"]}`

AI 读取后即可掌握全部 API 与 MCP 参数，也可以自动帮你下载解压 `{urls["zip_url"]}`。

### 方式 C：运行内置发布脚本（自动上传 `.assets/` 本地配图）

```bash
python3 ~/.claude/skills/wechat-publish/scripts/publish.py <markdown文件路径>
```

脚本会自动去除 YAML frontmatter、提取一级标题、扫描并上传本地配图、创建文章并输出带预览链接的 JSON：

```json
{{"title": "...", "article_id": "...", "preview_url": "{base}/preview/...", "image_count": 3}}
```
"""


def render_publish_script(base_url: str, mcp_token: str) -> str:
    base = (base_url or "").rstrip("/")
    tok = (mcp_token or "").strip()
    return f'''#!/usr/bin/env python3
"""
WeChat Publisher — 发布本地 Markdown 文章到公众号预览服务

用法: python3 publish.py <markdown文件路径> [theme_id]
输出: JSON {{title, article_id, preview_url, image_count}}
"""
import json
import mimetypes
import re
import sys
from pathlib import Path

import requests

BASE = {json.dumps(base)}
TOKEN = {json.dumps(tok)}
HEADERS = {{"Authorization": f"Bearer {{TOKEN}}"}}


def _upload_local_image(img_path: Path) -> str:
    mime = mimetypes.guess_type(img_path.name)[0] or "image/png"
    with open(img_path, "rb") as f:
        r = requests.post(
            f"{{BASE}}/api/images",
            headers=HEADERS,
            files={{"file": (img_path.name, f, mime)}},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["url"]


def publish_article(md_path: str, theme_id: str = "default") -> dict:
    p = Path(md_path).resolve()
    if not p.is_file():
        sys.exit(f"文件不存在: {{p}}")

    md = p.read_text(encoding="utf-8")
    md = re.sub(r"^---\\n.*?\\n---\\n+", "", md, count=1, flags=re.DOTALL)

    title_match = re.search(r"^#\\s+(.+?)\\s*$", md, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else p.stem
    if title_match:
        md = md[:title_match.start()] + md[title_match.end():]
    md = md.lstrip("\\n")

    # 匹配 Markdown 中的本地相对路径图片
    pattern = re.compile(r"!\\[([^\\]]*)\\]\\(<?([^)>\\s]+)>?\\)")
    url_map = {{}}
    uploaded = 0

    def _repl(m):
        nonlocal uploaded
        alt, target = m.group(1), m.group(2).strip()
        if re.match(r"^(https?:|data:)", target, re.IGNORECASE):
            return m.group(0)
        local_file = (p.parent / target).resolve()
        if not local_file.is_file():
            return m.group(0)
        key = str(local_file)
        if key not in url_map:
            url_map[key] = _upload_local_image(local_file)
            uploaded += 1
        return f"![{{alt}}]({{url_map[key]}})"

    new_md = pattern.sub(_repl, md)

    r = requests.post(
        f"{{BASE}}/api/articles",
        headers={{**HEADERS, "Content-Type": "application/json"}},
        json={{"title": title, "content": new_md, "theme_id": theme_id}},
        timeout=60,
    )
    r.raise_for_status()
    art = r.json()
    article_id = art["id"]

    r_tok = requests.post(
        f"{{BASE}}/api/preview-token",
        headers=HEADERS,
        params={{"article_id": article_id}},
        timeout=30,
    )
    r_tok.raise_for_status()
    preview_token = r_tok.json()["token"]
    preview_url = f"{{BASE}}/preview/{{article_id}}?token={{preview_token}}"

    result = {{
        "title": title,
        "article_id": article_id,
        "preview_url": preview_url,
        "image_count": uploaded,
    }}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: python3 {{sys.argv[0]}} <markdown文件路径> [theme_id]")
        sys.exit(1)
    theme = sys.argv[2] if len(sys.argv) >= 3 else "default"
    try:
        publish_article(sys.argv[1], theme)
    except Exception as e:
        sys.exit(f"发布失败: {{e}}")
'''


def build_skill_zip(base_url: str, mcp_token: str, author: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wechat-publish/SKILL.md", render_skill_md(base_url, mcp_token, author))
        zf.writestr("wechat-publish/scripts/publish.py", render_publish_script(base_url, mcp_token))
    return buf.getvalue()


def render_agent_bundle_text(base_url: str, mcp_token: str, author: str = "") -> str:
    mcp_url = build_mcp_url(base_url, mcp_token)
    mcp_snippet = build_mcp_snippet(base_url, mcp_token)
    urls = build_skill_urls(base_url, mcp_token)
    if not mcp_token:
        return ""
    return f"""【微信公众号发布台 · 专属 Agent 接入指南】

1. 私有 MCP Server 地址（适配 Claude / Cursor / Antigravity 等所有支持 MCP 的客户端）：
{mcp_url}

MCP JSON 配置：
{mcp_snippet}

2. 专属 Agent Skill 在线文档与 API 规范（可直接发给 AI 让其读取与自动安装）：
{urls["skill_url"]}

3. Skill 压缩包下载地址：
{urls["zip_url"]}

4. 本地终端一键安装 Skill（含 SKILL.md 与自动上传图片发布脚本）：
{urls["install_cmd"]}

5. 携带私有 Token 的 REST API 请求头：
Authorization: Bearer {mcp_token}
"""
