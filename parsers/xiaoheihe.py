"""小黑盒社区帖子解析。

api.xiaoheihe.cn/bbs/app/link/tree，请求参数需带 hkey 签名。
"""

import json
import re

import aiohttp
from astrbot.api import logger

from .base import BaseParser
from .xiaoheihe_signing import RequestSigner

class XiaoheiheParser(BaseParser):
    platform_name = "小黑盒"
    platform_key = "xiaoheihe"
    host_keys = ("xiaoheihe.cn",)
    # 正文较长，给足裁剪长度
    body_limit = 1500

    BBS_WEB_PATTERN = r"https?://(?:www\.)?xiaoheihe\.cn/app/bbs/link/([0-9a-z]+)"
    BBS_SHARE_PATTERN = (
        r"https?://api\.xiaoheihe\.cn/v3/bbs/app/api/(?:web/)?share"
        r"\?[^\s#]*\blink_id=([0-9a-z]+)[^\s#]*"
    )
    # 小黑盒图片直链可正常显示，保持原项目的 URL 方式
    download_images = False

    def __init__(self, *args, cookies: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.xiaoheihe.cn/',
            'Origin': 'https://www.xiaoheihe.cn',
        }
        if cookies:
            self.headers['Cookie'] = cookies
        self.image_headers = {**self.headers, 'Referer': 'https://www.xiaoheihe.cn/'}
        self._signer = RequestSigner()

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        links = [m.group(0) for m in re.finditer(self.BBS_WEB_PATTERN, text)]
        links += [m.group(0) for m in re.finditer(self.BBS_SHARE_PATTERN, text)]
        return list(dict.fromkeys(links))

    @staticmethod
    def _extract_link_id(url: str) -> str:
        for pattern in (
            XiaoheiheParser.BBS_WEB_PATTERN,
            XiaoheiheParser.BBS_SHARE_PATTERN,
        ):
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _parse_blocks(cls, raw_text) -> tuple[str, list[list[str]]]:
        """解析帖子正文 JSON 块，返回（纯文本，图片候选列表）。"""
        images: list[list[str]] = []
        if not isinstance(raw_text, str) or not raw_text.strip():
            return "", images
        try:
            blocks = json.loads(raw_text)
        except json.JSONDecodeError:
            return BaseParser.strip_html(raw_text), images
        if not isinstance(blocks, list):
            return BaseParser.strip_html(raw_text), images
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get('type') or '') == 'img' and block.get('url'):
                images.append([str(block['url'])])
                continue
            raw = str(block.get('text') or block.get('content') or '')
            # 文本块里可能内嵌 <img>，一并提取
            for found in re.findall(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp|gif|bmp)', raw):
                images.append([found.replace('\\/', '/')])
            fragment = BaseParser.strip_html(raw)
            if fragment:
                texts.append(fragment)
        return "\n".join(texts), images

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        link_id = self._extract_link_id(url)
        if not link_id:
            return None

        params = {
            "app": "heybox",
            "os_type": "web",
            "x_app": "heybox_website",
            "x_client_type": "web",
            "x_os_type": "Windows",
            "x_client_version": "",
            "client_type": "web",
            "web_version": "3.0",
            "version": "999.0.4",
            "link_id": link_id,
            "is_first": "1",
            "page": "1",
            "index": "1",
            "limit": "20",
            "owner_only": "0",
            **self._signer.sign_path("/bbs/app/link/tree"),
        }
        async with session.get(
            "https://api.xiaoheihe.cn/bbs/app/link/tree",
            params=params,
            headers={**self.headers, 'Referer': f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"},
        ) as response:
            if response.status != 200:
                return None
            try:
                payload = await response.json(content_type=None)
            except Exception:
                return None

        if not isinstance(payload, dict) or payload.get('status') != 'ok':
            status = payload.get('status') if isinstance(payload, dict) else 'unknown'
            logger.warning(
                f"[小黑盒] 接口返回异常（status={status}）；"
                f"若为 show_captcha，请在插件配置 → Cookies 配置 → 小黑盒 Cookies 中填写 Cookie"
            )
            return None
        root = payload.get('result') or {}
        link = root.get('link') if isinstance(root, dict) else None
        if not isinstance(link, dict):
            return None

        user = link.get('user') or {}
        body, images = self._parse_blocks(link.get('text'))
        video_url = ''
        if link.get('has_video') and link.get('video_url'):
            video_url = str(link['video_url'])
        stats = self.pick_stats(
            link,
            {
                'digg_count': ('liked_count', 'like_count', 'praise_num'),
                'comment_count': ('comment_num', 'comment_count', 'reply_num'),
                'collect_count': ('collect_num', 'fav_num'),
                'share_count': ('share_num', 'share_count'),
            },
        )
        return {
            'platform': 'xiaoheihe',
            'title': str(link.get('title') or '').strip() or "小黑盒帖子",
            'author': str((user or {}).get('username') or (user or {}).get('nickname') or ''),
            'publish_time': self.timestamp_to_date(link.get('create_time'))
            if link.get('create_time')
            else '',
            'statistics': stats,
            'clean_url': f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}",
            'video_url': video_url,
            'images': images,
            'is_gallery': bool(images),
            'text_extra': body,
        }
