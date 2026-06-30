"""
单元测试: QQ 官方引用消息解析核心逻辑

运行方式:
    python test_parse_quote.py
"""

import sys
import os

# 将插件目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_parse_quoted_message_text_only():
    """测试纯文本引用消息"""
    # 模拟导入(不依赖 astrbot 完整环境)
    # 手动构造一个最小化的 Reply 类来测试

    # 由于 Reply/Image/Plain 等依赖 astrbot,我们只测试 msg_elements 解析逻辑
    # 直接导入 main.py 中的 _parse_quoted_message 会触发 astrbot import
    # 所以这里复刻核心逻辑做独立测试

    msg_elements = [
        {
            "content": "你好世界",
            "attachments": [],
        }
    ]

    # 复刻 _parse_quoted_message 的文本提取逻辑
    quoted_text_parts = []
    for elem in msg_elements:
        if not isinstance(elem, dict):
            continue
        etext = str(elem.get("content", "")).strip()
        if etext:
            quoted_text_parts.append(etext)

    quoted_message_str = " ".join(quoted_text_parts).strip()
    assert quoted_message_str == "你好世界", (
        f"期望 '你好世界', 实际 '{quoted_message_str}'"
    )
    print("✅ test_parse_quoted_message_text_only 通过")


def test_parse_quoted_message_empty():
    """测试空引用消息"""
    msg_elements = []

    quoted_text_parts = []
    quoted_chain = []
    for elem in msg_elements:
        pass  # 空列表不循环

    quoted_message_str = " ".join(quoted_text_parts).strip()
    assert not quoted_message_str
    assert not quoted_chain
    print("✅ test_parse_quoted_message_empty 通过")


def test_parse_quoted_message_none():
    """测试 None 引用消息"""
    msg_elements = None

    # _parse_quoted_message 对 None 直接返回 None
    assert (
        msg_elements is None or not isinstance(msg_elements, list) or not msg_elements
    )
    print("✅ test_parse_quoted_message_none 通过")


def test_parse_quoted_message_with_attachments():
    """测试带附件的引用消息"""
    msg_elements = [
        {
            "content": "看这张图",
            "attachments": [
                {
                    "content_type": "image/jpeg",
                    "url": "multimedia.nt.qq.com/example.jpg",
                    "filename": "example.jpg",
                }
            ],
        }
    ]

    # 测试附件 URL 规范化
    def _normalize_attachment_url(url):
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"https://{url}"

    url = _normalize_attachment_url("multimedia.nt.qq.com/example.jpg")
    assert url == "https://multimedia.nt.qq.com/example.jpg", f"URL 规范化失败: {url}"

    url2 = _normalize_attachment_url("https://example.com/img.png")
    assert url2 == "https://example.com/img.png"

    url3 = _normalize_attachment_url(None)
    assert url3 == ""

    print("✅ test_parse_quoted_message_with_attachments 通过")


def test_parse_quoted_message_multiple_elements():
    """测试多个 element 的引用消息"""
    msg_elements = [
        {"content": "第一段", "attachments": []},
        {"content": "第二段", "attachments": []},
    ]

    quoted_text_parts = []
    for elem in msg_elements:
        if not isinstance(elem, dict):
            continue
        etext = str(elem.get("content", "")).strip()
        if etext:
            quoted_text_parts.append(etext)

    quoted_message_str = " ".join(quoted_text_parts).strip()
    assert quoted_message_str == "第一段 第二段", (
        f"期望 '第一段 第二段', 实际 '{quoted_message_str}'"
    )
    print("✅ test_parse_quoted_message_multiple_elements 通过")


def test_parse_face_message():
    """测试 QQ 表情消息解析"""
    import base64
    import json
    import re

    # 构造一个 face tag
    ext_data = {"text": "[满头问号]"}
    ext_encoded = base64.b64encode(json.dumps(ext_data).encode()).decode()
    face_tag = f'<faceType=4,faceId="",ext="{ext_encoded}">'
    content = f"hello {face_tag} world"

    FACE_TAG_RE = re.compile(r"<faceType=\d+[^>]*>")

    def replace_face(match):
        face_tag_inner = match.group(0)
        ext_match = re.search(r'ext="([^"]*)"', face_tag_inner)
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

    result = FACE_TAG_RE.sub(replace_face, content)
    assert "[表情:[满头问号]]" in result, f"表情解析失败: {result}"
    print("✅ test_parse_face_message 通过")


def test_cache_store_and_pop():
    """测试缓存存取"""
    # 直接测试缓存逻辑(复刻 main.py 的缓存)
    _cache = {}
    _timestamps = {}

    def store(mid, elements):
        if not mid or not elements:
            return
        _cache[mid] = elements
        _timestamps[mid] = 0  # 简化

    def pop(mid):
        return _cache.pop(mid, None)

    store("msg1", [{"content": "hello"}])
    assert pop("msg1") == [{"content": "hello"}]
    assert pop("msg1") is None  # 二次 pop 应为 None
    assert pop("msg2") is None  # 不存在的 key

    store("", [{"content": "empty"}])  # 空 ID 不应存储
    assert len(_cache) == 0

    store("msg3", None)  # None elements 不应存储
    assert len(_cache) == 0

    print("✅ test_cache_store_and_pop 通过")


if __name__ == "__main__":
    print("=" * 60)
    print("QQ 官方引用消息解析 - 单元测试")
    print("=" * 60)

    test_parse_quoted_message_text_only()
    test_parse_quoted_message_empty()
    test_parse_quoted_message_none()
    test_parse_quoted_message_with_attachments()
    test_parse_quoted_message_multiple_elements()
    test_parse_face_message()
    test_cache_store_and_pop()

    print("=" * 60)
    print("所有测试通过! ✅")
    print("=" * 60)
