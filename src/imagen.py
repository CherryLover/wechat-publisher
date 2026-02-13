"""
Google Imagen API 封装 — AI 图片生成

使用 Imagen 4 模型生成图片，支持自定义宽高比。
"""

import base64
import os

import httpx

IMAGEN_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-generate-001:predict"


class ImagenAPI:
    """Google Imagen 4 图片生成 API"""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def generate(self, prompt: str, aspect_ratio: str = "1:1") -> bytes:
        """生成图片，返回图片 bytes（PNG 格式）。

        Args:
            prompt: 图片描述
            aspect_ratio: 宽高比，可选 "1:1", "3:4", "4:3", "9:16", "16:9"

        Returns:
            图片的原始 bytes

        Raises:
            Exception: API 调用失败时抛出异常
        """
        url = f"{IMAGEN_API_URL}?key={self.api_key}"

        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": 1,
                "aspectRatio": aspect_ratio,
            },
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=60)

        if resp.status_code != 200:
            raise Exception(f"Imagen API 请求失败 ({resp.status_code}): {resp.text}")

        data = resp.json()

        if "predictions" not in data:
            raise Exception(f"Imagen API 响应异常: {data}")

        image_b64 = data["predictions"][0]["bytesBase64Encoded"]
        return base64.b64decode(image_b64)
