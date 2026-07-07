"""
AstrBot 插件: QQ 官方引用消息适配 + QQ内置ASR语音识别 + 可配置开关

功能:
1. 引用消息(回复消息)解析 —— 将QQ平台推送的msg_elements转为Reply组件
2. QQ内置ASR语音识别 —— 将asr_refer_text免费转写文本注入消息链
3. 可配置开关 —— /qqquote enable/disable quote|asr

AstrBot 4.26.x 已内置引用消息支持(PatchedMessage + msg_elements + Reply构造)。
本插件在检测到内置支持时自动跳过 quote patch,仅保留 ASR 功能。

参考实现: https://github.com/NousResearch/hermes-agent (gateway/platforms/qqbot)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain, Record, Video, File
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import BaseMessageComponent, Reply

# ====================================================================
# 配置系统
# ====================================================================

DEFAULT_CONFIG: dict[str, bool] = {
    "enable_quote": True,
    "enable_asr": True,
}


def _get_config_path() -> str:
    """获取配置文件路径"""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config.json",
    )


def load_config() -> dict[str, bool]:
    """加载插件配置，不存在则创建默认配置"""
    path = _get_config_path()
    if not os.path.exists(path):
        _save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        result = dict(DEFAULT_CONFIG)
        result.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
        return result
    except Exception:
        return dict(DEFAULT_CONFIG)


def _save_config(cfg: dict[str, bool]) -> None:
    path = _get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _detect_builtin_quote() -> bool:
    """检测 AstrBot 是否已内置引用消息支持"""
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            _ensure_group_message_create_parser,
        )
        return callable(_ensure_group_message_create_parser)
    except (ImportError, AttributeError):
        pass
    return False


# ====================================================================
# 全局开关(由 _apply_patches 根据配置设置)
# ====================================================================

_APPLY_QUOTE = True
_APPLY_ASR = True

# ====================================================================
# 模块级缓存
# ====================================================================

_quoted_msg_cache: dict[str, list] = {}
_quoted_msg_timestamps: dict[str, float] = {}
_raw_attachments_cache: dict[str, list] = {}
_raw_attachments_timestamps: dict[str, float] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300
_CACHE_MAX_SIZE = 500

_patch_applied = False
_patched_parsers: set[str] = set()


def _store_quoted_elements(message_id: str, msg_elements: list | None) -> None:
    if not message_id or not msg_elements:
        return
    with _cache_lock:
        _quoted_msg_cache[message_id] = msg_elements
        _quoted_msg_timestamps[message_id] = time.monotonic()
        _evict_expired_locked()


def _pop_quoted_elements(message_id: str) -> list | None:
    if not message_id:
        return None
    with _cache_lock:
        _quoted_msg_timestamps.pop(message_id, None)
        return _quoted_msg_cache.pop(message_id, None)


def _store_raw_attachments(message_id: str, attachments: list | None) -> None:
    if not message_id or not attachments:
        return
    with _cache_lock:
        _raw_attachments_cache[message_id] = attachments
        _raw_attachments_timestamps[message_id] = time.monotonic()
        _evict_expired_locked()


def _pop_raw_attachments(message_id: str) -> list | None:
    if not message_id:
        return None
    with _cache_lock:
        _raw_attachments_timestamps.pop(message_id, None)
        return _raw_attachments_cache.pop(message_id, None)


def _evict_expired_locked() -> None:
    total = len(_quoted_msg_cache) + len(_raw_attachments_cache)
    if total <= _CACHE_MAX_SIZE:
        return
    now = time.monotonic()
    for cache, ts_cache in [
        (_quoted_msg_cache, _quoted_msg_timestamps),
        (_raw_attachments_cache, _raw_attachments_timestamps),
    ]:
        expired = [mid for mid, ts in ts_cache.items() if now - ts > _CACHE_TTL]
        for mid in expired:
            cache.pop(mid, None)
            ts_cache.pop(mid, None)
    total = len(_quoted_msg_cache) + len(_raw_attachments_cache)
    if total > _CACHE_MAX_SIZE:
        _quoted_msg_cache.clear()
        _quoted_msg_timestamps.clear()
        _raw_attachments_cache.clear()
        _raw_attachments_timestamps.clear()


# ====================================================================
# QQ 表情消息解析
# ====================================================================

_FACE_TAG_RE = re.compile(r"<faceType=\d+[^>]*>")


def _parse_face_message(content: str) -> str:
    """解析 QQ face message, 转换为可读文本"""
    import base64

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
    """解析QQ引用消息的msg_elements, 构造Reply组件"""
    if not msg_elements or not isinstance(msg_elements, list):
        return None

    quoted_text_parts: list[str] = []
    quoted_chain: list[BaseMessageComponent] = []
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    for elem in msg_elements:
        if not isinstance(elem, dict):
            continue
        etext = str(elem.get("content", "")).strip()
        if etext:
            etext = _parse_face_message(etext)
            quoted_text_parts.append(etext)
            quoted_chain.append(Plain(etext))
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
                ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
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
# QQ 内置 ASR 语音识别
# ====================================================================


def _extract_asr_from_attachments(raw_attachments: list | None) -> list[str]:
    """从原始附件列表中提取 QQ 内置 ASR 转写文本"""
    if not raw_attachments or not isinstance(raw_attachments, list):
        return []
    transcripts: list[str] = []
    audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"}
    audio_cts = ("voice", "audio")
    for att in raw_attachments:
        if not isinstance(att, dict):
            continue
        ct = str(att.get("content_type", "") or "").lower()
        filename = str(att.get("filename", "") or att.get("name", "") or "")
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        is_voice = ct.startswith(audio_cts) or ext in audio_exts
        if not is_voice:
            continue
        asr_text = str(att.get("asr_refer_text", "") or "").strip()
        if asr_text:
            transcripts.append(asr_text)
    return transcripts


def _extract_asr_from_msg_elements(msg_elements: list | None) -> list[str]:
    if not msg_elements or not isinstance(msg_elements, list):
        return []
    transcripts: list[str] = []
    for elem in msg_elements:
        if not isinstance(elem, dict):
            continue
        eatts = elem.get("attachments")
        if isinstance(eatts, list):
            transcripts.extend(_extract_asr_from_attachments(eatts))
    return transcripts


# ====================================================================
# Monkey-Patch 引擎
# ====================================================================


def _apply_patches(context: Context, config: dict[str, bool] | None = None) -> None:
    """应用 monkey-patch, 根据配置跳过节选功能"""
    if config is None:
        config = load_config()

    global _APPLY_QUOTE, _APPLY_ASR
    _APPLY_QUOTE = config.get("enable_quote", True) and not _detect_builtin_quote()
    _APPLY_ASR = config.get("enable_asr", True)

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

    if _APPLY_ASR or _APPLY_QUOTE:
        try:
            _patch_webhook_handle_callback(context)
        except Exception as e:
            logger.warning(f"[qqofficial_quote] patch webhook handle_callback 失败: {e}")

    _patch_applied = True
    logger.info(
        f"[qqofficial_quote] patch 已应用 "
        f"(quote={'覆盖' if _APPLY_QUOTE else '跳过'}, asr={'启用' if _APPLY_ASR else '关闭'})"
    )


def _patch_connection_state_parsers() -> None:
    """patch botpy ConnectionState 的消息 parser, 捕获 msg_elements + 原始 attachments"""
    if not _APPLY_ASR and not _APPLY_QUOTE:
        return

    try:
        from botpy.connection import ConnectionState
    except ImportError:
        logger.warning("[qqofficial_quote] botpy 未安装, 跳过 ConnectionState patch")
        return

    event_names = [
        "group_at_message_create", "group_message_create",
        "c2c_message_create", "at_message_create", "direct_message_create",
    ]

    for event_name in event_names:
        parser_name = f"parse_{event_name}"
        original_parser = getattr(ConnectionState, parser_name, None)
        if original_parser is None:
            continue
        if getattr(original_parser, "_qq_quote_patched", False):
            continue

        def _make_wrapper(orig, name=parser_name):
            def wrapped_parser(self, payload: dict[str, Any]) -> Any:
                d = payload.get("d", {}) or {}
                msg_id = d.get("id")
                if msg_id:
                    if _APPLY_QUOTE and d.get("msg_elements"):
                        _store_quoted_elements(str(msg_id), d.get("msg_elements"))
                    if _APPLY_ASR:
                        raw_atts = d.get("attachments")
                        if raw_atts:
                            _store_raw_attachments(str(msg_id), raw_atts)
                return orig(self, payload)
            wrapped_parser._qq_quote_patched = True
            return wrapped_parser

        wrapped = _make_wrapper(original_parser)
        setattr(ConnectionState, parser_name, wrapped)
        _patched_parsers.add(parser_name)


def _patch_parse_from_qqofficial() -> None:
    """patch QQOfficialPlatformAdapter._parse_from_qqofficial, 注入 Reply + ASR"""
    if not _APPLY_ASR and not _APPLY_QUOTE:
        return

    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
    except ImportError:
        logger.warning("[qqofficial_quote] 无法导入 QQOfficialPlatformAdapter, 跳过 patch")
        return

    original_parse = QQOfficialPlatformAdapter._parse_from_qqofficial
    if getattr(original_parse, "_qq_quote_patched", False):
        return

    async def patched_parse(message, message_type, force_group_mention=False):
        abm = await original_parse(message, message_type, force_group_mention)
        msg_id = str(getattr(abm, "message_id", "") or "")

        # 1. 引用消息
        if _APPLY_QUOTE:
            quoted_elements = _pop_quoted_elements(msg_id)
            if quoted_elements:
                reply_comp = _parse_quoted_message(quoted_elements)
                if reply_comp:
                    abm.message.insert(0, reply_comp)
                    if reply_comp.message_str:
                        abm.message_str = (
                            f"[引用消息] {reply_comp.message_str}\n{abm.message_str}"
                        ).strip()
                    # 被引用语音 ASR
                    if _APPLY_ASR:
                        quoted_asr = _extract_asr_from_msg_elements(quoted_elements)
                        for transcript in quoted_asr:
                            if reply_comp.chain:
                                reply_comp.chain.append(Plain(f"[语音转文字] {transcript}"))
                            if reply_comp.message_str:
                                reply_comp.message_str += f"\n[语音转文字] {transcript}"
                            else:
                                reply_comp.message_str = f"[语音转文字] {transcript}"

        # 2. 直接语音 ASR
        if _APPLY_ASR:
            raw_atts = _pop_raw_attachments(msg_id)
            if raw_atts:
                asr_transcripts = _extract_asr_from_attachments(raw_atts)
                if asr_transcripts:
                    for transcript in asr_transcripts:
                        abm.message.append(Plain(f"[语音转文字] {transcript}"))
                    asr_block = "\n".join(f"[语音转文字] {t}" for t in asr_transcripts)
                    if abm.message_str:
                        abm.message_str = f"{abm.message_str}\n{asr_block}"
                    else:
                        abm.message_str = asr_block

        return abm

    patched_parse._qq_quote_patched = True
    QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(patched_parse)
    logger.info("[qqofficial_quote] 已 patch _parse_from_qqofficial")


def _patch_webhook_handle_callback(context: Context) -> None:
    """patch QQOfficialWebhook.handle_callback, 在 HTTP 入口捕获原始 payload"""
    try:
        from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
            QQOfficialWebhook,
        )
    except ImportError:
        logger.warning("[qqofficial_quote] 无法导入 QQOfficialWebhook, 跳过 webhook patch")
        return

    original_handle = QQOfficialWebhook.handle_callback
    if getattr(original_handle, "_qq_quote_patched", False):
        return

    async def patched_handle_callback(self, request) -> Any:
        try:
            body = await request.get_data()
            if body:
                msg = json.loads(body.decode("utf-8"))
                if isinstance(msg, dict):
                    data = msg.get("d") or {}
                    if isinstance(data, dict):
                        msg_id = data.get("id")
                        if msg_id:
                            if _APPLY_QUOTE and data.get("msg_elements"):
                                _store_quoted_elements(str(msg_id), data.get("msg_elements"))
                            if _APPLY_ASR and data.get("attachments"):
                                _store_raw_attachments(str(msg_id), data.get("attachments"))
        except Exception:
            pass
        return await original_handle(self, request)

    patched_handle_callback._qq_quote_patched = True
    QQOfficialWebhook.handle_callback = patched_handle_callback
    logger.info("[qqofficial_quote] 已 patch QQOfficialWebhook.handle_callback")


# ====================================================================
# 插件入口
# ====================================================================


@register(
    "astrbot_plugin_qqofficial_quote",
    "yuweitk",
    "QQ官方引用消息+内置ASR语音识别(配置文件开关)",
    "0.3.0",
)
class QQOfficialQuotePlugin(Star):
    """QQ 官方引用消息 + 内置ASR + 可配置开关

    功能:
    1. 引用消息解析(覆盖/增强 AstrBot 内置实现)
    2. QQ内置ASR语音识别(asr_refer_text 免费转写)
    3. /qqquote config 实时开关各项功能

    注意: AstrBot 4.26.x 已内置引用消息支持,
    本插件会自动检测并跳过重复的 quote patch。
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._config: dict[str, bool] = dict(DEFAULT_CONFIG)

    async def initialize(self) -> None:
        self._config = load_config()
        builtin = _detect_builtin_quote()
        logger.info(
            f"[qqofficial_quote] v0.2.1 已加载 "
            f"(quote={'启用' if self._config['enable_quote'] else '关闭'}, "
            f"asr={'启用' if self._config['enable_asr'] else '关闭'}, "
            f"AstrBot内置quote={'是' if builtin else '否'})"
        )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        self._config = load_config()
        try:
            _apply_patches(self.context, self._config)
        except Exception as e:
            logger.error(f"[qqofficial_quote] 应用 patch 失败: {e}", exc_info=True)

    @filter.command("qqquote")
    async def cmd_qqquote(self, event: AstrMessageEvent):
        """查看配置: /qqquote"""
        builtin = _detect_builtin_quote()
        yield event.plain_result(
            f"【QQ引用+ASR 插件 v0.3.0 配置】\n\n"
            f"引用消息: {'✅ 启用' if self._config['enable_quote'] else '❌ 关闭'}"
            f"{' (AstrBot已内置,跳过patch)' if builtin else ''}\n"
            f"ASR语音:  {'✅ 启用' if self._config['enable_asr'] else '❌ 关闭'}\n\n"
            f"📁 配置文件: config.json\n"
            f"  修改 enable_quote / enable_asr 后重启 AstrBot 生效\n"
            f"  当前值: enable_quote={str(self._config['enable_quote']).lower()}, "
            f"enable_asr={str(self._config['enable_asr']).lower()}"
        )

    async def terminate(self) -> None:
        global _patch_applied
        _patch_applied = False
        with _cache_lock:
            _quoted_msg_cache.clear()
            _quoted_msg_timestamps.clear()
            _raw_attachments_cache.clear()
            _raw_attachments_timestamps.clear()
        logger.info("[qqofficial_quote] 插件已卸载")
