"""
Markdown → 微信公众号兼容 HTML 转换（主题引擎）
"""

import re
from pathlib import Path

import css_inline
import markdown

THEMES_DIR = Path(__file__).parent.parent / "themes"

# 主题缓存：{theme_id: css_content}
_theme_cache: dict[str, str] = {}

# 主题显示名
THEME_NAMES = {
    "default": "默认",
    "purple": "Purple",
    "lapis": "Lapis",
    "rainbow": "Rainbow",
    "maize": "Maize",
    "orangeheart": "Orange Heart",
    "phycat": "Phycat",
    "pie": "Pie",
    "juejin_default": "掘金",
    "medium_default": "Medium",
    "toutiao_default": "头条",
    "zhihu_default": "知乎",
}


def load_themes():
    """启动时加载所有主题 CSS 到内存"""
    _theme_cache.clear()
    for css_file in THEMES_DIR.glob("*.css"):
        theme_id = css_file.stem
        _theme_cache[theme_id] = css_file.read_text(encoding="utf-8")


THEME_COLORS = {
    "default": "#ff502c",
    "juejin_default": "#1e80ff",
    "lapis": "#4870ac",
    "maize": "#e49123",
    "medium_default": "#191b1f",
    "orangeheart": "#e9694a",
    "phycat": "#3db8bf",
    "pie": "#f27f79",
    "purple": "#8064a9",
    "rainbow": "linear-gradient(90deg,#ff5f6d,#ffc371,#47cf73,#4aa8ff,#a06bff)",
    "toutiao_default": "#0069c2",
    "zhihu_default": "#535861",
}


def list_themes() -> list[dict]:
    """返回可用主题列表（含代表色，给设置页用）"""
    if not _theme_cache:
        load_themes()
    return [
        {
            "id": tid,
            "name": THEME_NAMES.get(tid, tid),
            "color": THEME_COLORS.get(tid, "#e4e4e7"),
        }
        for tid in sorted(_theme_cache.keys())
    ]


def _get_theme_css(theme_id: str) -> str:
    """获取主题 CSS，不存在则回退到 default"""
    if not _theme_cache:
        load_themes()
    return _theme_cache.get(theme_id, _theme_cache.get("default", ""))


def _parse_css_declarations(block: str) -> dict[str, str]:
    """
    解析 CSS 声明块中的属性，正确处理值中包含分号的情况（如 SVG data URL）。

    通过跟踪引号和括号状态，在正确的分号位置分割声明。
    """
    variables: dict[str, str] = {}
    i = 0
    length = len(block)

    while i < length:
        # 跳过空白和注释
        while i < length and block[i] in ' \t\n\r':
            i += 1
        if i >= length:
            break
        # 跳过 CSS 注释
        if block[i:i+2] == '/*':
            end = block.find('*/', i + 2)
            i = end + 2 if end != -1 else length
            continue

        # 找属性名
        j = i
        while j < length and block[j] != ':':
            j += 1
        if j >= length:
            break
        prop_name = block[i:j].strip()
        j += 1  # 跳过冒号

        # 找值（需要正确处理引号和括号中的分号）
        value_start = j
        in_single_quote = False
        in_double_quote = False
        paren_depth = 0

        while j < length:
            ch = block[j]
            if ch == '\\':
                j += 2  # 跳过转义字符
                continue
            if in_single_quote:
                if ch == "'":
                    in_single_quote = False
            elif in_double_quote:
                if ch == '"':
                    in_double_quote = False
            else:
                if ch == "'":
                    in_single_quote = True
                elif ch == '"':
                    in_double_quote = True
                elif ch == '(':
                    paren_depth += 1
                elif ch == ')':
                    paren_depth = max(0, paren_depth - 1)
                elif ch == ';' and paren_depth == 0:
                    break
            j += 1

        value = block[value_start:j].strip()
        if prop_name.startswith('--'):
            variables[prop_name] = value
        i = j + 1

    return variables


