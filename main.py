"""
AstrBot 插件: QQ 官方引用消息适配 + QQ内置ASR语音识别

功能:
1. 引用消息(回复消息)解析 —— 将QQ平台推送的msg_elements转为Reply组件
2. QQ内置ASR语音识别 —— 将asr_refer_text免费转写文本注入消息链

配置: 通过 _conf_schema.json 定义，WebUI 管理面板直接修改后重启生效。
      也可以用 /qqquote 命令查看当前配置状态。

AstrBot 4.26.x 已内置引用消息支持,本插件自动检测并跳过重复的 quote patch。

参考实现: https://github.com/NousResearch/hermes-agent (gateway/platforms/qqbot)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain, Record, Video, File
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import BaseMessageComponent, Reply


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
# 全局开关(由 _apply_patches 设置)
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
                ct = str(att.get("content_type", "") or "").lower()
                url = _normalize_attachment_url(att.get("url"))
                if not url:
                    continue
                fn = att.get("filename") or att.get("name") or "attachment"
                ext = "." + fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                if ct.startswith("image") or ext in image_exts:
                    quoted_chain.append(Image.fromURL(url))
                elif ct.startswith("voice") or ext in audio_exts:
                    quoted_chain.append(Record.fromURL(url))
                elif ct.startswith("video") or ext in video_exts:
                    quoted_chain.append(Video.fromURL(url))
                else:
                    quoted_chain.append(File(name=fn, file=url, url=url))
    msg_str = " ".join(quoted_text_parts).strip()
    if not msg_str and not quoted_chain:
        return None
    return Reply(id="", chain=quoted_chain, message_str=msg_str,
                 sender_id="", sender_nickname="", text=msg_str)


# ====================================================================
# QQ 内置 ASR 语音识别
# ====================================================================


def _extract_asr_from_attachments(raw_attachments: list | None) -> list[str]:
    if not raw_attachments or not isinstance(raw_attachments, list):
        return []
    transcripts: list[str] = []
    audio_exts = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk"}
    audio_cts = ("voice", "audio")
    for att in raw_attachments:
        if not isinstance(att, dict):
            continue
        ct = str(att.get("content_type", "") or "").lower()
        fn = str(att.get("filename", "") or att.get("name", "") or "")
        ext = "." + fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if not (ct.startswith(audio_cts) or ext in audio_exts):
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


def _apply_patches(context: Context, config: dict | None = None) -> None:
    if config is None:
        config = {"enable_quote": True, "enable_asr": True}
    global _APPLY_QUOTE, _APPLY_ASR
    _APPLY_QUOTE = bool(config.get("enable_quote", True)) and not _detect_builtin_quote()
    _APPLY_ASR = bool(config.get("enable_asr", True))
    global _patch_applied
    if _patch_applied:
        return
    try:
        _patch_connection_state_parsers()
    except Exception as e:
        logger.warning(f"[qqofficial_quote] parser patch 失败: {e}")
    try:
        _patch_parse_from_qqofficial()
    except Exception as e:
        logger.warning(f"[qqofficial_quote] _parse_from_qqofficial patch 失败: {e}")
    if _APPLY_ASR or _APPLY_QUOTE:
        try:
            _patch_webhook_handle_callback(context)
        except Exception as e:
            logger.warning(f"[qqofficial_quote] webhook patch 失败: {e}")
    _patch_applied = True
    logger.info(
        f"[qqofficial_quote] patch 已应用 "
        f"(quote={'覆盖' if _APPLY_QUOTE else '跳过'}, asr={'启用' if _APPLY_ASR else '关闭'})"
    )


def _patch_connection_state_parsers() -> None:
    if not _APPLY_ASR and not _APPLY_QUOTE:
        return
    try:
        from botpy.connection import ConnectionState
    except ImportError:
        logger.warning("[qqofficial_quote] botpy 未安装, 跳过 parser patch")
        return
    event_names = [
        "group_at_message_create", "group_message_create",
        "c2c_message_create", "at_message_create", "direct_message_create",
    ]
    for event_name in event_names:
        parser_name = f"parse_{event_name}"
        orig = getattr(ConnectionState, parser_name, None)
        if orig is None or getattr(orig, "_qq_quote_patched", False):
            continue
        def _make_wrapper(o, n=parser_name):
            def wrapped(self, payload):
                d = payload.get("d", {}) or {}
                mid = d.get("id")
                if mid:
                    if _APPLY_QUOTE and d.get("msg_elements"):
                        _store_quoted_elements(str(mid), d.get("msg_elements"))
                    if _APPLY_ASR and d.get("attachments"):
                        _store_raw_attachments(str(mid), d.get("attachments"))
                return o(self, payload)
            wrapped._qq_quote_patched = True
            return wrapped
        setattr(ConnectionState, parser_name, _make_wrapper(orig))
        _patched_parsers.add(parser_name)


def _patch_parse_from_qqofficial() -> None:
    if not _APPLY_ASR and not _APPLY_QUOTE:
        return
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
    except ImportError:
        logger.warning("[qqofficial_quote] 无法导入 QQOfficialPlatformAdapter")
        return
    orig = QQOfficialPlatformAdapter._parse_from_qqofficial
    if getattr(orig, "_qq_quote_patched", False):
        return
    async def patched(message, msg_type, force_group_mention=False):
        abm = await orig(message, msg_type, force_group_mention)
        mid = str(getattr(abm, "message_id", "") or "")
        if _APPLY_QUOTE:
            qe = _pop_quoted_elements(mid)
            if qe:
                rc = _parse_quoted_message(qe)
                if rc:
                    abm.message.insert(0, rc)
                    if rc.message_str:
                        abm.message_str = f"[引用消息] {rc.message_str}\n{abm.message_str}"
                    if _APPLY_ASR:
                        for t in _extract_asr_from_msg_elements(qe):
                            rc.chain.append(Plain(f"[语音转文字] {t}"))
                            rc.message_str = (rc.message_str or "") + f"\n[语音转文字] {t}"
        if _APPLY_ASR:
            ra = _pop_raw_attachments(mid)
            if ra:
                ts = _extract_asr_from_attachments(ra)
                if ts:
                    for t in ts:
                        abm.message.append(Plain(f"[语音转文字] {t}"))
                    blk = "\n".join(f"[语音转文字] {t}" for t in ts)
                    abm.message_str = f"{abm.message_str}\n{blk}" if abm.message_str else blk
        return abm
    patched._qq_quote_patched = True
    QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(patched)
    logger.info("[qqofficial_quote] 已 patch _parse_from_qqofficial")


def _patch_webhook_handle_callback(context: Context) -> None:
    try:
        from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import (
            QQOfficialWebhook,
        )
    except ImportError:
        return
    orig = QQOfficialWebhook.handle_callback
    if getattr(orig, "_qq_quote_patched", False):
        return
    async def patched_handle(self, request):
        try:
            body = await request.get_data()
            if body:
                msg = json.loads(body.decode("utf-8"))
                d = (msg.get("d") or {}) if isinstance(msg, dict) else {}
                mid = d.get("id") if isinstance(d, dict) else None
                if mid:
                    if _APPLY_QUOTE and d.get("msg_elements"):
                        _store_quoted_elements(str(mid), d.get("msg_elements"))
                    if _APPLY_ASR and d.get("attachments"):
                        _store_raw_attachments(str(mid), d.get("attachments"))
        except Exception:
            pass
        return await orig(self, request)
    patched_handle._qq_quote_patched = True
    QQOfficialWebhook.handle_callback = patched_handle
    logger.info("[qqofficial_quote] 已 patch QQOfficialWebhook.handle_callback")


# ====================================================================
# 插件入口
# ====================================================================


@register(
    "astrbot_plugin_qqofficial_quote",
    "yuweitk",
    "QQ官方引用消息+内置ASR语音识别",
    "0.3.0",
)
class QQOfficialQuotePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else AstrBotConfig({
            "enable_quote": True,
            "enable_asr": True,
        })

    async def initialize(self) -> None:
        builtin = _detect_builtin_quote()
        logger.info(
            f"[qqofficial_quote] v0.3.0 已加载 "
            f"(quote={'启用' if self.config.get('enable_quote', True) else '关闭'}, "
            f"asr={'启用' if self.config.get('enable_asr', True) else '关闭'}, "
            f"AstrBot内置quote={'是' if builtin else '否'})"
        )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self) -> None:
        try:
            _apply_patches(self.context, dict(self.config))
        except Exception as e:
            logger.error(f"[qqofficial_quote] 应用 patch 失败: {e}", exc_info=True)

    @filter.command("qqquote")
    async def cmd_qqquote(self, event: AstrMessageEvent):
        """查看当前配置: /qqquote"""
        builtin = _detect_builtin_quote()
        eq = self.config.get("enable_quote", True)
        ea = self.config.get("enable_asr", True)
        yield event.plain_result(
            f"【QQ引用+ASR 插件 v0.3.0 配置】\n\n"
            f"引用消息: {'✅ 启用' if eq else '❌ 关闭'}"
            f"{' (AstrBot已内置,跳过patch)' if builtin and eq else ''}\n"
            f"ASR语音:  {'✅ 启用' if ea else '❌ 关闭'}\n\n"
            f"📁 配置文件: _conf_schema.json\n"
            f"   在 WebUI 管理面板直接修改, 保存后重启 AstrBot 生效\n"
            f"   当前: enable_quote={str(eq).lower()}, enable_asr={str(ea).lower()}"
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
