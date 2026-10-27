"""抖音解析实现。"""

import json
import re
from datetime import datetime

import aiohttp
from astrbot.api import logger

from .base import BaseParser

class DouyinParser(BaseParser):
    platform_name = "抖音"
    platform_key = "douyin"
    host_keys = ("douyin.com", "iesdouyin.com")

    # 抖音接口必须使用移动端 UA + 抖音 Referer，否则拿不到 _ROUTER_DATA
    MOBILE_UA = (
        'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': self.MOBILE_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.douyin.com/?is_from_mobile_home=1&recommend=1',
        }
        self.image_headers = {
            'User-Agent': self.MOBILE_UA,
            'Referer': 'https://www.douyin.com/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }

    def extract_links(self, text: str) -> list[str]:
        result_links = []
        mobile_pattern = r'https?://v\.douyin\.com/[^\s]+'
        result_links.extend(re.findall(mobile_pattern, text))
        web_pattern = r'https?://(?:www\.)?douyin\.com/[^\s]*?(\d{19})[^\s]*'
        for match in re.finditer(web_pattern, text):
            video_id = match.group(1)
            result_links.append(f"https://www.douyin.com/video/{video_id}")
        return result_links

    @staticmethod
    def extract_router_data(text: str) -> str | None:
        start_flag = 'window._ROUTER_DATA = '
        start_idx = text.find(start_flag)
        if start_idx == -1:
            return None
        brace_start = text.find('{', start_idx)
        if brace_start == -1:
            return None
        stack = []
        i = brace_start
        while i < len(text):
            if text[i] == '{':
                stack.append('{')
            elif text[i] == '}':
                stack.pop()
                if not stack:
                    return text[brace_start:i + 1]
            i += 1
        return None

    async def fetch_video_info(self, session: aiohttp.ClientSession, video_id: str) -> dict | None:
        url = f'https://www.iesdouyin.com/share/video/{video_id}/'
        try:
            async with session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    logger.warning(f'[抖音] 作品页请求失败，状态码 {response.status}：{url}')
                    return None
                response_text = await response.text()
                json_str = self.extract_router_data(response_text)
                if not json_str:
                    logger.warning(f'[抖音] 未找到 _ROUTER_DATA：{url}（{len(response_text)} 字节）')
                    return None
                json_str = json_str.replace('\\u002F', '/').replace('\\/', '/')
                try:
                    json_data = json.loads(json_str)
                except Exception as e:
                    logger.warning(f'JSON解析失败：{e}')
                    return None
                loader_data = json_data.get('loaderData', {})
                video_info = None
                for v in loader_data.values():
                    if isinstance(v, dict) and 'videoInfoRes' in v:
                        video_info = v['videoInfoRes']
                        break
                if not video_info or 'item_list' not in video_info or not video_info['item_list']:
                    logger.warning('未找到视频信息')
                    return None
                item_list = video_info['item_list'][0]

                title = item_list.get('desc', '')
                nickname = item_list.get('author', {}).get('nickname', '')
                create_time = item_list.get('create_time')
                publish_time = (
                    datetime.fromtimestamp(create_time).strftime('%Y-%m-%d') if create_time else ''
                )
                video = item_list.get('video', {})
                thumb_url = ''
                try:
                    thumb_url = video['cover']['url_list'][0]
                except (KeyError, IndexError, TypeError):
                    pass
                play_uri = ''
                try:
                    play_uri = video['play_addr']['uri']
                except (KeyError, TypeError):
                    pass
                if play_uri.endswith('.mp3') or play_uri.startswith('https://'):
                    video_url = play_uri
                else:
                    video_url = f'https://www.douyin.com/aweme/v1/play/?video_id={play_uri}'

                images = [
                    img['url_list']
                    for img in (item_list.get('images') or [])
                    if isinstance(img, dict) and img.get('url_list')
                ]
                statistics = item_list.get('statistics') or {}
                is_gallery = len(images) > 0
                # 图集用 /note/，视频用 /video/
                clean_url = (
                    f"https://www.douyin.com/note/{video_id}"
                    if is_gallery
                    else f"https://www.douyin.com/video/{video_id}"
                )
                return {
                    'platform': 'douyin',
                    'title': title,
                    'author': nickname,
                    'publish_time': publish_time,
                    'statistics': statistics,
                    'clean_url': clean_url,
                    'video_url': video_url,
                    'thumb_url': thumb_url,
                    'images': images,
                    'is_gallery': is_gallery,
                }
        except aiohttp.ClientError as e:
            logger.warning(f'请求错误：{e}')
            return None

    async def fetch(
        self, session: aiohttp.ClientSession, url: str, group_id=None
    ) -> dict | None:
        try:
            redirected_url = await self.get_redirected_url(session, url)
        except aiohttp.ClientError as e:
            logger.warning(f'[抖音] 短链跳转失败：{url} -> {e}')
            return None
        match = re.search(r'(\d+)', redirected_url)
        if not match:
            logger.warning(f'[抖音] 未取到作品 id：{url} -> {redirected_url}')
            return None
        return await self.fetch_video_info(session, match.group(1))
