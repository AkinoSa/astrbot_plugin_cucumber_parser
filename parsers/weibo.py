"""微博解析。

获取微博正文、图片、视频与互动数据。
"""

import re
import time
from datetime import datetime

import aiohttp

from .base import BaseParser

class WeiboParser(BaseParser):
    platform_name = "微博"
    platform_key = "weibo"
    host_keys = ("weibo.com", "weibo.cn", "t.cn")

    STATUS_PATTERNS = (
        r"https?://(?:www\.)?weibo\.com/\d+/([0-9A-Za-z]+)",
        r"https?://m\.weibo\.cn/(?:status|detail|\d+)/([0-9A-Za-z]+)",
    )
    SHORT_RE = re.compile(r"https?://t\.cn/[A-Za-z0-9]+")
    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )

    def __init__(self, *args, cookies: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': self.MOBILE_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if cookies:
            self.headers['Cookie'] = cookies
        self.image_headers = {
            'User-Agent': self.MOBILE_UA,
            'Referer': 'https://m.weibo.cn/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        links = []
        for pattern in self.STATUS_PATTERNS:
            links += [m.group(0) for m in re.finditer(pattern, text)]
        links += [m.group(0) for m in self.SHORT_RE.finditer(text)]
        return list(dict.fromkeys(links))

    @staticmethod
    def _extract_status_id(url: str) -> str:
        for pattern in WeiboParser.STATUS_PATTERNS:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _parse_created_at(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d")
        except Exception:
            return value[:10]

    @classmethod
    def _collect_images(cls, status: dict) -> list[list[str]]:
        images: list[list[str]] = []
        for pic in status.get('pics') or []:
            if not isinstance(pic, dict):
                continue
            large = pic.get('large') or {}
            url = large.get('url') if isinstance(large, dict) else None
            url = url or pic.get('url')
            if url:
                images.append([url])
        pic_infos = status.get('pic_infos') or {}
        for info in pic_infos.values():
            if not isinstance(info, dict):
                continue
            for key in ('original', 'large', 'bmiddle'):
                url = ((info.get(key) or {}) if isinstance(info.get(key), dict) else {}).get('url')
                if url:
                    images.append([url])
                    break
        return images

    @staticmethod
    def _collect_video(status: dict) -> tuple[str, str]:
        page_info = status.get('page_info') or {}
        urls = (page_info or {}).get('urls') or {}
        for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4"):
            if urls.get(key):
                return urls[key], ((page_info.get('page_pic') or {}).get('url') or '')
        return '', ((page_info.get('page_pic') or {}).get('url') or '')

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        status_id = self._extract_status_id(url)
        if not status_id and self.SHORT_RE.search(url):
            try:
                async with session.get(url, allow_redirects=True) as response:
                    status_id = self._extract_status_id(str(response.url))
            except aiohttp.ClientError:
                return None
        if not status_id:
            return None

        headers = {
            **self.headers,
            "Referer": f"https://m.weibo.cn/detail/{status_id}",
            "Origin": "https://m.weibo.cn",
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
        }
        params = {"id": status_id, "_": int(time.time() * 1000)}
        async with session.get(
            "https://m.weibo.cn/statuses/show", params=params, headers=headers
        ) as response:
            if response.status in (403, 418) or response.status >= 400:
                return None
            try:
                payload = await response.json(content_type=None)
            except Exception:
                return None

        data = (payload or {}).get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None

        user = data.get('user') or {}
        text = self.strip_html(str(data.get('text') or ''))
        video_url, thumb_url = self._collect_video(data)
        images = self._collect_images(data)
        stats = self.pick_stats(
            data,
            {
                'digg_count': 'attitudes_count',
                'comment_count': 'comments_count',
                'share_count': 'reposts_count',
            },
        )
        return {
            'platform': 'weibo',
            'title': self.truncate(text, 60) or "微博",
            'author': str(user.get('screen_name') or ''),
            'publish_time': self._parse_created_at(str(data.get('created_at') or '')),
            'statistics': stats,
            'clean_url': f"https://m.weibo.cn/detail/{status_id}",
            'video_url': video_url,
            'thumb_url': thumb_url,
            'images': images,
            'is_gallery': bool(images),
            'text_extra': text if text != self.truncate(text, 60) else '',
        }