def resolve_css_variables(css: str) -> str:
    """
    解析 CSS 变量：将 :root 中定义的 var(--xxx) 替换为实际值。

    支持嵌套变量（如 --shadow: 3px 3px 10px var(--shadow-color)），
    通过多轮替换处理依赖链。
    """
    # 提取 :root 块中的变量定义
    root_match = re.search(r':root\s*\{([^}]+)\}', css, re.DOTALL)
    if not root_match:
        return css

    root_block = root_match.group(1)
    variables = _parse_css_declarations(root_block)

    # 多轮解析，处理变量之间的依赖（最多 5 轮）
    for _ in range(5):
        changed = False
        for var_name, var_value in variables.items():
            resolved = re.sub(
                r'var\((--[\w-]+)\)',
                lambda m: variables.get(m.group(1), m.group(0)),
                var_value
            )
            if resolved != var_value:
                variables[var_name] = resolved
                changed = True
        if not changed:
            break

    # 在 CSS 中替换所有 var(--xxx)
    def replace_var(m):
        return variables.get(m.group(1), m.group(0))

    resolved_css = re.sub(r'var\((--[\w-]+)\)', replace_var, css)

    # 移除 :root 块（内联后不需要）
    resolved_css = re.sub(r':root\s*\{[^}]+\}', '', resolved_css, count=1)

    return resolved_css


def _extract_pseudo_rules(css: str) -> str:
    """
    从 CSS 中提取伪元素/伪类规则（::before, ::after, :nth-child 等）。
    这些规则无法内联，需要保留在 <style> 标签中。
    """
    pseudo_rules = []
    # 匹配包含伪元素/伪类的 CSS 规则块
    pattern = r'([^{}]*(?:::before|::after|:nth-child|:hover|:focus)[^{]*)\{([^}]*)\}'
    for match in re.finditer(pattern, css):
        selector = match.group(1).strip()
        body = match.group(2).strip()
        if body:  # 只保留有内容的规则
            pseudo_rules.append(f"{selector} {{{body}}}")
    return '\n'.join(pseudo_rules)


_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _looks_like_table_row(line: str) -> bool:
    s = (line or "").strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")     # 不切分转义过的 \|


def _pipe_count(line: str) -> int:
    body = (line or "").strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    return len(_CELL_SPLIT_RE.split(body))


def merge_pipe_tables(md: str) -> str:
    """把被空行拆散的「裸竖线表格」重新拼成合法 GFM 表格。

    Notion 里直接粘贴的 markdown 表格会被拆成一堆 paragraph 块，
    每行之间被插入空行，导致 python-markdown 认不出表格。这里：
    - 连续（允许夹空行）的 | ... | 行合到一组
    - 缺分隔行就补一行 ---
    - 列数不齐的行补齐/截断到表头列数
    - 代码块里的内容一律不碰
    """
    lines = md.split("\n")
    out: list[str] = []
    i, in_code, changed = 0, False, False

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if in_code or not _looks_like_table_row(line):
            out.append(line)
            i += 1
            continue

        group: list[str] = []
        j = i
        while j < len(lines):
            if _looks_like_table_row(lines[j]):
                group.append(lines[j])
                j += 1
                continue
            if lines[j].strip() == "":
                k = j
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
                if k < len(lines) and _looks_like_table_row(lines[k]):
                    j = k
                    continue
            break

        if len(group) < 2:
            out.extend(group)
            out.append("")
            i = j
            continue

        ncol = max(_pipe_count(g) for g in group)
        has_sep = _TABLE_SEP_RE.match(group[1].strip()) is not None

        def fit(raw: str) -> str:
            body = raw.strip()
            if body.startswith("|"):
                body = body[1:]
            if body.endswith("|"):
                body = body[:-1]
            cells = [c.strip() for c in _CELL_SPLIT_RE.split(body)]
            cells = (cells + [""] * ncol)[:ncol]
            return "| " + " | ".join(cells) + " |"

        sep = "| " + " | ".join(["---"] * ncol) + " |"
        if has_sep:
            # 已经是合法表格：保留原分隔行（含 :---: 对齐标记）
            fixed = [fit(group[0]), group[1].strip()]
            rest = group[2:]
        else:
            # 没有分隔行 → 把首行当表头（粘贴来的表格基本都带表头）
            fixed = [fit(group[0]), sep]
            rest = group[1:]

        for g in rest:
            if _TABLE_SEP_RE.match(g.strip()):
                continue
            fixed.append(fit(g))

        out.extend(fixed)
        out.append("")
        changed = True
        i = j

    if not changed:
        return md
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n") + "\n"


# ---------- 表格：微信友好的内联样式 ----------

_TABLE_TAG_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S | re.I)


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return f"rgba(0,0,0,{alpha})"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(0,0,0,{alpha})"
    return f"rgba({r},{g},{b},{alpha})"


