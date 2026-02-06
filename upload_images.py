#!/usr/bin/env python3
"""
上传图片到微信并返回 URL
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
    resp = requests.get(
        f"{BASE_URL}/token",
        params={
            "grant_type": "client_credential",
            "appid": APPID,
            "secret": APPSECRET
        }
    )
    data = resp.json()
    if "access_token" in data:
        return data["access_token"]
    else:
        raise Exception(f"获取 token 失败: {data}")


def upload_image(token, image_path):
    """上传图片到微信，返回微信图片 URL"""
    url = f"{BASE_URL}/media/uploadimg?access_token={token}"

    with open(image_path, "rb") as f:
        files = {"media": (os.path.basename(image_path), f, "image/png")}
        resp = requests.post(url, files=files)

    data = resp.json()
    if "url" in data:
        return data["url"]
    else:
        raise Exception(f"上传失败: {data}")


def main():
    token = get_access_token()
    print(f"Token: {token[:20]}...")

    images = [
        "/tmp/SCR-20260206-ptqe.png",
        "/tmp/SCR-20260206-ptfl.png",
        "/tmp/SCR-20260206-ptdz.png",
        "/tmp/SCR-20260206-ptde.png",
    ]

    urls = []
    for img in images:
        print(f"\n上传: {img}")
        url = upload_image(token, img)
        print(f"  -> {url}")
        urls.append(url)

    print("\n\n=== 所有图片 URL ===")
    for i, url in enumerate(urls):
        print(f"图片 {i+1}: {url}")


if __name__ == "__main__":
    main()
