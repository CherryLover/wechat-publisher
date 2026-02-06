#!/usr/bin/env python3
"""
微信公众号 API 验证脚本
验证：1. 获取 token  2. 上传图片  3. 创建草稿
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

APPID = os.getenv("WX_APPID")
APPSECRET = os.getenv("WX_APPSECRET")

BASE_URL = "https://api.weixin.qq.com/cgi-bin"


def get_access_token():
    """获取 access_token"""
    print("=" * 50)
    print("步骤 1: 获取 access_token")
    print("=" * 50)

    url = f"{BASE_URL}/token"
    params = {
        "grant_type": "client_credential",
        "appid": APPID,
        "secret": APPSECRET
    }

    resp = requests.get(url, params=params)
    data = resp.json()

    if "access_token" in data:
        print(f"✅ 成功获取 access_token")
        print(f"   Token 前 20 位: {data['access_token'][:20]}...")
        print(f"   有效期: {data['expires_in']} 秒")
        return data["access_token"]
    else:
        print(f"❌ 获取失败: {data}")
        return None


def upload_image(token):
    """上传图片到微信（文章内图片）"""
    print("\n" + "=" * 50)
    print("步骤 2: 上传图片 (media/uploadimg)")
    print("=" * 50)

    # 先下载一张测试图片
    test_image_url = "https://picsum.photos/400/300"
    print(f"   下载测试图片: {test_image_url}")

    img_resp = requests.get(test_image_url, allow_redirects=True)
    if img_resp.status_code != 200:
        print(f"❌ 下载测试图片失败")
        return None

    # 保存到临时文件
    with open("/tmp/test_image.jpg", "wb") as f:
        f.write(img_resp.content)
    print(f"   图片大小: {len(img_resp.content)} bytes")

    # 上传到微信
    url = f"{BASE_URL}/media/uploadimg?access_token={token}"

    with open("/tmp/test_image.jpg", "rb") as f:
        files = {"media": ("test.jpg", f, "image/jpeg")}
        resp = requests.post(url, files=files)

    data = resp.json()

    if "url" in data:
        print(f"✅ 图片上传成功")
        print(f"   微信图片 URL: {data['url']}")
        return data["url"]
    else:
        print(f"❌ 上传失败: {data}")
        return None


def upload_thumb_image(token):
    """上传封面图（永久素材，返回 media_id）"""
    print("\n" + "=" * 50)
    print("步骤 3: 上传封面图 (material/add_material)")
    print("=" * 50)

    url = f"{BASE_URL}/material/add_material?access_token={token}&type=image"

    with open("/tmp/test_image.jpg", "rb") as f:
        files = {"media": ("thumb.jpg", f, "image/jpeg")}
        resp = requests.post(url, files=files)

    data = resp.json()

    if "media_id" in data:
        print(f"✅ 封面图上传成功")
        print(f"   media_id: {data['media_id']}")
        return data["media_id"]
    else:
        print(f"❌ 上传失败: {data}")
        return None


def create_draft(token, image_url, thumb_media_id):
    """创建草稿"""
    print("\n" + "=" * 50)
    print("步骤 4: 创建草稿 (draft/add)")
    print("=" * 50)

    # 简单的 HTML 内容，包含图片
    html_content = f"""
<p>这是一篇通过 API 创建的测试文章。</p>
<p>下面是一张测试图片：</p>
<p><img src="{image_url}" alt="测试图片" /></p>
<p>如果你能看到这篇文章，说明 API 调用成功了！🎉</p>
<p>创建时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
"""

    url = f"{BASE_URL}/draft/add?access_token={token}"

    payload = {
        "articles": [
            {
                "title": "API 测试文章",
                "author": "",
                "digest": "API测试",
                "content": html_content,
                "thumb_media_id": thumb_media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0
            }
        ]
    }

    # 使用 ensure_ascii=False 保留中文字符
    resp = requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    data = resp.json()

    if "media_id" in data:
        print(f"✅ 草稿创建成功")
        print(f"   草稿 media_id: {data['media_id']}")
        print(f"\n📝 请到微信公众平台 -> 草稿箱 查看")
        return data["media_id"]
    else:
        print(f"❌ 创建失败: {data}")
        return None


def main():
    print("\n🚀 微信公众号 API 验证开始\n")
    print(f"AppID: {APPID}")
    print(f"AppSecret: {APPSECRET[:10]}...")

    # 步骤 1: 获取 token
    token = get_access_token()
    if not token:
        print("\n❌ 验证失败：无法获取 access_token")
        return

    # 步骤 2: 上传文章内图片
    image_url = upload_image(token)
    if not image_url:
        print("\n⚠️ 图片上传失败，继续尝试创建草稿（不含图片）")
        image_url = ""

    # 步骤 3: 上传封面图
    thumb_media_id = upload_thumb_image(token)
    if not thumb_media_id:
        print("\n❌ 验证失败：封面图上传失败，无法创建草稿")
        return

    # 步骤 4: 创建草稿
    draft_id = create_draft(token, image_url, thumb_media_id)

    if draft_id:
        print("\n" + "=" * 50)
        print("✅ 全部验证通过！")
        print("=" * 50)
    else:
        print("\n❌ 草稿创建失败")


if __name__ == "__main__":
    main()
