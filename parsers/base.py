"""各平台解析器的公共基类与工具方法。

新增平台时，继承 BaseParser 并实现 extract_links / fetch 即可，
消息拼装、图片下载转码、互动数据格式化等通用逻辑都在这里复用。
"""

import asyncio
import io
import re
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import Plain, Reply

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass


class BaseParser(ABC):
    """平台解析器基类。

    子类需要：
    - 设置 platform_name
    - 实现 extract_links(text)：从消息文本中提取本平台的链接
    - 实现 fetch(session, url)：解析链接，返回统一的作品信息字典

    作品信息字典字段（均为可选，缺失的字段不会出现在文本里）：
        platform      平台标识
        title         标题
        author        作者
        publish_time  发布时间
        statistics    互动数据 dict，键见 STAT_LABELS
        clean_url     干净的原链接
        video_url     视频直链
        thumb_url     视频封面
        images        图集，元素为某张图片的全部候选链接 list
        text          预先拼好的整段文本（填写后不再走通用模板）
    """

    platform_name = "base"
    # 平台标识，用于按平台区分配置
    platform_key = ""
    # 是否把图片下载后转码发送（抖音图床需要，B 站等直链可正常显示则关闭）
    download_images = True
    # 是否显示封面 / 图片（由插件配置按平台控制）
    show_images = True
    # 是否发送视频直链（由插件配置按平台控制）
    send_video = True
    # 正文最大字符数（按行裁剪，仅在「裁剪」模式下生效）
    body_limit = 800
    # 正文显示方式：clip 裁剪 / full 完整
    body_mode = "clip"
    # 需要显示的类别，未配置的默认显示
    show_fields: dict = {}
    # 本平台域名关键词，用于从 QQ 分享卡片中筛出属于本平台的链接
    host_keys: tuple[str, ...] = ()

    # 互动数据：字典键 → 显示名称。平台没有的类别自动不显示
    STAT_LABELS = (
        ("digg_count", "点赞"),
        ("comment_count", "评论"),
        ("collect_count", "收藏"),
        ("share_count", "转发"),
    )

    def __init__(self, image_max_side: int = 1440, image_quality: int = 85) -> None:
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
        }
        self.image_headers = {
            **self.headers,
            'Accept': 'image/jpeg,image/png,image/*,*/*;q=0.8',
        }
        self.semaphore = asyncio.Semaphore(10)
        self.image_max_side = image_max_side
        self.image_quality = image_quality

    @abstractmethod
    def extract_links(self, text: str) -> list[str]:
        """从（已还原转义的）消息文本中提取本平台链接。"""

    # ---------- QQ 分享卡片 / JSON 消息支持 ----------

    URL_IN_JSON_RE = re.compile(r'https?://[^\s"\'\\<>）)】\]]+')

    @classmethod
    def clean_message_text(cls, text: str) -> str:
        """还原 CQ 码 / JSON 中被转义的内容，便于正则匹配。"""
        if not text:
            return ''
        value = str(text).replace('\\/', '/').replace('\\"', '"')
        value = (
            value.replace('&amp;', '&')
            .replace('&#44;', ',')
            .replace('&#91;', '[')
            .replace('&#93;', ']')
        )
        return value

    @staticmethod
    def strip_quote_patterns(text: str) -> str:
        """从字符串中移除常见的引用消息前缀/块。"""
        if not text:
            return ''
        val = re.sub(r"^\[引用消息\([\s\S]*?\)\]\s*", '', text)
        val = re.sub(r"^\[引用\s*>>>[\s\S]*?\]\s*", '', val)
        val = re.sub(r"\[CQ:reply,[^\]]+\]\s*", '', val)
        return val

    @classmethod
    def get_user_text(cls, event) -> str:
        """提取用户实际发送的纯文本内容，排除引用回复（Reply）组件。"""
        message_obj = getattr(event, 'message_obj', None)
        messages = getattr(message_obj, 'message', None) if message_obj else None
        if messages:
            plain_parts = []
            for comp in messages:
                if isinstance(comp, Reply) or getattr(comp, 'type', None) in ('reply', 'Reply'):
                    continue
                if isinstance(comp, Plain):
                    plain_parts.append(comp.text)
                elif hasattr(comp, 'text') and isinstance(comp.text, str):
                    plain_parts.append(comp.text)
            if plain_parts:
                return ''.join(plain_parts)
        return cls.strip_quote_patterns(getattr(event, 'message_str', None) or '')

    @classmethod
    def _collect_raw_urls(cls, value, ignore_keys: set[str] | None = None) -> list[str]:
        """递归从 dict / list / str 中收集所有 http(s) 链接（忽略引用回复与来源消息）。"""
        if ignore_keys is None:
            ignore_keys = {'reply', 'source'}
        urls: list[str] = []
        if isinstance(value, str):
            urls.extend(cls.URL_IN_JSON_RE.findall(cls.clean_message_text(value)))
        elif isinstance(value, dict):
            if value.get('type') in ('reply', 'Reply'):
                return urls
            for k, item in value.items():
                if k in ignore_keys:
                    continue
                urls.extend(cls._collect_raw_urls(item, ignore_keys=ignore_keys))
        elif isinstance(value, (list, tuple)):
            for item in value:
                urls.extend(cls._collect_raw_urls(item, ignore_keys=ignore_keys))
        return urls

    @classmethod
    def _match_host(cls, url: str) -> bool:
        if not cls.host_keys:
            return True
        try:
            host = (urlparse(url).hostname or '').lower()
        except ValueError:
            return False
        return any(host == key or host.endswith('.' + key) for key in cls.host_keys)

    def extract_links_from_event(self, event) -> list[str]:
        """从 QQ 分享卡片、小程序消息等原始内容中提取本平台链接。"""
        candidates: list[str] = []
        message_obj = getattr(event, 'message_obj', None)
        if message_obj is not None:
            candidates += self._collect_raw_urls(getattr(message_obj, 'raw_message', None))
            for comp in getattr(message_obj, 'message', None) or []:
                if isinstance(comp, Reply) or getattr(comp, 'type', None) in ('reply', 'Reply'):
                    continue
                inner = getattr(comp, 'raw', None)
                if inner is None:
                    inner = getattr(comp, 'data', None)
                candidates += self._collect_raw_urls(inner)

        user_text = self.get_user_text(event)
        if user_text:
            candidates += self._collect_raw_urls(user_text)

        result: list[str] = []
        for url in candidates:
            url = url.rstrip('.,;，。；、）)】]')
            if url and self._match_host(url):
                result.append(url)
        return list(dict.fromkeys(result))

    @abstractmethod
    async def fetch(
        self, session: aiohttp.ClientSession, url: str, group_id=None
    ) -> dict | None:
        """解析链接并返回统一的作品信息字典，失败返回 None。"""

    async def close(self):
        """释放解析器持有的资源（如长连接 session）。"""
        return None

    async def parse(
        self, session: aiohttp.ClientSession, url: str, group_id=None
    ) -> dict | None:
        async with self.semaphore:
            try:
                return await self.fetch(session, url, group_id=group_id)
            except aiohttp.ClientError as e:
                logger.warning(f"[{self.platform_name}] 解析失败 {url} -> {e}")
                return None
            except Exception as e:
                logger.warning(f"[{self.platform_name}] 解析异常 {url} -> {e}")
                return None

    async def get_redirected_url(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.head(url, allow_redirects=True) as response:
            return str(response.url)

    def normalize_image(self, data: bytes) -> bytes:
        """统一转码为 JPEG 并限制尺寸。

        部分图床返回 webp / heic 等格式时 PC 端 QQ 无法解码
        （表现为手机能看、电脑看不到），因此这里重新编码为标准 JPEG。
        """
        try:
            from PIL import Image as PILImage

            pil_img = PILImage.open(io.BytesIO(data))
            pil_img.load()
            if getattr(pil_img, "n_frames", 1) > 1:
                pil_img.seek(0)
            logger.info(f"[{self.platform_name}] 图片原始格式：{pil_img.format}，尺寸：{pil_img.size}")
            pil_img = pil_img.convert("RGB")
            width, height = pil_img.size
            scale = max(width, height) / self.image_max_side
            if scale > 1:
                pil_img = pil_img.resize((int(width / scale), int(height / scale)))
            buffer = io.BytesIO()
            pil_img.save(buffer, format="JPEG", quality=self.image_quality, optimize=True)
            return buffer.getvalue()
        except ImportError:
            logger.error("缺少 Pillow，无法转码图片，请安装：pip install Pillow pillow-heif")
            return data
        except Exception as e:
            logger.warning(f"图片转码失败，将使用原始数据发送：{e}")
            return data

    async def fetch_image_bytes(
        self, session: aiohttp.ClientSession, url_candidates: list[str]
    ) -> bytes | None:
        """下载图片字节，逐个尝试候选链接。"""
        for image_url in url_candidates:
            try:
                async with session.get(
                    image_url,
                    headers=self.image_headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            content_type = response.headers.get('Content-Type', 'unknown')
                            logger.info(f"图片下载成功：{len(data)} 字节，类型 {content_type}")
                            return self.normalize_image(data)
                    else:
                        logger.warning(f"图片下载失败，状态码 {response.status}：{image_url}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"图片下载异常：{image_url} -> {e}")
        return None

    @staticmethod
    def format_count(value) -> str:
        """互动数据格式化：超过一万显示 x.xx万，超过一亿显示 x.xx亿。"""
        try:
            num = int(value)
        except (TypeError, ValueError):
            return "0"
        if num >= 100000000:
            return f"{num / 100000000:.2f}亿"
        if num >= 10000:
            return f"{num / 10000:.2f}万"
        return str(num)

    @staticmethod
    def strip_html(text: str) -> str:
        """去掉 HTML 标签与常见转义，压缩空白。"""
        if not text:
            return ""
        value = re.sub(r"<br\s*/?>", "\n", str(text))
        value = re.sub(r"</(p|div|li|section|blockquote)>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", "", value)
        value = (
            value.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
        )
        value = re.sub(r"[ \t\u00a0]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @staticmethod
    def truncate(text: str, limit: int = 80) -> str:
        text = (text or "").strip().replace("\n", " ")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def clip_text(text: str, limit: int = 500) -> str:
        """按行裁剪长文本，保留原始换行与段落结构。"""
        lines = [line.rstrip() for line in str(text or '').splitlines()]
        kept: list[str] = []
        total = 0
        for line in lines:
            if not line:
                if kept and kept[-1] != '':
                    kept.append('')
                continue
            if total + len(line) > limit:
                kept.append('…')
                break
            kept.append(line)
            total += len(line) + 1
        while kept and kept[-1] == '':
            kept.pop()
        return "\n".join(kept)

    @staticmethod
    def timestamp_to_date(value, fmt: str = "%Y-%m-%d") -> str:
        try:
            return datetime.fromtimestamp(int(value)).strftime(fmt)
        except Exception:
            return ""

    @staticmethod
    def pick_stats(source: dict, mapping: dict) -> dict:
        """按映射从原始数据里挑出存在的互动数据，缺失的类别不会出现。"""
        stats = {}
        if not isinstance(source, dict):
            return stats
        for target_key, source_keys in mapping.items():
            if isinstance(source_keys, str):
                source_keys = (source_keys,)
            for key in source_keys:
                if key in source and source[key] is not None:
                    stats[target_key] = source[key]
                    break
        return stats

    def _field_enabled(self, key: str) -> bool:
        """类别是否显示，配置里没写的默认显示。"""
        return bool(self.show_fields.get(key, True))

    def _body_text(self, text: str) -> str:
        if self.body_mode == "full":
            return str(text or '').strip()
        return self.clip_text(text, self.body_limit)

    def build_info_text(self, result: dict) -> str:
        """生成作品简介：标题 / 作者 / 发布时间 / 互动数据 / 干净链接 / 正文。"""
        lines = []
        if result.get('title') and self._field_enabled('title'):
            lines.append(f"标题：{result['title']}")
        if result.get('author') and self._field_enabled('author'):
            lines.append(f"作者：{result['author']}")
        if result.get('publish_time') and self._field_enabled('publish_time'):
            lines.append(f"发布时间：{result['publish_time']}")

        stats = result.get('statistics') or {}
        stat_parts = [
            f"{label}：{self.format_count(stats[key])}"
            for key, label in self.STAT_LABELS
            if key in stats and stats[key] is not None
        ]
        if stat_parts and self._field_enabled('statistics'):
            lines.append(" | ".join(stat_parts))

        if result.get('clean_url') and self._field_enabled('link'):
            lines.append(f"链接：{result['clean_url']}")

        body = result.get('text_extra')
        if body and self._field_enabled('body'):
            # 正文单独成行，保留原文的换行与段落
            lines.append(f"正文：\n{self._body_text(body)}")
        return "\n".join(lines)
