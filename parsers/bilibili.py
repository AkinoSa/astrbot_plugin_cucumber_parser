"""B 站解析。

B 站视频、番剧、直播、专栏、动态解析。
"""

import json
import re
import urllib.parse
from collections import OrderedDict
from pathlib import PurePosixPath
from time import localtime, strftime
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
from aiohttp import ClientSession
from astrbot.api import logger

from .base import BaseParser



# ---------- 内容解析 ----------

class BoundedDict(OrderedDict):
    """OrderedDict subclass with a max size, LRU eviction."""

    def __init__(self, max_size: int = 1024, *args, **kwargs):
        self._max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max_size:
            self.popitem(last=False)


# group_id : last_vurl
analysis_stat: BoundedDict = BoundedDict(1024)

analysis_display_image = True
analysis_display_image_list = []
images_size = ""
cover_images_size = ""


def resize_image(src: str, is_cover: bool = False) -> str:
    img_type = src[-3:]
    if cover_images_size and is_cover:
        return f"{src}@{cover_images_size}.{img_type}"
    if images_size:
        return f"{src}@{images_size}.{img_type}"
    return src


async def bili_keyword(
    group_id: Optional[str], text: str, session: ClientSession
) -> Union[List[Union[List[str], str]], str]:
    try:
        # 提取url
        url, page, time_location = extract(text)

        # 获取视频详细信息
        msg, vurl = "", ""
        if "view?" in url:
            msg, vurl = await video_detail(
                url, page=page, time_location=time_location, session=session
            )
        elif "bangumi" in url:
            msg, vurl = await bangumi_detail(url, time_location, session)
        elif "xlive" in url:
            msg, vurl = await live_detail(url, session)
        elif "article" in url:
            msg, vurl = await article_detail(url, page, session)
        elif "dynamic" in url:
            msg, vurl = await dynamic_detail(url, session)

        # 避免多个机器人解析重复推送
        if group_id:
            if group_id in analysis_stat and analysis_stat[group_id] == vurl:
                return ""
            analysis_stat[group_id] = vurl
    except Exception as e:
        logger.error(f"bili_keyword Error: {e!r}", exc_info=True)
        msg = f"bili_keyword Error: {e!r}"
    return msg


async def b23_extract(text: str, session: ClientSession) -> str:
    b23 = re.compile(
        r"b23\.tv/(\w+)|(bili(22|23|33|2233)\.cn)/(\w+)", re.I
    ).search(text.replace("\\", ""))
    if not b23:
        return text

    url = f"https://{b23[0]}"
    async with session.get(url, allow_redirects=False) as resp:
        if resp.status in (301, 302, 303, 307, 308):
            return resp.headers.get("Location", text)
        # 没有重定向则尝试跟随
        return str(resp.url) if resp.status == 200 else text


def extract(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    try:
        url = ""
        # 视频分p
        page = re.compile(r"([?&]|&amp;)p=\d+").search(text)
        # 视频播放定位时间
        time = re.compile(r"([?&]|&amp;)t=\d+").search(text)
        # 主站视频 av 号
        aid = re.compile(r"av\d+", re.I).search(text)
        # 主站视频 bv 号
        bvid = re.compile(r"BV([A-Za-z0-9]{10})+", re.I).search(text)
        # 番剧视频页
        epid = re.compile(r"ep\d+", re.I).search(text)
        # 番剧剧集ssid(season_id)
        ssid = re.compile(r"ss\d+", re.I).search(text)
        # 番剧详细页
        mdid = re.compile(r"md\d+", re.I).search(text)
        # 直播间
        room_id = re.compile(
            r"live.bilibili.com/(blanc/|h5/)?(\d+)", re.I
        ).search(text)
        # 文章
        cvid = re.compile(
            r"(/read/(cv|mobile|native)(/|\?id=)?|^cv)(\d+)", re.I
        ).search(text)
        # 动态 (opus 新格式)
        opus_id = re.compile(r"bilibili.com/opus/(\d+)", re.I).search(text)
        # 动态
        dynamic_id_type2 = re.compile(
            r"(t|m).bilibili.com/(\d+)\?(.*?)(&|&amp;)type=2", re.I
        ).search(text)
        # 动态
        dynamic_id = re.compile(r"(t|m).bilibili.com/(\d+)", re.I).search(
            text
        )
        if bvid:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid[0]}"
        elif aid:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={aid[0][2:]}"
        elif epid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?ep_id={epid[0][2:]}"
        elif ssid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?season_id={ssid[0][2:]}"
        elif mdid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?media_id={mdid[0][2:]}"
        elif room_id:
            url = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={room_id[2]}"
        elif cvid:
            page = cvid[4]
            url = f"https://api.bilibili.com/x/article/viewinfo?id={page}&mobi_app=pc&from=web"
        elif opus_id:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={opus_id[1]}"
        elif dynamic_id_type2:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?rid={dynamic_id_type2[2]}&type=2"
        elif dynamic_id:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id[2]}"
        return url, page, time
    except Exception:
        return "", None, None


