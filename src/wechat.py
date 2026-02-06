"""
微信公众号 API 封装
"""

import json
import time
import httpx
from typing import Optional

class WechatAPI:
    BASE_URL = "https://api.weixin.qq.com/cgi-bin"

    def __init__(self, appid: str, appsecret: str):
        self.appid = appid
        self.appsecret = appsecret
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    async def get_access_token(self) -> str:
        """获取 access_token，带缓存"""
        if self._access_token and time.time() < self._token_expires_at - 300:
            return self._access_token

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BASE_URL}/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self.appid,
                    "secret": self.appsecret
                }
            )
            data = resp.json()

        if "access_token" not in data:
            raise Exception(f"获取 access_token 失败: {data}")

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"]
        return self._access_token

    async def upload_image(self, image_data: bytes, filename: str = "image.jpg") -> str:
        """上传文章内图片，返回微信图片 URL"""
        token = await self.get_access_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE_URL}/media/uploadimg",
                params={"access_token": token},
                files={"media": (filename, image_data, "image/jpeg")}
            )
            data = resp.json()

        if "url" not in data:
            raise Exception(f"上传图片失败: {data}")

        return data["url"]

    async def upload_thumb(self, image_data: bytes, filename: str = "thumb.jpg") -> str:
        """上传封面图（永久素材），返回 media_id"""
        token = await self.get_access_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE_URL}/material/add_material",
                params={"access_token": token, "type": "image"},
                files={"media": (filename, image_data, "image/jpeg")}
            )
            data = resp.json()

        if "media_id" not in data:
            raise Exception(f"上传封面图失败: {data}")

        return data["media_id"]

    async def create_draft(
        self,
        title: str,
        content: str,
        thumb_media_id: str,
        author: str = "",
        digest: str = ""
    ) -> str:
        """创建草稿，返回草稿 media_id"""
        token = await self.get_access_token()

        payload = {
            "articles": [
                {
                    "title": title,
                    "author": author,
                    "digest": digest or title[:50],
                    "content": content,
                    "thumb_media_id": thumb_media_id,
                    "need_open_comment": 0,
                    "only_fans_can_comment": 0
                }
            ]
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.BASE_URL}/draft/add",
                params={"access_token": token},
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            data = resp.json()

        if "media_id" not in data:
            raise Exception(f"创建草稿失败: {data}")

        return data["media_id"]
