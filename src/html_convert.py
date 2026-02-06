"""
Markdown → 微信公众号兼容 HTML 转换
"""

import markdown
import re


def markdown_to_wechat_html(md_content: str) -> str:
    """
    将 Markdown 转换为微信公众号兼容的 HTML

    微信公众号限制：
    - 不支持外部 CSS
    - 不支持 JS
    - 样式必须内联
    """
    # 先用 markdown 库转换
    html = markdown.markdown(
        md_content,
        extensions=['fenced_code', 'tables', 'nl2br']
    )

    # 添加内联样式
    html = add_inline_styles(html)

    return html


def add_inline_styles(html: str) -> str:
    """为 HTML 元素添加内联样式"""

    # 段落样式
    html = re.sub(
        r'<p>',
        '<p style="margin: 1em 0; line-height: 1.8; color: #333;">',
        html
    )

    # 标题样式
    html = re.sub(
        r'<h1>',
        '<h1 style="font-size: 1.5em; font-weight: bold; margin: 1.5em 0 0.5em; color: #333;">',
        html
    )
    html = re.sub(
        r'<h2>',
        '<h2 style="font-size: 1.3em; font-weight: bold; margin: 1.2em 0 0.5em; color: #333;">',
        html
    )
    html = re.sub(
        r'<h3>',
        '<h3 style="font-size: 1.1em; font-weight: bold; margin: 1em 0 0.5em; color: #333;">',
        html
    )

    # 代码块样式
    html = re.sub(
        r'<pre>',
        '<pre style="background: #f5f5f5; padding: 1em; border-radius: 4px; overflow-x: auto; font-size: 0.9em;">',
        html
    )
    html = re.sub(
        r'<code>',
        '<code style="background: #f5f5f5; padding: 0.2em 0.4em; border-radius: 3px; font-family: monospace;">',
        html
    )

    # 引用样式
    html = re.sub(
        r'<blockquote>',
        '<blockquote style="border-left: 4px solid #ddd; padding-left: 1em; margin: 1em 0; color: #666;">',
        html
    )

    # 列表样式
    html = re.sub(
        r'<ul>',
        '<ul style="margin: 1em 0; padding-left: 2em;">',
        html
    )
    html = re.sub(
        r'<ol>',
        '<ol style="margin: 1em 0; padding-left: 2em;">',
        html
    )
    html = re.sub(
        r'<li>',
        '<li style="margin: 0.5em 0; line-height: 1.6;">',
        html
    )

    # 图片样式（居中，最大宽度）
    html = re.sub(
        r'<img ',
        '<img style="max-width: 100%; height: auto; display: block; margin: 1em auto;" ',
        html
    )

    # 表格样式
    html = re.sub(
        r'<table>',
        '<table style="width: 100%; border-collapse: collapse; margin: 1em 0;">',
        html
    )
    html = re.sub(
        r'<th>',
        '<th style="border: 1px solid #ddd; padding: 8px; background: #f5f5f5; text-align: left;">',
        html
    )
    html = re.sub(
        r'<td>',
        '<td style="border: 1px solid #ddd; padding: 8px;">',
        html
    )

    # 链接样式
    html = re.sub(
        r'<a ',
        '<a style="color: #576b95; text-decoration: none;" ',
        html
    )

    return html
