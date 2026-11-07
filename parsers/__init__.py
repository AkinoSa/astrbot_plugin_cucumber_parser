"""解析器管理器：负责链接分发、解析与消息拼装。

接入新平台只需在 __init__ 里追加对应解析器实例。
"""

import asyncio
import re
import time

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import Image, Node, Plain, Video

from .base import BaseParser
from .bilibili import BilibiliParser
from .douyin import DouyinParser
from .redbook import RedbookParser
from .tieba import TiebaParser
from .wechat import WechatParser
from .weibo import WeiboParser
from .xiaoheihe import XiaoheiheParser
from .zhihu import ZhihuParser

__all__ = [
    "BaseParser",
    "ParserManager",
    "BilibiliParser",
    "DouyinParser",
    "RedbookParser",
    "TiebaParser",
    "WechatParser",
    "WeiboParser",
    "XiaoheiheParser",
    "ZhihuParser",
]


class ParserManager:
    def __init__(
        self,
        image_max_side: int = 1440,
        image_quality: int = 85,
        platform_switches: dict | None = None,
        cookies: dict | None = None,
        display: dict | None = None,
        image_switches: dict | None = None,
        video_switches: dict | None = None,
        dedupe_seconds: int = 300,
    ) -> None:
        self.dedupe_seconds = max(int(dedupe_seconds or 0), 0)
        self._recent: dict[tuple[str, str], float] = {}
        switches = platform_switches or {}
        cookie_map = cookies or {}
        display = display or {}
        image_switches = image_switches or {}
        video_switches = video_switches or {}

        def images_enabled(key: str) -> bool:
            return bool(image_switches.get(key, True))

        def video_enabled(key: str) -> bool:
            return bool(video_switches.get(key, True))
        show_fields = display.get("fields") or {}
        body_mode = display.get("body_mode") or "clip"
        body_limit = int(display.get("body_limit", 800) or 800)
        platform_body_mode = display.get("platform_body_mode") or {}

        def enabled(key: str) -> bool:
            return bool(switches.get(key, True))

        self.parsers: list[BaseParser] = []
        if enabled("douyin"):
            self.parsers.append(
                DouyinParser(image_max_side=image_max_side, image_quality=image_quality)
            )
        if enabled("bilibili"):
            self.parsers.append(
                BilibiliParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    display_image=images_enabled("bilibili"),
                )
            )
        if enabled("redbook"):
            self.parsers.append(
                RedbookParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    cookies=cookie_map.get("redbook_cookies", ""),
                )
            )
        if enabled("tieba"):
            self.parsers.append(
                TiebaParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    cookies=cookie_map.get("tieba_cookies", ""),
                )
            )
        if enabled("weibo"):
            self.parsers.append(
                WeiboParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    cookies=cookie_map.get("weibo_cookies", ""),
                )
            )
        if enabled("wechat"):
            self.parsers.append(
                WechatParser(image_max_side=image_max_side, image_quality=image_quality)
            )
        if enabled("xiaoheihe"):
            self.parsers.append(
                XiaoheiheParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    cookies=cookie_map.get("xiaoheihe_cookies", ""),
                )
            )
        if enabled("zhihu"):
            self.parsers.append(
                ZhihuParser(
                    image_max_side=image_max_side,
                    image_quality=image_quality,
                    cookies=cookie_map.get("zhihu_cookies", ""),
                )
            )

        # 统一应用文本与图片显示配置（平台未单独配置时跟随全局）
        for parser in self.parsers:
            parser.show_images = images_enabled(parser.platform_key)
            parser.send_video = video_enabled(parser.platform_key)
            parser.show_fields = show_fields
            parser.body_mode = platform_body_mode.get(parser.platform_key) or body_mode
            if parser.body_mode == "clip":
                parser.body_limit = body_limit

    async def close_all(self):
        for parser in self.parsers:
            try:
                await parser.close()
            except Exception as e:
                logger.debug(f"关闭解析器资源失败：{e}")

    def collect(self, event) -> list[tuple[BaseParser, str]]:
        """从消息中收集所有待解析的链接（含 QQ 小程序卡片）。"""
        user_text = BaseParser.get_user_text(event)
        text = BaseParser.clean_message_text(user_text)
        jobs: list[tuple[BaseParser, str]] = []
        seen: set[str] = set()

        def add(parser: BaseParser, url: str):
            if url and url not in seen:
                seen.add(url)
                jobs.append((parser, url))

        for parser in self.parsers:
            for url in parser.extract_links(text):
                add(parser, url)
            extractor = getattr(parser, "extract_links_from_event", None)
            if extractor:
                try:
                    for url in extractor(event):
                        add(parser, url)
                except Exception as e:
                    logger.debug(f"[{parser.platform_name}] 原始消息提取失败：{e}")
        return jobs

    @staticmethod
    def resolve_sender_id(event) -> int | str:
        platform = event.get_platform_name()
        sender_id = event.get_self_id()
        if platform not in ("wechatpadpro", "webchat", "gewechat"):
            try:
                sender_id = int(sender_id)
            except (TypeError, ValueError):
                sender_id = 10000
        return sender_id

    def _filter_recent(self, jobs: list[tuple[BaseParser, str]], scope: str) -> list[tuple[BaseParser, str]]:
        """按防抖秒数过滤掉刚解析过的链接。"""
        if self.dedupe_seconds <= 0:
            return jobs
        now = time.time()
        kept = []
        for parser, url in jobs:
            key = (scope, self._dedupe_key(url))
            last = self._recent.get(key)
            if last is not None and now - last < self.dedupe_seconds:
                logger.debug(
                    f"[{parser.platform_name}] 防抖：{int(self.dedupe_seconds - (now - last))} "
                    f"秒内不重复解析 {url}"
                )
                continue
            kept.append((parser, url))
        return kept

    @staticmethod
    def _dedupe_key(url: str) -> str:
        # 去掉片段与常见追踪参数，避免同一内容被当成不同链接
        cleaned = url.split('#', 1)[0].rstrip('/')
        cleaned = re.sub(r'[?&](?:xsec_token|share|from|spm|unique|st|t)=[^&]*', '', cleaned)
        return cleaned.rstrip('?&')

    @staticmethod
    async def _build_images(parser: BaseParser, session, url_candidates: list[str]):
        """构造图片组件：需要转码的平台先下载，否则直接用直链。"""
        if parser.download_images:
            data = await parser.fetch_image_bytes(session, url_candidates)
            if data:
                return Image.fromBytes(data)
        return Image.fromURL(url_candidates[0])

    async def build_messages(self, event, is_auto_pack: bool, sender_name: str) -> list | None:
        """返回待发送的消息列表。

        列表元素可以是单个消息组件，也可以是组件列表（表示"一条消息"）。
        """
        jobs = self.collect(event)
        if not jobs:
            return None

        nodes: list = []
        sender_id = self.resolve_sender_id(event)
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        scope = str(group_id or event.get_sender_id() or '')

        jobs = self._filter_recent(jobs, scope)
        if not jobs:
            return None

        # 先占位，避免同一条消息被并发重复解析（解析失败会撤销占位）
        pending_keys = [(scope, self._dedupe_key(url)) for _parser, url in jobs]
        now = time.time()
        for key in pending_keys:
            self._recent[key] = now

        async with aiohttp.ClientSession() as session:
            tasks = [parser.parse(session, url, group_id=group_id) for parser, url in jobs]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for (parser, url), result in zip(jobs, results):
                if isinstance(result, Exception):
                    logger.warning(f"[{parser.platform_name}] 解析异常：{url} -> {result}")
                    self._recent.pop((scope, self._dedupe_key(url)), None)
                    continue
                if not result:
                    logger.info(f"[{parser.platform_name}] 未解析到内容：{url}")
                    self._recent.pop((scope, self._dedupe_key(url)), None)
                    continue

                # 解析成功，保留占位时间，用于防抖
                self._recent[(scope, self._dedupe_key(url))] = time.time()

                text = result.get('text') or parser.build_info_text(result)
                image_urls_list = result.get('images') or [] if parser.show_images else []
                # 图集被关掉图片时不再发送它附带的轮播视频（抖音图集会带 15 秒无声视频）
                # 平台关闭「发送视频」时也不再发送
                video_url = ''
                if parser.send_video and not result.get('is_gallery'):
                    video_url = result.get('video_url') or ''

                if image_urls_list:
                    if is_auto_pack:
                        # 消息集合：文本与各张图片各自一个平级 Node（与视频一致，不再嵌套）
                        nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[Plain(text)])
                        )
                        for url_candidates in image_urls_list:
                            nodes.append(
                                Node(
                                    name=sender_name,
                                    uin=sender_id,
                                    content=[await self._build_images(parser, session, url_candidates)],
                                )
                            )
                    else:
                        # 普通模式：图片在上、文本在下，合并为一条消息
                        image_components = [
                            await self._build_images(parser, session, url_candidates)
                            for url_candidates in image_urls_list
                        ]
                        nodes.append(image_components + [Plain(text)])
                    continue

                if video_url:
                    thumb_url = result.get('thumb_url') or ''
                    if is_auto_pack:
                        nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[Plain(text)])
                        )
                        nodes.append(
                            Node(
                                name=sender_name,
                                uin=sender_id,
                                content=[Video.fromURL(video_url)],
                            )
                        )
                    else:
                        nodes.append(Plain(text))
                        nodes.append(
                            Video.fromURL(video_url, cover=thumb_url)
                            if thumb_url
                            else Video.fromURL(video_url)
                        )
                    continue

                # 纯文本结果（如 B 站未开启封面、知乎等）
                if is_auto_pack:
                    nodes.append(
                        Node(name=sender_name, uin=sender_id, content=[Plain(text)])
                    )
                else:
                    nodes.append(Plain(text))

        return nodes or None
