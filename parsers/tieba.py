"""百度贴吧帖子解析。

让服务端返回旧版页面（首帖正文为服务端渲染），再解析 HTML。
"""

import json
import re

import aiohttp
from astrbot.api import logger

from .base import BaseParser

class TiebaParser(BaseParser):
    platform_name = "贴吧"
    platform_key = "tieba"
    host_keys = ("tieba.baidu.com", "tiebac.baidu.com")

    THREAD_RE = re.compile(
        r"https?://(?:www\.|tiebac\.)?tieba\.baidu\.com/p/(\d+)(?![A-Za-z0-9])(?:[/?#][^\s]*)?"
    )

    def __init__(self, *args, cookies: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://tieba.baidu.com/',
        }
        # 关键：强制旧版页面
        cookie_parts = [p.strip() for p in (cookies or '').split(';') if p.strip()]
        if not any(p.startswith('TIEBA_NEW_PC=') for p in cookie_parts):
            cookie_parts.append('TIEBA_NEW_PC=0')
        self.headers['Cookie'] = '; '.join(cookie_parts)
        self.image_headers = {
            **self.headers,
            'Referer': 'https://tieba.baidu.com/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        return [m.group(0) for m in self.THREAD_RE.finditer(text)]

    @staticmethod
    def _extract_title(html: str) -> str:
        match = re.search(r'class="[^"]*core_title_txt[^"]*"[^>]*title="([^"]*)"', html)
        if match:
            return BaseParser.strip_html(match.group(1))
        match = re.search(r'<title>(.*?)</title>', html, re.S)
        return BaseParser.strip_html(match.group(1)) if match else ''

    @staticmethod
    def _extract_author(html: str) -> str:
        for match in re.finditer(r"data-field=(?:'|\")(\{.*?\})(?:'|\")", html, re.S):
            try:
                data = json.loads(match.group(1).replace('&quot;', '"'))
            except json.JSONDecodeError:
                continue
            author = (data.get('author') or {}) if isinstance(data, dict) else {}
            for key in ('user_name', 'user_nickname', 'name_show'):
                if author.get(key):
                    return str(author[key])
        match = re.search(r'class="[^"]*p_author_name[^"]*"[^>]*>(.*?)<', html, re.S)
        return BaseParser.strip_html(match.group(1)) if match else ''

    @staticmethod
    def _post_html(html: str) -> str:
        """截取首帖正文所在的 HTML 片段。"""
        start_match = re.search(r'class="[^"]*d_post_content[^"]*"[^>]*>', html)
        if not start_match:
            return ''
        start = start_match.end()
        end = len(html)
        for marker in (
            '</cc>',
            'class="[^"]*d_post_content[^"]*"',
            'class="[^"]*post_tail[^"]*"',
            'class="[^"]*core_reply_wrapper[^"]*"',
            'class="[^"]*j_jb_ele[^"]*"',
        ):
            found = re.search(marker, html[start:])
            if found:
                end = min(end, start + found.start())
        return html[start:end]

    @classmethod
    def _extract_body(cls, html: str) -> tuple[str, list[list[str]], str]:
        """返回（正文文本，图片候选列表，视频直链）。"""
        chunk = cls._post_html(html)
        if not chunk:
            return '', [], ''

        images: list[list[str]] = []
        seen: set[str] = set()
        for attr in re.findall(r'(?:data-original|data-src|data-url|src)="([^"]+)"', chunk):
            url = attr
            if url.startswith('//'):
                url = 'https:' + url
            elif url.startswith('/'):
                url = 'https://tieba.baidu.com' + url
            if not url.startswith('http'):
                continue
            if 'emoticon' in url or 'face' in url:
                continue
            if not re.search(r'\.(jpg|jpeg|png|gif|webp|bmp)', url, re.I):
                continue
            if url in seen:
                continue
            seen.add(url)
            images.append([url])

        video_url = ''
        for found in re.findall(r'https?://[^\s"\'<>]+', chunk.replace('\\/', '/')):
            if re.search(r'\.(mp4|m3u8|mov)', found, re.I):
                video_url = found.rstrip(',);]')
                break

        body = BaseParser.truncate(BaseParser.strip_html(chunk), 400)
        return body, images, video_url

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        match = self.THREAD_RE.search(url)
        if not match:
            return None
        thread_id = match.group(1)
        page_url = f"https://tieba.baidu.com/p/{thread_id}?see_lz=1&pn=1"

        html = ''
        for page_url in (
            f"https://tieba.baidu.com/p/{thread_id}?see_lz=1&pn=1",
            f"https://tieba.baidu.com/p/{thread_id}",
            f"https://tiebac.baidu.com/p/{thread_id}",
        ):
            try:
                async with session.get(page_url, headers=self.headers) as response:
                    if response.status != 200:
                        logger.warning(f"[贴吧] 页面请求失败 {response.status}：{page_url}")
                        continue
                    html = await response.text(errors="ignore")
            except aiohttp.ClientError as e:
                logger.warning(f"[贴吧] 页面请求异常：{page_url} -> {e}")
                continue
            if 'l_post' in html or 'd_post_content' in html or 'core_title_txt' in html:
                break
            html = ''
        if not html:
            logger.warning(f"[贴吧] 未取到帖子页面内容：{url}")
            return None

        body, images, video_url = self._extract_body(html)
        title = self._extract_title(html)
        author = self._extract_author(html)
        if not (title or body):
            logger.warning(f"[贴吧] 页面无帖子内容（可能需要登录）：{url}")
            return None
        return {
            'platform': 'tieba',
            'title': title or f"贴吧帖子 {thread_id}",
            'author': author,
            'publish_time': '',
            'statistics': {},
            'clean_url': f"https://tieba.baidu.com/p/{thread_id}",
            'video_url': video_url,
            'images': images,
            'is_gallery': bool(images),
        }