def _add_style(html: str, tag: str, style: str) -> str:
    """给某类标签追加内联样式（已有 style 就合并，不覆盖）"""
    def repl(m):
        attrs = m.group(1) or ""
        if re.search(r'style="', attrs):
            attrs = re.sub(
                r'style="([^"]*)"',
                lambda mm: f'style="{mm.group(1).rstrip(";")};{style}"',
                attrs, count=1)
        else:
            attrs = attrs + f' style="{style}"'
        return f"<{tag}{attrs}>"

    return re.sub(rf"<{tag}(?=[\s>])([^>]*)>", repl, html)


def style_tables(html: str, theme_id: str = "default") -> str:
    """把 Markdown 出来的裸 <table> 加工成公众号里能看的样子。

    主题 CSS 里没有表格规则（微信也不支持外部样式），所以这里直接写内联：
    浅色表头底 + 细边框 + 舒适内边距 + 斑马纹 + 横向滚动兜底。
    """
    if "<table" not in (html or ""):
        return html

    color = THEME_COLORS.get(theme_id) or "#5b6b6b"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color or ""):
        color = "#5b6b6b"

    head_bg = _hex_to_rgba(color, 0.08)
    zebra = _hex_to_rgba(color, 0.03)
    border = "rgba(0,0,0,0.10)"
    text = "#2f3437"

    def one(match):
        t = match.group(0)
        if "border-collapse:collapse" in t:      # 已经加工过，直接跳过
            return t

        # Notion 没有表头时我们会造一个空表头，渲染上直接去掉
        thead = re.search(r"<thead>(.*?)</thead>", t, re.S)
        if thead and not re.sub(r"<[^>]+>|&nbsp;|\s", "", thead.group(1)):
            t = t[:thead.start()] + t[thead.end():]

        t = _add_style(t, "table",
                       "width:100%;border-collapse:collapse;border-spacing:0;"
                       "font-size:13px;line-height:1.6;word-break:break-word;margin:0;")
        t = _add_style(t, "th",
                       f"padding:8px 10px;border:1px solid {border};background:{head_bg};"
                       f"font-weight:600;color:{text};text-align:left;font-size:13px;")
        t = _add_style(t, "td",
                       f"padding:8px 10px;border:1px solid {border};"
                       f"vertical-align:top;color:{text};font-size:13px;")

        # 斑马纹：数据行隔行加浅底
        parts = re.split(r"(<tr\b[^>]*>.*?</tr>)", t, flags=re.S)
        merged, idx = [], 0
        for part in parts:
            if re.match(r"<tr\b", part or ""):
                idx += 1
                if idx > 1 and idx % 2 == 0:
                    part = _add_style(part, "td", f"background:{zebra};")
            merged.append(part)
        t = "".join(merged)

        return f'<section style="overflow-x:auto;margin:1.2em 0;">{t}</section>'

    return _TABLE_TAG_RE.sub(one, html)


def markdown_to_html(md_content: str) -> str:
    """Markdown → 原始 HTML（无样式）

    先做一遍表格修复：Notion 里粘贴的 markdown 表格常被空行拆散、
    或缺少 GFM 必需的分隔行，不修的话会渲染成裸竖线文字。
    """
    md_content = merge_pipe_tables(md_content or "")
    return markdown.markdown(
        md_content,
        extensions=['fenced_code', 'tables', 'nl2br']
    )


def apply_theme_for_preview(html: str, theme_id: str = "default") -> str:
    """
    生成预览用 HTML：<style>CSS</style><section id="wenyan">HTML</section>

    预览页直接用 <style> 标签加载 CSS，渲染效果完整。
    """
    css = _get_theme_css(theme_id)
    html = style_tables(html, theme_id)
    return f'<style>\n{css}\n</style>\n<section id="wenyan">\n{html}\n</section>'


