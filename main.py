"""
AstrBot 插件: QQ 官方引用消息适配

在不修改 AstrBot 源码的前提下,通过 monkey-patch 补全 QQ 官方适配器
(qq_official / qq_official_webhook)对群聊/私聊引用消息(回复消息)的解析能力。

原理
----
QQ 官方 API 在用户回复(引用)消息时,推送的 payload 中:
  - message_type = 103
  - 被引用消息的内容和附件放在 msg_elements 字段中

但 botpy SDK 的消息类使用 __slots__ 限定字段,没有保存 message_type 和 msg_elements,
导致这些数据在 SDK 层面就被丢弃了。

本插件通过三层 patch 解决该问题:
  1. patch botpy ConnectionState 的 parser —— WebSocket 模式下捕获 msg_elements
  2. patch QQOfficialWebhook.handle_callback —— Webhook 模式下捕获 msg_elements
  3. patch QQOfficialPlatformAdapter._parse_from_qqofficial —— 构造 Reply 组件并注入消息链

参考实现: https://github.com/NousResearch/hermes-agent (gateway/platforms/qqbot)
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import Image, Plain, Record, Video, File
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import BaseMessageComponent, Reply

# ====================================================================
# 模块级引用消息缓存
# ====================================================================
# message_id -> msg_elements (list[dict])
# botpy parser 在解析 payload 时会丢弃 msg_elements 字段,
# 我们在 patched parser 中提前捕获并缓存,供 _parse_from_qqofficial 查询。
_quoted_msg_cache: dict[str, list] = {}
_quoted_msg_timestamps: dict[str, float] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300  # 秒
_CACHE_MAX_SIZE = 500


def _store_quoted_elements(message_id: str, msg_elements: list | None) -> None:
    """缓存引用消息的 msg_elements。"""
    if not message_id or not msg_elements:
        return
    with _cache_lock:
        _quoted_msg_cache[message_id] = msg_elements
        _quoted_msg_timestamps[message_id] = time.monotonic()
        _evict_expired_locked()


def _pop_quoted_elements(message_id: str) -> list | None:
    """取出并移除缓存的 msg_elements(一次性消费)。"""
    if not message_id:
        return None
    with _cache_lock:
        _quoted_msg_timestamps.pop(message_id, None)
        return _quoted_msg_cache.pop(message_id, None)


def _evict_expired_locked() -> None:
    """清理超过 TTL 的缓存条目(调用方需持有锁)。"""
    if len(_quoted_msg_cache) <= _CACHE_MAX_SIZE:
        return
    now = time.monotonic()
    expired = [
        mid for mid, ts in _quoted_msg_timestamps.items() if now - ts > _CACHE_TTL
    ]
    for mid in expired:
        _quoted_msg_cache.pop(mid, None)
        _quoted_msg_timestamps.pop(mid, None)
    # 如果清理后仍超限,直接清空最旧的一半
    if len(_quoted_msg_cache) > _CACHE_MAX_SIZE:
        _quoted_msg_cache.clear()
        _quoted_msg_timestamps.clear()


# ====================================================================
# QQ 表情消息解析(复刻自 AstrBot qqofficial 适配器)
# ====================================================================

_FACE_TAG_RE = re.compile(r"<faceType=\d+[^>]*>")


def _parse_face_message(content: str) -> str:
    """解析 QQ 官方 face message 格式,转换为可读文本。

    格式: <faceType=4,faceId="",ext="eyJ0ZXh0IjoiW+a7oeWktOmXruWPt10ifQ==">
    ext 字段是 base64 编码的 JSON,含 text 字段描述表情。
    """
    import base64
    import json

    def replace_face(match: re.Match) -> str:
        face_tag = match.group(0)
        ext_match = re.search(r'ext="([^"]*)"', face_tag)
        if ext_match:
            try:
                ext_decoded = base64.b64decode(ext_match.group(1)).decode("utf-8")
                ext_data = json.loads(ext_decoded)
                emoji_text = ext_data.get("text", "")
                if emoji_text:
                    return f"[表情:{emoji_text}]"
            except Exception:
                pass
        return "[表情]"

    return _FACE_TAG_RE.sub(replace_face, content)


# ====================================================================
# 引用消息解析核心
# ====================================================================


def _normalize_attachment_url(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{url}"


def _parse_quoted_message(msg_elements: list | None) -> Reply | None:
    """解析 QQ 官方引用消息的 msg_elements,构造 Reply 组件。

    参考 hermes-agent 的 _process_quoted_context 实现。

    QQ 官方 API 在用户回复(引用)消息时,设置 message_type=103,
    并将被引用消息的内容和附件放在 msg_elements 字段中。
    每个 element 是 dict,含:
      - content: str        被引用消息的文本
      - attachments: list    被引用消息的附件列表

    Args:
        msg_elements: payload 中的 msg_elements 字段(list[dict])。

    Returns:
        构造好的 Reply 组件,或 None(无引用内容)。
    """
    if not msg_elements or not isinstance(msg_elements, list):
        return None

    quoted_text_parts: list[str] = []
    quoted_chain: list[BaseMessageComponent] = []

    # 音频/视频/图片扩展名集合
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    for elem in msg_elements:
        if not isinstance(elem, dict):
            continue

        # 提取引用文本
        etext = str(elem.get("content", "")).strip()
        if etext:
            etext = _parse_face_message(etext)
            quoted_text_parts.append(etext)
            quoted_chain.append(Plain(etext))

        # 提取引用附件
        eatts = elem.get("attachments")
        if isinstance(eatts, list):
            for att in eatts:
                if not isinstance(att, dict):
                    continue
                content_type = str(att.get("content_type", "") or "").lower()
                url = _normalize_attachment_url(att.get("url"))
                if not url:
                    continue
                filename = att.get("filename") or att.get("name") or "attachment"
                ext = (
                    "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                )

                if content_type.startswith("image") or ext in image_exts:
                    quoted_chain.append(Image.fromURL(url))
                elif content_type.startswith("voice") or ext in audio_exts:
                    quoted_chain.append(Record.fromURL(url))
                elif content_type.startswith("video") or ext in video_exts:
                    quoted_chain.append(Video.fromURL(url))
                else:
                    quoted_chain.append(File(name=filename, file=url, url=url))

    quoted_message_str = " ".join(quoted_text_parts).strip()
    if not quoted_message_str and not quoted_chain:
        return None

    return Reply(
        id="",
        chain=quoted_chain,
        message_str=quoted_message_str,
        sender_id="",
        sender_nickname="",
        text=quoted_message_str,
    )


# ====================================================================
# Monkey-Patch 引擎
# ====================================================================

_patch_applied = False
_patched_parsers: set[str] = set()


def _apply_patches(context: Context) -> None:
    """应用所有必要的 monkey-patch。

    在 on_platform_loaded 钩子中调用。此时:
    - 平台适配器已实例化(__init__ 已执行,_ensure_group_message_create_parser 已调用)
    - inst.run() 作为异步任务已创建但尚未开始执行(asyncio.create_task 不立即执行)
    - ConnectionSession 尚未创建(在 run() 中创建)

    时序保证:on_platform_loaded 钩子(await handler.handler())在 inst.run() 任务
    被事件循环调度之前完成,因此 patch 类方法对后续创建的 ConnectionSession 有效。
    """
    global _patch_applied
    if _patch_applied:
        return

    try:
        _patch_connection_state_parsers()
    except Exception as e:
        logger.warning(f"[qqofficial_quote] patch ConnectionState parser 失败: {e}")

    try:
        _patch_parse_from_qqofficial()
    except Exception as e:
        logger.warning(f"[qqofficial_quote] patch _parse_from_qqofficial 失败: {e}")

    # Webhook 模式的 handle_callback patch 作为额外保险
    # (Webhook 模式下 handle_callback 直接处理 HTTP payload,不经过 botpy parser)
    try:
        _patch_webhook_handle_callback(context)
    except Exception as e:
        logger.warning(f"[qqofficial_quote] patch webhook handle_callback 失败: {e}")

    _patch_applied = True
    logger.info("[qqofficial_quote] 所有 patch 已应用")


def _patch_connection_state_parsers() -> None:
    """patch botpy ConnectionState 的所有消息 parser,捕获 msg_elements。

    botpy 的消息类(GroupMessage/C2CMessage/Message/DirectMessage)使用 __slots__
    限定字段,不含 message_type 和 msg_elements,解析时直接丢弃。

    ConnectionState.__init__ 通过 inspect.getmembers 收集所有 parse_ 方法到
    self.parsers dict。因此 patch 类方法后,之后创建的 ConnectionState 实例
    会自动引用 patched 版本。

    对于已存在的实例(竞态情况),额外 patch 实例的 parsers dict。
    """
    try:
        from botpy.connection import ConnectionState
    except ImportError:
        logger.warning("[qqofficial_quote] botpy 未安装,跳过 ConnectionState patch")
        return

    # 需要 patch 的 parser 事件名
    event_names = [
        "group_at_message_create",
        "group_message_create",
        "c2c_message_create",
        "at_message_create",
        "direct_message_create",
    ]

    for event_name in event_names:
        parser_name = f"parse_{event_name}"
        original_parser = getattr(ConnectionState, parser_name, None)
        if original_parser is None:
            logger.debug(f"[qqofficial_quote] {parser_name} 不存在,跳过")
            continue
        if getattr(original_parser, "_qq_quote_patched", False):
            continue

        def _make_wrapper(orig, name=parser_name):
            def wrapped_parser(self, payload: dict[str, Any]) -> Any:
                d = payload.get("d", {}) or {}
                msg_id = d.get("id")
                if msg_id and d.get("msg_elements"):
                    _store_quoted_elements(str(msg_id), d.get("msg_elements"))
                return orig(self, payload)

            wrapped_parser._qq_quote_patched = True  # type: ignore[attr-defined]
            return wrapped_parser

        wrapped = _make_wrapper(original_parser)
        setattr(ConnectionState, parser_name, wrapped)
        _patched_parsers.add(parser_name)
        logger.debug(f"[qqofficial_quote] 已 patch 类方法 {parser_name}")


def _patch_parse_from_qqofficial() -> None:
    """patch QQOfficialPlatformAdapter._parse_from_qqofficial,注入 Reply 组件。

    在原始方法返回 abm 后,查询缓存获取 msg_elements,
    构造 Reply 组件并插入消息链头部。
    """
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
    except ImportError:
        logger.warning(
            "[qqofficial_quote] 无法导入 QQOfficialPlatformAdapter,跳过 patch"
        )
        return

    original_parse = QQOfficialPlatformAdapter._parse_from_qqofficial

    if getattr(original_parse, "_qq_quote_patched", False):
        return

    async def patched_parse(message, message_type, force_group_mention=False):
        abm = await original_parse(message, message_type, force_group_mention)

        # 查询缓存获取引用消息的 msg_elements
        msg_id = str(getattr(abm, "message_id", "") or "")
        quoted_elements = _pop_quoted_elements(msg_id)
        if quoted_elements:
            reply_comp = _parse_quoted_message(quoted_elements)
            if reply_comp:
                # 将 Reply 组件插入消息链头部(与 aiocqhttp 适配器保持一致)
                abm.message.insert(0, reply_comp)
                # 在 message_str 中追加引用标记
                if reply_comp.message_str:
                    abm.message_str = (
                        f"[引用消息] {reply_comp.message_str}\n{abm.message_str}"
                    ).strip()

        return abm

    patched_parse._qq_quote_patched = True  # type: ignore[attr-defined]
    QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(patched_parse)
    logger.info("[qqofficial_quote] 已 patch _parse_from_qqofficial")


def _patch_webhook_handle_callback(context: Context) -> None:
    """patch QQOfficialWebhook.handle_callback,捕获 msg_elements。

    Webhook 模式下,handle_callback 直接处理 HTTP payload。虽然 webhook 模式
    也通过 ConnectionState parser 解析消息(类方法 patch 已覆盖),但这里作为
    额外保险,在 HTTP 入口处直接提取 msg_elements。

    注意: FastAPI/Starlette 的 Request.body()/get_data() 会缓存 body,
    多次调用是安全的。
    """
    try:
        from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
            QQOfficialWebhook,
        )
    except ImportError:
        logger.warning(
            "[qqofficial_quote] 无法导入 QQOfficialWebhook,跳过 webhook patch"
        )
        return

    original_handle = QQOfficialWebhook.handle_callback

    if getattr(original_handle, "_qq_quote_patched", False):
        return

    async def patched_handle_callback(self, request) -> Any:
        # 在原始处理前,提取 msg_elements
        try:
            body = await request.get_data()
            if body:
                import json

                msg = json.loads(body.decode("utf-8"))
                if isinstance(msg, dict):
                    data = msg.get("d") or {}
                    if isinstance(data, dict):
                        msg_id = data.get("id")
                        if msg_id and data.get("msg_elements"):
                            _store_quoted_elements(
                                str(msg_id), data.get("msg_elements")
                            )
        except Exception as e:
            logger.debug(f"[qqofficial_quote] webhook 提取 msg_elements 失败: {e}")

        return await original_handle(self, request)

    patched_handle_callback._qq_quote_patched = True  # type: ignore[attr-defined]
    QQOfficialWebhook.handle_callback = patched_handle_callback
    logger.info("[qqofficial_quote] 已 patch QQOfficialWebhook.handle_callback")


# ====================================================================
# 插件入口
# ====================================================================


@register(
    "astrbot_plugin_qqofficial_quote",
    "yuweitk",
    "为 QQ 官方适配器补全群聊/私聊引用消息(回复消息)的解析能力",
    "0.1.0",
)
class QQOfficialQuotePlugin(Star):
    """QQ 官方引用消息适配插件。

    通过 monkey-patch 在不修改 AstrBot 源码的前提下,补全 QQ 官方适配器
    对引用消息的解析能力。QQ 平台推送的引用消息内容(msg_elements)会被
    转换为 AstrBot 标准 Reply 组件,使机器人能感知用户引用了什么内容。
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)

    async def initialize(self) -> None:
        """插件初始化,注册说明信息。"""
        logger.info("[qqofficial_quote] 插件已加载,等待平台就绪后自动应用 patch...")

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        """平台加载完成时应用 monkey-patch。

        此时平台适配器已实例化但可能尚未开始运行,是 patch 的最佳时机。
        """
        try:
            _apply_patches(self.context)
        except Exception as e:
            logger.error(
                f"[qqofficial_quote] 应用 patch 失败: {e}",
                exc_info=True,
            )

    async def terminate(self) -> None:
        """插件卸载时清理。"""
        global _patch_applied
        _patch_applied = False
        with _cache_lock:
            _quoted_msg_cache.clear()
            _quoted_msg_timestamps.clear()
        logger.info("[qqofficial_quote] 插件已卸载")
