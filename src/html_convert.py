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


def list_themes() -> list[dict]:
    """返回可用主题列表"""
    if not _theme_cache:
        load_themes()
    return [
        {"id": tid, "name": THEME_NAMES.get(tid, tid)}
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


def markdown_to_html(md_content: str) -> str:
    """Markdown → 原始 HTML（无样式）"""
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

    return result


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
