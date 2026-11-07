from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .parsers import ParserManager

import re


@register(
    "astrbot_plugin_cucumber_parser",
    "AkinoSa",
    "黄瓜解析器：自动识别抖音、B 站、小红书、贴吧、微博、微信、小黑盒、知乎链接并解析发送",
    "1.0.1",
)
class ResolveLitePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        general = config.get("general") or {}
        media = config.get("media") or {}

        self.is_auto_parse = general.get("is_auto_parse", True)
        self.is_auto_pack = general.get("is_auto_pack", True)
        self.enable_group = general.get("enable_group", True)
        self.enable_private = general.get("enable_private", True)
        self.node_sender_name = general.get("node_sender_name", "")
        self.enable_conversation_history = general.get("enable_conversation_history", False)
        self.conversation_history_mode = general.get("conversation_history_mode", "text_only")
        self.block_after_parse = general.get("block_after_parse", True)

        self.manager = ParserManager(
            image_max_side=int(media.get("image_max_side", 1440) or 1440),
            image_quality=int(media.get("image_quality", 85) or 85),
            platform_switches=config.get("platform_switches") or {},
            cookies=config.get("cookies") or {},
            display=config.get("display") or {},
            image_switches=media.get("image_switches") or {},
            video_switches=media.get("video_switches") or {},
            dedupe_seconds=int(general.get("dedupe_seconds", 300) or 0),
        )

    async def terminate(self):
        await self.manager.close_all()

    async def _resolve_bot_name(self, event: AstrMessageEvent):
        """尝试通过平台 API 获取机器人自己的昵称/群名片，失败返回 None。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        self_id = event.get_self_id()
        group_id = event.get_group_id()
        try:
            if group_id:
                try:
                    info = await bot.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(self_id),
                        no_cache=False,
                    )
                except Exception:
                    info = None
                if isinstance(info, dict) and (info.get("card") or info.get("nickname")):
                    return info.get("card") or info.get("nickname")
            info = await bot.call_action("get_login_info")
            if isinstance(info, dict):
                return info.get("nickname")
        except Exception as e:
            logger.debug(f"获取机器人昵称失败，将使用默认名字：{e}")
        return None

    @filter.event_message_type(EventMessageType.ALL)
    async def auto_parse(self, event: AstrMessageEvent):
        message = event.message_str or ""
        if not (self.is_auto_parse or bool(re.search(r'.?(链接解析|抖音解析|b站解析)', message, re.I))):
            return

        # 群聊 / 私聊开关
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            is_group = False
        if is_group and not self.enable_group:
            return
        if not is_group and not self.enable_private:
            return
        sender_name = self.node_sender_name or await self._resolve_bot_name(event) or "链接解析"
        nodes = await self.manager.build_messages(event, self.is_auto_pack, sender_name)
        if nodes is None:
            return
        if self.is_auto_pack:
            await event.send(event.chain_result([Nodes(nodes)]))
        else:
            for node in nodes:
                # 列表表示"一条消息"中包含多个消息段
                await event.send(event.chain_result(node if isinstance(node, list) else [node]))

        # 解析成功后：可选写入 LLM 会话，并阻断后续 LLM 回复
        if self.enable_conversation_history:
            await self._record_conversation(event, message, nodes)
        if self.block_after_parse:
            event.stop_event()

    def _history_content(self, nodes: list, include_images: bool):
        """把本次解析结果整理成可写入 LLM 会话的内容。"""
        parts: list = []

        def walk(components):
            for comp in components:
                if isinstance(comp, Node):
                    walk(comp.content)
                elif isinstance(comp, Plain):
                    parts.append({"type": "text", "text": comp.text})
                elif isinstance(comp, Image) and include_images:
                    file_value = comp.file or ''
                    if file_value.startswith("base64://"):
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{file_value[9:]}"},
                        })
                    elif file_value.startswith(("http://", "https://")):
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": file_value},
                        })

        walk(nodes)
        if not parts:
            return ""
        if include_images:
            return parts
        return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text")

    async def _record_conversation(self, event: AstrMessageEvent, source_text: str, nodes: list):
        """将解析结果写入当前 LLM 会话，供后续对话参考。"""
        try:
            manager = getattr(self.context, "conversation_manager", None)
            if manager is None:
                return
            include_images = self.conversation_history_mode == "text_and_images"
            content = self._history_content(nodes, include_images)
            if not content:
                return
            conversation_id = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if not conversation_id:
                conversation_id = await manager.new_conversation(
                    event.unified_msg_origin, platform_id=event.get_platform_id()
                )
            await manager.add_message_pair(
                conversation_id,
                {"role": "user", "content": source_text},
                {"role": "assistant", "content": f"[由链接解析插件发送]\n{content}"
                    if isinstance(content, str) else
                    [{"type": "text", "text": "[由链接解析插件发送]"}] + content},
            )
        except Exception as e:
            logger.warning(f"解析结果写入 LLM 会话失败：{type(e).__name__}: {e}")
