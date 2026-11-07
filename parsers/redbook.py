"""小红书笔记解析。

window.__INITIAL_STATE__，无需签名即可读取公开笔记。
"""

import json
import re
from urllib.parse import quote

import aiohttp
from astrbot.api import logger

from .base import BaseParser

class RedbookParser(BaseParser):
    platform_name = "小红书"
    platform_key = "redbook"
    host_keys = ("xiaohongshu.com", "xiaohongshu.cn", "xhslink.com", "xhslink.cn")

    NOTE_RE = re.compile(
        r"https?://(?:www\.)?xiaohongshu\.(?:com|cn)/(?:explore|discovery/item)/"
        r"([A-Za-z0-9_\-]+)(?:[?#][^\s]*)?"
    )
    SHORT_RE = re.compile(r"https?://xhslink\.(?:com|cn)/[^\s]+")
    EXPLORE_UA = (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    )

    def __init__(self, *args, cookies: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': self.EXPLORE_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.xiaohongshu.com/',
        }
        self.image_headers = {
            'User-Agent': self.EXPLORE_UA,
            'Referer': 'https://www.xiaohongshu.com/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }
        if cookies:
            self.headers['Cookie'] = cookies

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        links = [m.group(0).rstrip("，。；、）)】]") for m in self.NOTE_RE.finditer(text)]
        links += [m.group(0).rstrip("，。；、）)】]") for m in self.SHORT_RE.finditer(text)]
        return list(dict.fromkeys(link for link in links if link))

    @staticmethod
    def _extract_initial_state(html: str) -> dict | None:
        match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", html, flags=re.DOTALL
        )
        if not match:
            return None
        raw = match.group(1).strip().replace("undefined", "null")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @classmethod
    def _find_note(cls, state: dict, note_id: str) -> dict | None:
        if not isinstance(state, dict):
            return None
        note = state.get('note') or {}
        detail_map = note.get('noteDetailMap') or {}
        if isinstance(detail_map, dict):
            if note_id in detail_map:
                return (detail_map[note_id] or {}).get('note')
            # 键名可能带前缀，退而取第一个有效值
            for value in detail_map.values():
                if isinstance(value, dict) and value.get('note'):
                    return value['note']
        note_data = (state.get('noteData') or {}).get('data') or {}
        return note_data.get('noteData')

    @staticmethod
    def _collect_images(note: dict) -> list[list[str]]:
        images = []
        for item in note.get('imageList') or []:
            if not isinstance(item, dict):
                continue
            candidates = []
            for field in ("fileId", "traceId"):
                value = item.get(field)
                if value:
                    candidates.append(
                        f"https://sns-img-qc.xhscdn.com/{quote(str(value).lstrip('/'), safe='/')}"
                    )
            for field in ("urlDefault", "url", "urlPre"):
                value = item.get(field)
                if value:
                    candidates.append(re.sub(r"![^/]*$", "", str(value)))
            if candidates:
                images.append(candidates)
        return images

    @staticmethod
    def _collect_video(note: dict) -> str:
        if str(note.get('type') or '') != 'video':
            return ''
        stream = ((note.get('video') or {}).get('media') or {}).get('stream') or {}
        for codec in ("h264", "h265", "av1", "h266"):
            items = stream.get(codec) or []
            for item in items:
                if isinstance(item, dict) and item.get('masterUrl'):
                    return str(item['masterUrl'])
        return ''

    async def _resolve_note_url(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
        """返回（笔记 id，带原始 query 的页面 URL）。

        小红书校验 xsec_token 等 query 参数，丢掉后会被重定向到 404 / 验证页，
        因此优先使用原链接本身作为页面地址。
        """
        match = self.NOTE_RE.search(url)
        if match:
            return match.group(1), match.group(0)
        if self.SHORT_RE.search(url):
            try:
                async with session.get(url, allow_redirects=True) as response:
                    final_url = str(response.url)
                    redirected = self.NOTE_RE.search(final_url)
                    if redirected:
                        return redirected.group(1), redirected.group(0)
            except aiohttp.ClientError as e:
                logger.warning(f"[小红书] 短链跳转失败：{url} -> {e}")
        return "", ""

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        note_id, original_url = await self._resolve_note_url(session, url)
        if not note_id:
            return None

        query = ""
        if '?' in original_url:
            query = '?' + original_url.split('?', 1)[1]

        candidates = [
            original_url,
            f"https://www.xiaohongshu.com/explore/{note_id}{query}",
            f"https://www.xiaohongshu.com/discovery/item/{note_id}{query}",
            f"https://www.xiaohongshu.com/explore/{note_id}",
            f"https://www.xiaohongshu.com/discovery/item/{note_id}",
        ]
        note = None
        success_url = ''
        for page_url in list(dict.fromkeys(candidates)):
            try:
                async with session.get(page_url, headers=self.headers) as response:
                    if response.status != 200:
                        logger.warning(f"[小红书] 页面请求失败 {response.status}：{page_url}")
                        continue
                    page_html = await response.text(errors="ignore")
            except aiohttp.ClientError as e:
                logger.warning(f"[小红书] 页面请求异常：{page_url} -> {e}")
                continue
            if '/404' in str(response.url):
                logger.warning(f"[小红书] 页面被重定向到 404：{page_url}")
                continue
            state = self._extract_initial_state(page_html) or {}
            found = self._find_note(state, note_id)
            if found and (found.get('title') or found.get('desc') or found.get('imageList')):
                note = found
                success_url = page_url
                break
            logger.warning(f"[小红书] 页面无笔记内容：{page_url}")
        if not note:
            logger.warning(f"[小红书] 未取到笔记数据：{url}")
            return None

        user = note.get('user') or {}
        author = user.get('nickname') or user.get('nickName') or ''
        desc = self.strip_html(str(note.get('desc') or ''))
        # 不再用正文首行兜底标题，避免标题与正文重复
        title = str(note.get('title') or '').strip()
        interact = note.get('interactInfo') or {}
        stats = self.pick_stats(
            interact,
            {
                'digg_count': ('likedCount', 'liked_count'),
                'collect_count': ('collectedCount', 'collected_count'),
                'comment_count': ('commentCount', 'comment_count'),
                'share_count': ('shareCount', 'share_count'),
            },
        )
        images = self._collect_images(note)
        video_url = self._collect_video(note)
        # 视频笔记时把首图作为封面，避免被当成图集处理
        thumb_url = ''
        if video_url and images:
            thumb_url = images[0][0]
            images = []
        return {
            'platform': 'redbook',
            'title': title,
            'author': author,
            'publish_time': self.timestamp_to_date(note.get('time')) if note.get('time') else '',
            'statistics': stats,
            'clean_url': self._display_url(url, original_url, success_url, note_id),
            'video_url': video_url,
            'thumb_url': thumb_url,
            'images': images,
            'is_gallery': bool(images),
            'text_extra': desc,
        }

    @classmethod
    def _display_url(cls, raw_url: str, original_url: str, success_url: str, note_id: str) -> str:
        """给出最可能在 PC 端直接打开的链接。

        优先级：分享短链（xhslink，实测最稳）> 带 xsec_token 的长链 > 用户原链接
        > 拼接出来的 explore 地址。
        """
        for candidate in (raw_url, original_url, success_url):
            if candidate and cls.SHORT_RE.search(candidate):
                return candidate.strip()
        for candidate in (original_url, success_url):
            if candidate and 'xsec_token' in candidate:
                return candidate
        if raw_url and (cls.NOTE_RE.search(raw_url) or cls.SHORT_RE.search(raw_url)):
            return raw_url.strip()
        return success_url or original_url or f"https://www.xiaohongshu.com/explore/{note_id}"

    def extract_links_from_event(self, event) -> list[str]:
        """分享卡片里同时存在短链和长链时，优先使用短链。"""
        urls = super().extract_links_from_event(event)
        short_links = [url for url in urls if self.SHORT_RE.search(url)]
        return short_links or urls
