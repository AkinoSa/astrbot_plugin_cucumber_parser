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

    # 移动端 UA（用于兼容历史接口）
    MOBILE_UA = (
        'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36'
    )
    # Feed 接口 UA
    FEED_UA = (
        'com.ss.android.ugc.aweme/100801 (Linux; U; Android 10; zh_CN; MI 9; '
        'Build/QKQ1.190825.002; Cronet/58.0.2991.0)'
    )
    # 百度蜘蛛 UA（抖音对搜索引擎爬虫强制返回完整 SSR 结构化数据，用于图文/兜底）
    SPIDER_UA = (
        'Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)'
    )

    FEED_URL = "https://api5-normal-c-lq.amemv.com/aweme/v1/feed/?aweme_id={aweme_id}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            'User-Agent': self.MOBILE_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.douyin.com/?is_from_mobile_home=1&recommend=1',
        }
        self.feed_headers = {
            'User-Agent': self.FEED_UA,
            'Accept': '*/*',
        }
        self.spider_headers = {
            'User-Agent': self.SPIDER_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        self.image_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://www.douyin.com/',
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }

    def extract_links(self, text: str) -> list[str]:
        result_links = []
        mobile_pattern = r'https?://v\.douyin\.com/[^\s]+'
        result_links.extend(re.findall(mobile_pattern, text))
        web_pattern = r'https?://(?:www\.)?douyin\.com/[^\s]*?(\d{18,20})[^\s]*'
        for match in re.finditer(web_pattern, text):
            result_links.append(match.group(0).rstrip('.,;，。；、）)】]'))
        return result_links

    async def get_redirected_url(self, session: aiohttp.ClientSession, url: str) -> str:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
        }
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            return str(response.url)

    @staticmethod
    def _parse_count_str(val_str: str) -> int:
        if not val_str:
            return 0
        val_str = str(val_str).strip()
        try:
            if val_str.endswith('亿'):
                return int(float(val_str[:-1]) * 100000000)
            if val_str.endswith('万'):
                return int(float(val_str[:-1]) * 10000)
            return int(val_str)
        except Exception:
            return 0

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

    async def _fetch_from_feed(self, session: aiohttp.ClientSession, video_id: str) -> dict | None:
        """方案1：通过移动端 Feed 接口获取无水印视频直链、高清封面与互动数据。"""
        url = self.FEED_URL.format(aweme_id=video_id)
        try:
            async with session.get(url, headers=self.feed_headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                aweme_list = data.get('aweme_list') or []
                item = None
                for candidate in aweme_list:
                    if str(candidate.get('aweme_id')) == str(video_id):
                        item = candidate
                        break
                if not item:
                    return None

                title = item.get('desc', '')
                nickname = item.get('author', {}).get('nickname', '')
                create_time = item.get('create_time')
                publish_time = (
                    datetime.fromtimestamp(create_time).strftime('%Y-%m-%d') if create_time else ''
                )
                statistics = item.get('statistics') or {}
                raw_images = item.get('images') or []
                images = [
                    img['url_list']
                    for img in raw_images
                    if isinstance(img, dict) and img.get('url_list')
                ]
                is_gallery = len(images) > 0

                video = item.get('video') or {}
                thumb_url = ''
                try:
                    thumb_url = video['cover']['url_list'][0]
                except (KeyError, IndexError, TypeError):
                    pass

                video_url = ''
                if not is_gallery:
                    play_addr = video.get('play_addr') or {}
                    url_list = play_addr.get('url_list') or []
                    for u in url_list:
                        if u and ('douyinvod.com' in u or '365yg.com' in u or 'aweme/v1/play' in u):
                            video_url = u
                            break
                    if not video_url and url_list:
                        video_url = url_list[0]

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
        except Exception as e:
            logger.debug(f'[抖音] Feed 接口请求失败：{e}')
            return None

    async def _fetch_from_router_data(self, session: aiohttp.ClientSession, video_id: str) -> dict | None:
        """方案2：通过作品分享页 _ROUTER_DATA 提取信息（兼容历史网页端）。"""
        url = f'https://www.iesdouyin.com/share/video/{video_id}/'
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status != 200:
                    return None
                response_text = await response.text()
                json_str = self.extract_router_data(response_text)
                if not json_str:
                    return None
                json_str = json_str.replace('\\u002F', '/').replace('\\/', '/')
                try:
                    json_data = json.loads(json_str)
                except Exception:
                    return None
                loader_data = json_data.get('loaderData', {})
                video_info = None
                for v in loader_data.values():
                    if isinstance(v, dict) and 'videoInfoRes' in v:
                        video_info = v['videoInfoRes']
                        break
                if not video_info or 'item_list' not in video_info or not video_info['item_list']:
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
                elif play_uri:
                    video_url = f'https://www.douyin.com/aweme/v1/play/?video_id={play_uri}'
                else:
                    video_url = ''

                images = [
                    img['url_list']
                    for img in (item_list.get('images') or [])
                    if isinstance(img, dict) and img.get('url_list')
                ]
                statistics = item_list.get('statistics') or {}
                is_gallery = len(images) > 0
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
        except Exception:
            return None

    async def _fetch_from_spider(self, session: aiohttp.ClientSession, video_id: str, is_note: bool = False) -> dict | None:
        """方案3：通过百度爬虫 UA 请求获取 SSR 服务端渲染的 ld+json 结构化数据（支持图文/兜底）。"""
        urls_to_try = [
            f"https://www.douyin.com/note/{video_id}" if is_note else f"https://www.douyin.com/video/{video_id}",
            f"https://www.iesdouyin.com/share/video/{video_id}/",
        ]
        for page_url in urls_to_try:
            try:
                async with session.get(page_url, headers=self.spider_headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                    ld_matches = re.findall(
                        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        html,
                        re.DOTALL
                    )
                    for block in ld_matches:
                        try:
                            data = json.loads(block)
                        except Exception:
                            continue
                        tp = data.get('@type')
                        if tp in ('article', 'NewsArticle'):
                            # 图文/图集
                            title = data.get('headline') or data.get('name') or data.get('articleBody') or ''
                            author = data.get('author', {}).get('name', '')
                            date_str = data.get('datePublished', '')
                            publish_time = date_str[:10] if date_str else ''
                            stats = {}
                            if 'interactionStatistic' in data and isinstance(data['interactionStatistic'], list):
                                for stat_item in data['interactionStatistic']:
                                    if stat_item.get('interactionType', {}).get('@type') == 'https://schema.org/LikeAction':
                                        stats['digg_count'] = stat_item.get('userInteractionCount')
                            if 'commentCount' in data:
                                stats['comment_count'] = data.get('commentCount')
                            if 'collectCount' in data:
                                stats['collect_count'] = data.get('collectCount')
                            if 'repostCount' in data:
                                stats['share_count'] = data.get('repostCount')

                            img_list = data.get('image') or []
                            images = [[u] for u in img_list if u]
                            thumb_url = images[0][0] if images else ''
                            return {
                                'platform': 'douyin',
                                'title': title.strip(),
                                'author': author,
                                'publish_time': publish_time,
                                'statistics': stats,
                                'clean_url': f"https://www.douyin.com/note/{video_id}",
                                'video_url': '',
                                'thumb_url': thumb_url,
                                'images': images,
                                'is_gallery': True,
                            }
                        elif tp == 'VideoObject':
                            title = data.get('name', '')
                            if title.endswith(' - 抖音'):
                                title = title[:-5]
                            author = data.get('creator', {}).get('name', '')
                            upload_date = data.get('uploadDate', '')
                            publish_time = upload_date[:10] if upload_date else ''
                            stats = {}
                            if 'commentCount' in data:
                                stats['comment_count'] = data.get('commentCount')
                            desc = data.get('description', '')
                            like_match = re.search(r'已经收获了\s*([\d\.]+[万亿]?)\s*个喜欢', desc)
                            if like_match:
                                stats['digg_count'] = self._parse_count_str(like_match.group(1))
                            thumbs = data.get('thumbnailUrl') or []
                            thumb_url = thumbs[0] if thumbs else ''
                            # 如果没有视频直链，把封面放进 images 以便展示预览图
                            images = [[thumb_url]] if thumb_url else []
                            return {
                                'platform': 'douyin',
                                'title': title.strip(),
                                'author': author,
                                'publish_time': publish_time,
                                'statistics': stats,
                                'clean_url': f"https://www.douyin.com/video/{video_id}",
                                'video_url': '',
                                'thumb_url': thumb_url,
                                'images': images,
                                'is_gallery': False,
                            }
            except Exception as e:
                logger.debug(f'[抖音] 爬虫 SSR 请求异常 {page_url}：{e}')
        return None

    async def fetch(
        self, session: aiohttp.ClientSession, url: str, group_id=None
    ) -> dict | None:
        try:
            redirected_url = await self.get_redirected_url(session, url)
        except aiohttp.ClientError as e:
            logger.warning(f'[抖音] 短链跳转失败：{url} -> {e}')
            return None

        match = re.search(r'(\d{18,20})', redirected_url) or re.search(r'(\d+)', redirected_url)
        if not match:
            logger.warning(f'[抖音] 未取到作品 id：{url} -> {redirected_url}')
            return None
        video_id = match.group(1)
        is_note = '/note/' in redirected_url

        # 1. 优先调用官方 Feed 接口（可获取无水印高清视频直链与完整数据）
        res = await self._fetch_from_feed(session, video_id)
        if res:
            return res

        # 2. 回退尝试原网页 _ROUTER_DATA
        res = await self._fetch_from_router_data(session, video_id)
        if res:
            return res

        # 3. 回退尝试爬虫 SSR 解析（完美支持图文/图集 note 以及各类风控/兜底页面）
        res = await self._fetch_from_spider(session, video_id, is_note=is_note)
        if res:
            return res

        logger.warning(f'[抖音] 全部解析方案均未获取到作品信息：{video_id}')
        return None
