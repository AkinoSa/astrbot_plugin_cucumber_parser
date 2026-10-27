"""知乎解析（问题 / 回答 / 专栏文章 / 想法）。

Cookie 选填（部分内容需要登录态）。
"""

import re

import aiohttp

from .base import BaseParser

class ZhihuParser(BaseParser):
    platform_name = "知乎"
    platform_key = "zhihu"
    # 知乎图片直链可正常显示
    download_images = False
    host_keys = ("zhihu.com", "zhuanlan.zhihu.com", "link.zhihu.com")

    ANSWER_PATTERN = r"https?://(?:www\.)?zhihu\.com/question/(\d+)/answer/(\d+)"
    QUESTION_PATTERN = r"https?://(?:www\.)?zhihu\.com/question/(\d+)"
    ARTICLE_PATTERN = r"https?://zhuanlan\.zhihu\.com/p/(\d+)"
    PIN_PATTERN = r"https?://(?:www\.)?zhihu\.com/pin/(\d+)"

    def __init__(self, *args, cookies: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://www.zhihu.com/',
            'Origin': 'https://www.zhihu.com',
        }
        if cookies:
            self.headers['Cookie'] = cookies
        self.image_headers = {**self.headers, 'Referer': 'https://www.zhihu.com/'}

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        links = []
        for pattern in (
            self.ANSWER_PATTERN,
            self.ARTICLE_PATTERN,
            self.PIN_PATTERN,
            self.QUESTION_PATTERN,
        ):
            links += [m.group(0) for m in re.finditer(pattern, text)]
        return list(dict.fromkeys(links))

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> dict | None:
        async with session.get(url, headers=self.headers) as response:
            if response.status != 200:
                return None
            try:
                return await response.json(content_type=None)
            except Exception:
                return None

    @staticmethod
    def _collect_images(*sources) -> list[list[str]]:
        images: list[list[str]] = []
        seen = set()
        for source in sources:
            if not isinstance(source, str) or not source:
                continue
            for url in re.findall(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp|gif)', source):
                url = url.replace('\\/', '/')
                if url in seen:
                    continue
                seen.add(url)
                images.append([url])
        return images

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        if re.search(self.ANSWER_PATTERN, url):
            match = re.search(self.ANSWER_PATTERN, url)
            answer_id = match.group(2)
            payload = await self._get_json(
                session, f"https://www.zhihu.com/api/v4/answers/{answer_id}"
            )
            if not payload:
                return None
            question = payload.get('question') or {}
            content = payload.get('content') or payload.get('excerpt') or ''
            return {
                'platform': 'zhihu',
                'title': str(question.get('title') or '').strip() or "知乎回答",
                'author': str(((payload.get('author') or {}).get('name')) or ''),
                'publish_time': self.timestamp_to_date(payload.get('created_time')),
                'statistics': self.pick_stats(
                    payload,
                    {
                        'digg_count': 'voteup_count',
                        'comment_count': 'comment_count',
                        'collect_count': 'favorite_count',
                    },
                ),
                'clean_url': f"https://www.zhihu.com/question/{match.group(1)}/answer/{answer_id}",
                'images': self._collect_images(content),
                'is_gallery': False,
                'text_extra': self.truncate(self.strip_html(content), 200),
            }

        if re.search(self.ARTICLE_PATTERN, url):
            article_id = re.search(self.ARTICLE_PATTERN, url).group(1)
            payload = await self._get_json(
                session, f"https://www.zhihu.com/api/v4/articles/{article_id}"
            )
            if not payload:
                return None
            content = payload.get('content') or ''
            return {
                'platform': 'zhihu',
                'title': str(payload.get('title') or '').strip() or "知乎文章",
                'author': str(((payload.get('author') or {}).get('name')) or ''),
                'publish_time': self.timestamp_to_date(payload.get('created')),
                'statistics': self.pick_stats(
                    payload,
                    {
                        'digg_count': 'voteup_count',
                        'comment_count': 'comment_count',
                        'collect_count': 'favorite_count',
                    },
                ),
                'clean_url': f"https://zhuanlan.zhihu.com/p/{article_id}",
                'images': self._collect_images(content),
                'is_gallery': False,
                'text_extra': self.truncate(self.strip_html(content), 200),
            }

        if re.search(self.PIN_PATTERN, url):
            pin_id = re.search(self.PIN_PATTERN, url).group(1)
            payload = await self._get_json(
                session, f"https://www.zhihu.com/api/v4/pins/{pin_id}"
            )
            if not payload:
                return None
            content = payload.get('content') or []
            text = ''
            if isinstance(content, list):
                text = "\n".join(
                    self.strip_html(str((item or {}).get('content') or ''))
                    for item in content
                    if isinstance(item, dict)
                )
            return {
                'platform': 'zhihu',
                'title': self.truncate(text, 60) or "知乎想法",
                'author': str(((payload.get('author') or {}).get('name')) or ''),
                'publish_time': self.timestamp_to_date(payload.get('created')),
                'statistics': self.pick_stats(
                    payload,
                    {
                        'digg_count': ('like_count', 'voteup_count'),
                        'comment_count': 'comment_count',
                    },
                ),
                'clean_url': f"https://www.zhihu.com/pin/{pin_id}",
                'images': self._collect_images(text),
                'is_gallery': False,
                'text_extra': self.truncate(text, 200),
            }

        match = re.search(self.QUESTION_PATTERN, url)
        if match:
            question_id = match.group(1)
            payload = await self._get_json(
                session, f"https://www.zhihu.com/api/v4/questions/{question_id}"
            )
            if not payload:
                return None
            return {
                'platform': 'zhihu',
                'title': str(payload.get('title') or '').strip() or "知乎问题",
                'author': '',
                'publish_time': self.timestamp_to_date(payload.get('created')),
                'statistics': self.pick_stats(
                    payload,
                    {
                        'comment_count': 'comment_count',
                        'share_count': 'share_count',
                    },
                ),
                'clean_url': f"https://www.zhihu.com/question/{question_id}",
                'images': [],
                'is_gallery': False,
                'text_extra': self.truncate(self.strip_html(payload.get('detail')), 200),
            }
        return None
