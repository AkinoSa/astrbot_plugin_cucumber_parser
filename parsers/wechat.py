"""微信公众号文章解析（匿名抓取，无需 Cookie/签名）。

标题、公众号名、发布时间与封面。
"""

import re

import aiohttp

from .base import BaseParser

class WechatParser(BaseParser):
    platform_name = "微信"
    platform_key = "wechat"
    host_keys = ("mp.weixin.qq.com", "weixin.qq.com", "channels.weixin.qq.com")

    ARTICLE_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s(?:[/?][^\s<>\"']*)?")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        self.image_headers = {
            **self.headers,
            'Referer': 'https://mp.weixin.qq.com/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        return [m.group(0) for m in self.ARTICLE_RE.finditer(text)]

    @staticmethod
    def _match_first(html: str, patterns: tuple[str, ...]) -> str:
        for pattern in patterns:
            match = re.search(pattern, html, re.S)
            if match:
                value = BaseParser.strip_html(match.group(1))
                if value:
                    return value
        return ""

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        if not self.ARTICLE_RE.search(url):
            return None
        headers = {**self.headers, 'Referer': url}
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return None
            html = await response.text()

        title = self._match_first(
            html,
            (
                r'var\s+msg_title\s*=\s*["\'](.*?)["\']',
                r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"',
                r'id="activity-name"[^>]*>(.*?)<',
            ),
        )
        author = self._match_first(
            html,
            (
                r'var\s+nickname\s*=\s*["\'](.*?)["\']',
                r'id="js_name"[^>]*>(.*?)<',
                r'<meta[^>]+property="article:author"[^>]+content="([^"]*)"',
            ),
        )
        raw_time = self._match_first(html, (r'var\s+ct\s*=\s*["\'](\d+)["\']',))
        publish_time = self.timestamp_to_date(raw_time) if raw_time else ''
        cover = self._match_first(
            html,
            (
                r'var\s+msg_cdn_url\s*=\s*["\'](.*?)["\']',
                r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"',
            ),
        )
        if not title:
            return None

        return {
            'platform': 'wechat',
            'title': title,
            'author': author,
            'publish_time': publish_time,
            'statistics': {},
            'clean_url': url,
            'images': [[cover]] if cover else [],
            'is_gallery': bool(cover),
        }