def apply_theme_for_publish(html: str, theme_id: str = "default") -> str:
    """
    生成发布用 HTML：CSS → 内联样式（微信不支持 <style> 标签）。

    步骤：
    1. 解析 CSS 变量
    2. 提取伪元素规则（无法内联，保留在 <style> 中）
    3. 用 css-inline 将普通规则转为内联样式
    """
    css = _get_theme_css(theme_id)
    resolved_css = resolve_css_variables(css)

    # 注入列表基础样式（使用 list-style:none，后续用文本前缀替代序号）
    list_base_css = """
#wenyan ul { list-style: none; padding-left: 2em; margin: 1em 0; }
#wenyan ol { list-style: none; padding-left: 2em; margin: 1em 0; }
#wenyan li { margin: 0.5em 0; }
#wenyan li p { margin: 0; display: inline; }
"""
    # 主题 CSS 放后面，可以覆盖基础样式
    combined_css = list_base_css + resolved_css

    # 提取伪元素规则
    pseudo_css = _extract_pseudo_rules(combined_css)

    # 构建完整 HTML 文档供 css-inline 处理
    full_html = f"""<html><head><style>{combined_css}</style></head>
<body><section id="wenyan">{html}</section></body></html>"""

    inliner = css_inline.CSSInliner(
        inline_style_tags=True,
        keep_style_tags=False,
        keep_link_tags=False,
    )
    inlined = inliner.inline(full_html)

    # 提取 <section id="wenyan" ...>...</section> 部分（css-inline 会给 section 加 style）
    m = re.search(
        r'(<section id="wenyan"[^>]*>)(.*?)(</section>)',
        inlined,
        re.DOTALL
    )
    if m:
        result = m.group(1) + m.group(2) + m.group(3)
    else:
        result = f'<section id="wenyan">{html}</section>'

    # 如果有伪元素规则，加上 <style> 标签
    if pseudo_css.strip():
        result = f'<style>{pseudo_css}</style>\n{result}'

    # 表格：微信不支持外部样式，这里全部转成内联
    return style_tables(result, theme_id)


def _inject_list_prefixes(html: str) -> str:
    """
    给列表项注入文本前缀（• 和 1. 2. 3.），同时去掉原生 list-style。

    微信公众号对 list-style-type 支持不稳定，会出现多余空白序号。
    参考 doocs/md 的做法：用文本前缀替代 CSS 序号，彻底避免渲染问题。
    """
    # 处理无序列表：在 <li> 内容前插入 "• "
    def replace_ul(m: re.Match) -> str:
        ul_tag = m.group(1)  # <ul ...>
        ul_body = m.group(2)  # 列表内容
        ul_close = m.group(3)  # </ul>

        # 强制 list-style: none（移除主题可能内联的 list-style-type）
        ul_tag = re.sub(r'list-style[^;"]*:[^;"]*;?', '', ul_tag)
        if 'style="' in ul_tag:
            ul_tag = ul_tag.replace('style="', 'style="list-style:none;')
        else:
            ul_tag = ul_tag.replace('>', ' style="list-style:none;">', 1)

        # 给每个 <li> 添加 • 前缀
        ul_body = re.sub(
            r'(<li[^>]*>)',
            r'\1• ',
            ul_body
        )
        return ul_tag + ul_body + ul_close

    # 处理有序列表：在 <li> 内容前插入 "1. " "2. " ...
    def replace_ol(m: re.Match) -> str:
        ol_tag = m.group(1)
        ol_body = m.group(2)
        ol_close = m.group(3)

        # 强制 list-style: none（移除主题可能内联的 list-style-type）
        ol_tag = re.sub(r'list-style[^;"]*:[^;"]*;?', '', ol_tag)
        if 'style="' in ol_tag:
            ol_tag = ol_tag.replace('style="', 'style="list-style:none;')
        else:
            ol_tag = ol_tag.replace('>', ' style="list-style:none;">', 1)

        counter = [0]
        def add_number(li_match: re.Match) -> str:
            counter[0] += 1
            return f'{li_match.group(1)}{counter[0]}. '

        ol_body = re.sub(r'(<li[^>]*>)', add_number, ol_body)
        return ol_tag + ol_body + ol_close

    html = re.sub(r'(<ul[^>]*>)(.*?)(</ul>)', replace_ul, html, flags=re.DOTALL)
    html = re.sub(r'(<ol[^>]*>)(.*?)(</ol>)', replace_ol, html, flags=re.DOTALL)
    return html


def markdown_to_wechat_html(md_content: str, theme_id: str = "default") -> str:
    """
    主入口：Markdown → 微信兼容 HTML（带内联样式）。

    向后兼容旧调用方式。
    """
    html = markdown_to_html(md_content)
    result = apply_theme_for_publish(html, theme_id)
    result = _inject_list_prefixes(result)
    return result
