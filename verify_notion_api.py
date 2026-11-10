#!/usr/bin/env python3
"""
Notion API 验证脚本
验证：1. Token 可用  2. 读取「公众号文章库」结构  3. 统计记录分布

说明：notion-client 3.x 走 Notion 新版接口，数据库字段挂在 data source 下，
本脚本自动兼容新旧两种结构。

用法：
    .venv/bin/python verify_notion_api.py
"""

import os

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

TOKEN = os.getenv("NOTION_TOKEN")
DB_ID = os.getenv("NOTION_DB_ID")
PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")


def step(n, title):
    print("=" * 50)
    print(f"步骤 {n}: {title}")
    print("=" * 50)


def plain(prop):
    """把任意 Notion 属性压成可打印的纯文本"""
    if not prop:
        return None
    t = prop["type"]
    v = prop.get(t)

    if t in ("title", "rich_text"):
        return "".join(x["plain_text"] for x in v)
    if t == "select":
        return v["name"] if v else None
    if t == "multi_select":
        return [o["name"] for o in v]
    if t == "date":
        return (v or {}).get("start")
    if t == "number":
        return v
    if t == "url":
        return v
    return str(v)[:60]


def resolve_source(client, db_id):
    """返回 (properties, data_source_id)。新版接口字段在 data source 上。"""
    db = client.databases.retrieve(database_id=db_id)
    sources = db.get("data_sources") or []
    if sources:
        ds_id = sources[0]["id"]
        ds = client.data_sources.retrieve(data_source_id=ds_id)
        return ds["properties"], ds_id, db
    return db["properties"], None, db


def query_all(client, db_id, ds_id):
    """分页拉全量记录"""
    pages, cursor = [], None
    while True:
        kw = {"page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        if ds_id:
            res = client.data_sources.query(data_source_id=ds_id, **kw)
        else:
            res = client.databases.query(database_id=db_id, **kw)
        pages.extend(res["results"])
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def dump_schema(props, db, ds_id):
    print(f"数据库名称: {''.join(t['plain_text'] for t in db['title'])}")
    print(f"数据库 ID : {db['id']}")
    if ds_id:
        print(f"数据源 ID : {ds_id}")
    print(f"创建时间  : {db['created_time'][:10]}")
    print(f"最后编辑  : {db['last_edited_time'][:16]}")
    print(f"\n字段共 {len(props)} 个:")

    for name, p in props.items():
        t = p["type"]
        extra = ""
        cfg = p.get(t)
        if isinstance(cfg, dict):
            if "options" in cfg:
                opts = [o["name"] for o in cfg["options"]]
                extra = f"  选项({len(opts)}): " + ", ".join(opts[:8])
                if len(opts) > 8:
                    extra += f" … 等 {len(opts)} 个"
            elif "format" in cfg:
                extra = f"  格式: {cfg['format']}"
        print(f"  - {name:<12} {t:<12}{extra}")


def main():
    if not TOKEN:
        raise SystemExit("缺少 NOTION_TOKEN，请在 .env 中配置")
    if not DB_ID:
        raise SystemExit("缺少 NOTION_DB_ID，请在 .env 中配置")

    client = Client(auth=TOKEN)

    step(1, "验证 Token 并读取数据库")
    props, ds_id, db = resolve_source(client, DB_ID)
    print("✅ Token 可用\n")
    dump_schema(props, db, ds_id)

    step(2, "拉取全部记录")
    pages = query_all(client, DB_ID, ds_id)
    print(f"共 {len(pages)} 条记录\n")

    rows = []
    for p in pages:
        pr = p["properties"]
        rows.append({k: plain(pr.get(k)) for k in
                     ["标题", "编号", "状态", "栏目", "整理分类", "发布日期", "原始标签"]})

    step(3, "字段分布")
    from collections import Counter
    for field in ["状态", "栏目", "整理分类"]:
        c = Counter(str(r[field]) for r in rows)
        print(f"\n{field}:")
        for k, v in c.most_common():
            print(f"  {k:<16} {v}")

    dated = [r for r in rows if r.get("发布日期")]
    print(f"\n有发布日期的: {len(dated)} 条 / 共 {len(rows)} 条")

    step(4, "编号最大的 5 条")
    recent = sorted(rows, key=lambda r: -(r["编号"] or 0))[:5]
    for r in recent:
        print(f"  #{r['编号']} [{r['状态']}/{r['栏目']}] {r['标题'][:40]}")

    if PARENT_PAGE_ID:
        step(5, "上层页面中的数据库")
        blocks = client.blocks.children.list(block_id=PARENT_PAGE_ID, page_size=50)
        for blk in blocks["results"]:
            if blk["type"] == "child_database":
                print(f"  - {blk['child_database']['title']}  ({blk['id']})")

    print("\n✅ 验证完成")


if __name__ == "__main__":
    main()
