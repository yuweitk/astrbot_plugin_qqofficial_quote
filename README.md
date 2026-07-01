# astrbot_plugin_qqofficial_quote

> 为 AstrBot QQ 官方适配器补全 **引用消息解析** + **QQ内置ASR语音转文字** 能力

---

## 功能

| 功能 | 说明 | 版本 |
|------|------|------|
| 引用消息解析 | 用户回复(引用)消息时,机器人能感知被引用的文本/图片/语音/文件 | v0.1.0 |
| QQ内置ASR | QQ平台免费提供的语音转文字(`asr_refer_text`),零配置自动识别 | v0.2.0 |

---

## 背景

### 问题一：引用消息无法解析

QQ 官方 API 在用户回复消息时,推送 `message_type=103` 和 `msg_elements` 字段,
但 `botpy` SDK 的 `__slots__` 不包含这些字段,直接丢弃。

### 问题二：语音消息无法识别

QQ 平台对语音消息免费提供腾讯ASR转写结果(`asr_refer_text`),同时提供预转换WAV下载链接(`voice_wav_url`)。
但 `botpy` 的 `_Attachments.__slots__` 同样丢弃了这两个字段。

> 参考实现: [hermes-agent](https://github.com/NousResearch/hermes-agent/tree/main/gateway/platforms/qqbot)

---

## 工作原理

本插件通过 **monkey-patch** 技术,在不修改 AstrBot 源码的前提下工作:

```
QQ 推送消息 payload
    |
    +-- [WebSocket] patched ConnectionState parser 捕获 msg_elements + 原始 attachments -> 缓存
    +-- [Webhook]   patched handle_callback 同样捕获
    |
    v
patched _parse_from_qqofficial()
    +-- 查询缓存获取 msg_elements -> 构造 Reply 组件
    +-- 查询缓存获取原始 attachments -> 提取 asr_refer_text -> 注入消息链
    |
    v
进入 AstrBot 管道(Reply + ASR文本已在消息入口就存在)
```

### 四层 Patch

| 序号 | 目标 | 作用 |
|------|------|------|
| 1 | `botpy.connection.ConnectionState` parser | 捕获 `msg_elements`(引用消息) + 原始 `attachments`(含ASR) |
| 2 | `QQOfficialWebhook.handle_callback` | Webhook 模式同样捕获 |
| 3 | `QQOfficialPlatformAdapter._parse_from_qqofficial` | 构造 `Reply` 组件 + 注入 ASR 文本 |
| 4 | 引用消息中的 ASR | 引用语音消息时,被引用语音的 ASR 文本注入 `Reply.chain` |

---

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/yuweitk/astrbot_plugin_qqofficial_quote.git
```

## ⚠️ 重要：必须重启AstrBot！

本插件通过 `on_platform_loaded` 钩子应用 monkey-patch,该钩子**仅在 AstrBot 启动时平台加载完毕后触发**。

- ❌ **仅重载插件**不会生效
- ❌ **先装插件后启用平台**不会重新触发
- ✅ **必须完全重启 AstrBot** 才能应用 patch

```
# 重启AstrBot
cd /root/AstrBot
# 停止后重新启动
```

重启后检查日志,应看到:
```
[qqofficial_quote] 插件已加载,等待平台就绪后自动应用 patch...
[qqofficial_quote] 所有 patch 已应用
```

---

## 使用

安装并重启后无需任何配置,插件自动生效:

### 引用消息
- 用户回复消息时,机器人能感知被引用的**文本内容**
- 被引用的**图片/语音/视频/文件**也会被解析
- 引用内容注入 LLM 请求,机器人可基于引用内容回复

### 语音转文字(ASR)
- QQ 平台免费提供腾讯 ASR,零配置、无需 API Key
- 语音消息自动追加 `[语音转文字] xxx` 到消息中
- 引用语音消息时,被引用语音的文字也会追加

---

## 故障排查

### 确认插件是否生效

发一条语音消息后,在 AstrBot 日志中搜索 `qqofficial_quote`:

**插件已生效**(正常):
```
[qqofficial_quote] parse_group_at_message_create raw attachments: [...]
[qqofficial_quote] attachment keys: ['content_type', 'filename', 'url', 'asr_refer_text', ...]
[qqofficial_quote] voice attachment: ct=audio/amr, asr_refer_text=有(你好), voice_wav_url=有
```

**插件未生效**(patch未应用):
日志中完全没有 `[qqofficial_quote]` 相关行 → 需要重启 AstrBot

**ASR字段不存在**(QQ未提供):
```
[qqofficial_quote] voice attachment: ct=audio/amr, asr_refer_text=无(), voice_wav_url=无
```
→ QQ 未返回 ASR 结果,需要配置外部 STT Provider

### 已知限制

- 本插件仅处理 QQ**平台层面**丢失的字段(`msg_elements` / `asr_refer_text`)
- **不实现** STT API 调用(AstrBot 已有自己的 STT 管道 — 见 `PreProcessStage`)
- **不处理** SILK 音频解码(AstrBot 的 `MediaResolver` 已有)
- `asr_refer_text` 是否可用取决于 QQ 平台,非所有语音消息都有

---

## 兼容性

- AstrBot >= 4.26.0
- 平台: `qq_official` / `qq_official_webhook`
- 依赖: `botpy`(AstrBot 自带), 无额外依赖

---

## 版本历史

| 版本 | 日期 | 内容 |
|------|------|------|
| v0.2.0 | 2026-07-01 | 新增 QQ内置ASR语音转文字,引用语音ASR,调试日志 |
| v0.1.0 | 2026-06-30 | 首次发布,引用消息解析 |

---

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [hermes-agent QQ 适配器](https://github.com/NousResearch/hermes-agent/tree/main/gateway/platforms/qqbot)
- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)

## License

AGPL-3.0