# 处理超过一万的数字
def handle_num(num: int) -> str:
    if num > 10000:
        return f"{num / 10000:.2f}万"
    return str(num)


async def video_detail(
    url: str, session: ClientSession, **kwargs
) -> Tuple[List[str], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"视频请求失败: HTTP {resp.status}", url
            res = (await resp.json()).get("data")
            if not res:
                return "解析到视频被删了/稿件不可见或审核中/权限不足", url
        vurl = f"https://www.bilibili.com/video/av{res['aid']}"
        title = f"标题：{res['title']}"

        has_image = False
        if analysis_display_image or "video" in analysis_display_image_list:
            has_image = True

        cover = resize_image(res["pic"]) if has_image else ""
        if page := kwargs.get("page"):
            page = page[0].replace("&amp;", "&")
            p = int(page[3:])
            if p <= len(res["pages"]):
                vurl += f"?p={p}"
        if time_location := kwargs.get("time_location"):
            time_location = time_location[0].replace("&amp;", "&")[3:]
            if page:
                vurl += f"&t={time_location}"
            else:
                vurl += f"?t={time_location}"
        # 只保留 UP 主名，去掉空间链接与重复字段
        tname = f"UP：{res['owner']['name']}\n"
        stat = (
            f"播放：{handle_num(res['stat']['view'])} | "
            f"弹幕：{handle_num(res['stat']['danmaku'])} | "
            f"收藏：{handle_num(res['stat']['favorite'])} | "
            f"点赞：{handle_num(res['stat']['like'])} | "
            f"硬币：{handle_num(res['stat']['coin'])} | "
            f"评论：{handle_num(res['stat']['reply'])}"
        )
        # 保留完整简介与原始换行，截断交由插件的「平台正文显示方式」处理
        desc = f"\n简介：{res['desc']}"
        msg = [cover, vurl, title, tname, stat, desc]
        return msg, vurl
    except Exception as e:
        logger.error(f"视频解析出错: {e!r}", exc_info=True)
        msg = f"视频解析出错--Error: {e!r}"
        return msg, None


async def bangumi_detail(
    url: str,
    time_location: Optional[re.Match],
    session: ClientSession,
) -> Tuple[List[str], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"番剧请求失败: HTTP {resp.status}", None
            res = (await resp.json()).get("result")
            if not res:
                return None, None

        has_image = False
        if analysis_display_image or "bangumi" in analysis_display_image_list:
            has_image = True

        cover = (
            resize_image(res["cover"], is_cover=True) if has_image else ""
        )
        title = f"番剧：{res['title']}\n"
        desc = f"{res['newest_ep']['desc']}\n"
        index_title = ""
        style = "".join(f"{i}," for i in res["style"])
        style = f"类型：{style[:-1]}\n"
        evaluate = f"简介：{res['evaluate']}\n"
        if "season_id" in url:
            vurl = (
                f"https://www.bilibili.com/bangumi/play/ss{res['season_id']}"
            )
        elif "media_id" in url:
            vurl = (
                f"https://www.bilibili.com/bangumi/media/md{res['media_id']}"
            )
        else:
            epid = re.compile(r"ep_id=\d+").search(url)[0][len("ep_id="):]
            for i in res["episodes"]:
                if str(i["ep_id"]) == epid:
                    index_title = f"标题：{i['index_title']}\n"
                    break
            vurl = f"https://www.bilibili.com/bangumi/play/ep{epid}"
        if time_location:
            time_location = time_location[0].replace("&amp;", "&")[3:]
            vurl += f"?t={time_location}"
        vurl = "\n" + vurl if cover else vurl
        msg = [cover, f"{vurl}\n", title, index_title, desc, style, evaluate]
        return msg, vurl
    except Exception as e:
        logger.error(f"番剧解析出错: {e!r}", exc_info=True)
        msg = f"番剧解析出错--Error: {e!r}"
        msg += f"\n{url}"
        return msg, None


async def live_detail(
    url: str, session: ClientSession
) -> Tuple[List[str], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"直播间请求失败: HTTP {resp.status}", None
            res = await resp.json()
            if res["code"] != 0:
                return None, None
        res = res["data"]
        uname = res["anchor_info"]["base_info"]["uname"]
        room_id = res["room_info"]["room_id"]
        title = res["room_info"]["title"]

        has_image = False
        if analysis_display_image or "live" in analysis_display_image_list:
            has_image = True

        cover = (
            resize_image(res["room_info"]["cover"], is_cover=True)
            if has_image
            else ""
        )
        live_status = res["room_info"]["live_status"]
        lock_status = res["room_info"]["lock_status"]
        parent_area_name = res["room_info"]["parent_area_name"]
        area_name = res["room_info"]["area_name"]
        online = res["room_info"]["online"]
        tags = res["room_info"]["tags"]
        watched_show = res["watched_show"]["text_large"]
        vurl = f"https://live.bilibili.com/{room_id}\n"
        if lock_status:
            lock_time = res["room_info"]["lock_time"]
            lock_time = strftime(
                "%Y-%m-%d %H:%M:%S", localtime(lock_time)
            )
            title = f"[已封禁]直播间封禁至：{lock_time}\n"
        elif live_status == 1:
            title = f"[直播中]标题：{title}\n"
        elif live_status == 2:
            title = f"[轮播中]标题：{title}\n"
        else:
            title = f"[未开播]标题：{title}\n"
        up = f"主播：{uname}  当前分区：{parent_area_name}-{area_name}\n"
        watch = (
            f"观看：{watched_show}  "
            f"直播时的人气上一次刷新值：{handle_num(online)}\n"
        )
        if tags:
            tags = f"标签：{tags}\n"
        if live_status:
            player = (
                f"独立播放器：https://www.bilibili.com/blackboard/live/"
                f"live-activity-player.html?enterTheRoom=0&cid={room_id}"
            )
        else:
            player = ""
        vurl = "\n" + vurl if cover else vurl
        msg = [cover, vurl, title, up, watch, tags, player]
        return msg, vurl
    except Exception as e:
        logger.error(f"直播间解析出错: {e!r}", exc_info=True)
        msg = f"直播间解析出错--Error: {e!r}"
        return msg, None


async def article_detail(
    url: str, cvid: str, session: ClientSession
) -> Tuple[List[Union[List[str], str]], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"专栏请求失败: HTTP {resp.status}", None
            res = (await resp.json()).get("data")
            if not res:
                return None, None

        has_image = False
        if analysis_display_image or "article" in analysis_display_image_list:
            has_image = True

        images = (
            [resize_image(i) for i in res["origin_image_urls"]]
            if has_image
            else []
        )
        vurl = f"https://www.bilibili.com/read/cv{cvid}"
        title = f"标题：{res['title']}\n"
        up = f"作者：{res['author_name']}\n"
        view = f"阅读数：{handle_num(res['stats']['view'])} "
        favorite = f"收藏数：{handle_num(res['stats']['favorite'])} "
        coin = f"硬币数：{handle_num(res['stats']['coin'])}"
        share = f"分享数：{handle_num(res['stats']['share'])} "
        like = f"点赞数：{handle_num(res['stats']['like'])} "
        dislike = f"不喜欢数：{handle_num(res['stats']['dislike'])}"
        desc = view + favorite + coin + "\n" + share + like + dislike + "\n"
        msg = [images, title, up, desc, vurl]
        return msg, vurl
    except Exception as e:
        logger.error(f"专栏解析出错: {e!r}", exc_info=True)
        msg = f"专栏解析出错--Error: {e!r}"
        return msg, None


async def dynamic_detail(
    url: str, session: ClientSession
) -> Tuple[List[Union[List[str], str]], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"动态请求失败: HTTP {resp.status}", None
            res = await resp.json()
            if res["code"] != 0:
                return None, None

        data = res.get("data")
        if not data:
            return "动态数据为空", None
        item = data.get("item")
        if not item:
            return "动态内容为空", None

        dynamic_id = item["id_str"]
        vurl = f"https://t.bilibili.com/{dynamic_id}\n"

        # 动态内容
        module_dynamic = item["modules"]["module_dynamic"]
        module_type = item["type"]

        # 文字信息
        desc = module_dynamic["desc"]
        content = desc.get("text").replace("\r", "\n").replace("\n\n", "\n")

        has_image = False
        if analysis_display_image or "dynamic" in analysis_display_image_list:
            has_image = True

        # 额外信息(会员购)
        additional_msg = []
        additional = module_dynamic.get("additional")
        if isinstance(additional, dict):
            additional_type = additional.get("type")
            if additional_type == "ADDITIONAL_TYPE_GOODS":
                items = additional.get("goods", {}).get("items", [])
                for goods_item in items:
                    additional_msg.append(
                        f"{goods_item.get('name')}"
                        f"（{goods_item.get('price')}）\n"
                    )

        # DRAW图片/ARCHIVE转发视频/null纯文字
        draws = []
        archive_cover = ""
        archive_msg = ""
        split = "\n----------------------------------------\n"
        major = module_dynamic["major"]
        if isinstance(major, dict):
            if module_type == "DYNAMIC_TYPE_DRAW":
                split = split if additional_msg else ""
                if has_image:
                    draws = [
                        resize_image(i.get("src"))
                        for i in major.get("draw", {}).get("items", [])
                    ]
                else:
                    items_len = len(
                        major.get("draw", {}).get("items", [])
                    )
                    content += f"\nPS：动态中包含{items_len}张图片"

            elif module_type == "DYNAMIC_TYPE_AV":
                archive = major.get("archive", {})
                jump_url = archive.get("jump_url", "")
                archive_cover = (
                    resize_image(archive.get("cover", ""))
                    if has_image
                    else ""
                )
                archive_msg += f"转发视频：https:{jump_url}\n"
                archive_msg += f"简介：{archive.get('desc', '')}"

        elif module_type == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            orig_id = orig.get("id_str") if orig else ""
            archive_msg += f"转发动态：https://t.bilibili.com/{orig_id}\n"
        else:
            split = ""

        msg = [
            content,
            draws,
            split,
            archive_cover,
            archive_msg,
            additional_msg,
            f"\n动态链接：{vurl}",
        ]
        return msg, vurl
    except Exception as e:
        logger.error(f"动态解析出错: {e!r}", exc_info=True)
        msg = f"动态解析出错--Error: {e!r}"
        return msg, None

# ---------- 解析器 ----------

"""B 站解析器：链接识别、小程序卡片解析与结果结构化。"""




B23_RE = re.compile(r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", re.I)
BILI_URL_RE = re.compile(
    r"https?://[^\s]*(?:bilibili\.com|b23\.tv|bili(?:22|23|33|2233)\.cn)[^\s]*",
    re.I,
)
BILI_ID_RE = re.compile(r"\b(?:av\d+|BV[A-Za-z0-9]{10})\b")
QQ_MINI_PROGRAM_RE = re.compile(r"(QQ小程序(&amp;|&#|)#93;哔哩哔哩)")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".jfif", ".webp"}

ALLOWED_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "bilivideo.com",
    "bilivideo.cn",
    "bilivideo.net",
    "hdslb.com",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bili33.cn",
)


def _is_allowed_domain(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def _is_image(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        suffix = PurePosixPath(urllib.parse.urlparse(text).path).suffix.lower()
        return suffix in IMAGE_SUFFIXES
    except Exception:
        return False


def _flatten(container):
    for item in container:
        if isinstance(item, (list, tuple)):
            yield from _flatten(item)
        else:
            yield item


class BilibiliParser(BaseParser):
    platform_name = "哔哩哔哩"
    platform_key = "bilibili"
    download_images = False

    def __init__(self, *args, display_image: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.69"
            ),
            "Referer": "https://www.bilibili.com",
        }
        global analysis_display_image
        analysis_display_image = display_image
        self._session: aiohttp.ClientSession | None = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _canonical(value: str) -> str:
        """归一成规范视频链接，避免完整链接与裸 BV/av 号被当成两条。"""
        match = re.search(r'BV[A-Za-z0-9]{10}', value)
        if match:
            return f"https://www.bilibili.com/video/{match.group(0)}"
        match = re.search(r'av(\d+)', value, re.I)
        if match:
            return f"https://www.bilibili.com/video/av{match.group(1)}"
        # 其它形态（专栏、直播、动态等）保留原链接
        return value.rstrip("，。、）)】]")

    def extract_links(self, text: str) -> list[str]:
        if not text:
            return []
        links: list[str] = []
        for match in BILI_URL_RE.finditer(text):
            links.append(self._canonical(match.group(0).rstrip("，。、）)】]")))
        for match in BILI_ID_RE.finditer(text):
            links.append(self._canonical(match.group(0)))
        return list(dict.fromkeys(link for link in links if link))

    @staticmethod
    def _find_qqdocurl(data: dict) -> str:
        meta = data.get("meta")
        if not isinstance(meta, dict):
            return ""
        for val in meta.values():
            if isinstance(val, dict):
                url = val.get("qqdocurl", "") or val.get("url", "")
                if url and _is_allowed_domain(url):
                    return url
        return ""

    @classmethod
    def _try_parse_json(cls, text: str) -> str:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return cls._find_qqdocurl(data)
        except (json.JSONDecodeError, TypeError):
            pass
        return ""

    @classmethod
    def _extract_from_raw_message(cls, raw) -> str:
        """从 QQ 小程序卡片 / CQ 码 / JSON 中提取 B 站链接。"""
        if raw is None:
            return ""
        if isinstance(raw, dict):
            url = cls._find_qqdocurl(raw)
            if url:
                return url
            if raw.get("type") == "json":
                inner = raw.get("data", {})
                if isinstance(inner, dict) and isinstance(inner.get("data"), str):
                    url = cls._try_parse_json(inner["data"])
                    if url:
                        return url
        if isinstance(raw, list):
            for seg in raw:
                if isinstance(seg, dict) and seg.get("type") == "json":
                    inner = seg.get("data", {})
                    payload = inner.get("data") if isinstance(inner, dict) else inner
                    if isinstance(payload, str):
                        url = cls._try_parse_json(payload)
                        if url:
                            return url
        if isinstance(raw, str):
            raw_str = raw.strip()
            if raw_str.startswith("{"):
                url = cls._try_parse_json(raw_str)
                if url:
                    return url
            cq_match = re.search(r'\[CQ:json,data=(.*?)\]', raw_str, re.S)
            if cq_match:
                cq_data = (
                    cq_match.group(1)
                    .replace("&amp;", "&")
                    .replace("&#44;", ",")
                    .replace("&#91;", "[")
                    .replace("&#93;", "]")
                )
                url = cls._try_parse_json(cq_data)
                if url:
                    return url
        return ""

    def extract_links_from_event(self, event) -> list[str]:
        """从消息原始内容中提取 B 站链接（QQ 小程序卡片等）。"""
        links: list[str] = []
        try:
            message_obj = getattr(event, "message_obj", None)
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict) and "reply" in raw:
                raw_filtered = {k: v for k, v in raw.items() if k != "reply"}
                url = self._extract_from_raw_message(raw_filtered)
            else:
                url = self._extract_from_raw_message(raw)

            if not url and message_obj is not None:
                for comp in getattr(message_obj, "message", None) or []:
                    if getattr(comp, "type", None) in ("reply", "Reply"):
                        continue
                    inner = getattr(comp, "raw", None) or getattr(comp, "data", None)
                    if inner:
                        url = self._extract_from_raw_message(inner)
                        if url:
                            break
            if url:
                links.append(self._canonical(url))
            else:
                user_text = self.get_user_text(event)
                if QQ_MINI_PROGRAM_RE.search(user_text):
                    # 小程序卡片但未解析出链接时，交给解析流程按标题搜索
                    links.append(user_text)
        except Exception as e:
            logger.debug(f"[哔哩哔哩] 原始消息解析失败：{e}")
        return links

    # 各类别行前缀，用于把 B 站文本重排成与其它平台一致的结构
    TITLE_PREFIXES = ('标题：', '番剧：')
    AUTHOR_PREFIXES = ('UP：', 'UP主：', '作者：', '主播：')
    STAT_PREFIXES = ('播放：', '点赞：', '阅读数：', '观看：')
    BODY_MARKER = '简介：'

    def clean_bili_text(self, text: str) -> str:
        """按配置过滤类别并重排结构：标题 → 作者 → 互动数据 → 链接 → 正文。"""
        value = str(text or '')

        # 拆出简介
        body = ''
        index = value.find(self.BODY_MARKER)
        if index != -1:
            body = value[index + len(self.BODY_MARKER):].strip('\n')
            value = value[:index]
        lines = [line.rstrip() for line in value.splitlines() if line.strip()]

        title_line = ''
        author_line = ''
        stat_line = ''
        link = ''
        rest: list[str] = []
        for line in lines:
            if line.startswith(self.TITLE_PREFIXES) and not title_line:
                title_line = line
            elif line.startswith(self.AUTHOR_PREFIXES) and not author_line:
                author_line = line
            elif line.startswith(self.STAT_PREFIXES) and not stat_line:
                stat_line = line
            elif line.startswith(('http://', 'https://')) and not link:
                link = line
            elif line.startswith('动态链接：') and not link:
                link = line.split('：', 1)[1].strip()
            else:
                rest.append(line)

        ordered: list[str] = []
        if title_line and self._field_enabled('title'):
            ordered.append(title_line)
        if author_line and self._field_enabled('author'):
            ordered.append(author_line)
        if stat_line and self._field_enabled('statistics'):
            ordered.append(stat_line)
        ordered.extend(rest)
        if link and self._field_enabled('link'):
            ordered.append(f"链接：{link}")
        if body and self._field_enabled('body'):
            rendered = self._body_text(body)
            ordered.append(f"正文：\n{rendered}" if '\n' in rendered else f"正文：{rendered}")
        return "\n".join(ordered).strip()

    @staticmethod
    def normalize_url(url: str) -> str:
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        return f"https://{url}"

    async def _fetch_play_url(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        """获取视频直链：先取 cid，再取 durl（清晰度受匿名限制）。"""
        markers = ("bangumi", "live", "read", "article", "opus", "t.bilibili")
        if any(marker in url for marker in markers):
            return ''

        bvid_match = re.search(r'BV[A-Za-z0-9]{10}', url)
        aid_match = re.search(r'av(\d+)', url, re.I)
        if bvid_match:
            id_key, id_value = 'bvid', bvid_match.group(0)
        elif aid_match:
            id_key, id_value = 'aid', aid_match.group(1)
        else:
            return ''

        headers = {'Referer': 'https://www.bilibili.com'}
        try:
            async with session.get(
                f"https://api.bilibili.com/x/web-interface/view?{id_key}={id_value}",
                headers=headers,
            ) as response:
                if response.status != 200:
                    return ''
                payload = await response.json(content_type=None)
            cid = ((payload or {}).get('data') or {}).get('cid')
            if not cid:
                return ''
            async with session.get(
                f"https://api.bilibili.com/x/player/playurl?{id_key}={id_value}"
                f"&cid={cid}&qn=16&type=mp4&platform=html5",
                headers=headers,
            ) as response:
                if response.status != 200:
                    return ''
                data = await response.json(content_type=None)
            return (((data or {}).get('data') or {}).get('durl') or [{}])[0].get('url', '')
        except Exception as e:
            logger.debug(f"[Bilibili] 获取视频直链失败：{e}")
            return ''

    async def fetch(self, session: aiohttp.ClientSession, url: str, group_id=None) -> dict | None:
        bili_session = await self.ensure_session()
        text = url
        if B23_RE.search(text):
            text = await b23_extract(text, session=bili_session)

        msg = await bili_keyword(group_id, text, session=bili_session)
        if not msg:
            return None

        if isinstance(msg, str):
            return {
                'platform': 'bilibili',
                'title': '',
                'author': '',
                'publish_time': '',
                'statistics': {},
                'clean_url': '',
                'text': msg,
                'images': [],
                'is_gallery': False,
            }

        flat = list(_flatten(msg))
        images = [[self.normalize_url(i)] for i in flat if _is_image(i)]
        # 各片段自带换行，这里按行拼接即可，不会产生空行
        segments = [str(i).strip('\n') for i in flat if i and not _is_image(i)]
        body = self.clean_bili_text("\n".join(seg for seg in segments if seg))

        result = {
            'platform': 'bilibili',
            'title': '',
            'author': '',
            'publish_time': '',
            'statistics': {},
            'clean_url': '',
            'text': body,
            'images': images,
            'is_gallery': bool(images),
        }

        # 需要发送视频时尝试取直链，封面转为视频封面
        if self.send_video:
            video_url = await self._fetch_play_url(bili_session, text)
            if video_url:
                result['video_url'] = video_url
                result['thumb_url'] = images[0][0] if images else ''
                result['images'] = []
                result['is_gallery'] = False
        return result
