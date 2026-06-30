# astrbot_plugin_qqofficial_quote

> 为 AstrBot 的 QQ 官方适配器补全群聊/私聊引用消息(回复消息)的解析能力

## 背景

AstrBot 的 QQ 官方机器人适配器(`qq_official` / `qq_official_webhook`)目前**不支持解析引用消息**。当用户在 QQ 群聊或私聊中回复(引用)一条消息时,机器人**无法感知用户引用了什么内容**。

### 根因

QQ 官方 API 在用户回复消息时,推送的 payload 中:
- `message_type = 103`(表示引用消息)
- 被引用消息的内容和附件放在 `msg_elements` 字段中

但 `botpy` SDK 的消息类使用 `__slots__` 限定字段,**没有保存 `message_type` 和 `msg_elements`**,导致这些数据在 SDK 层面就被丢弃了。

而 AstrBot 的其他适配器(`aiocqhttp`、`lark`、`satori`、`weixin_oc`)都已实现了引用消息解析,下游管道(`PreProcessStage`、`astr_main_agent`)也已完整支持 `Reply` 组件处理。缺口仅在 QQ 官方适配器上游。

## 解决方案

本插件通过 **monkey-patch** 技术,在**不修改 AstrBot 源码**的前提下,补全引用消息解析能力。

### 工作原理

```
QQ 推送引用消息 (message_type=103, msg_elements=[...])
    |
    +-- [WebSocket] patched ConnectionState parser 捕获 msg_elements -> 缓存
    |
    +-- [Webhook]   patched handle_callback 捕获 msg_elements -> 缓存
    |
    v
patched _parse_from_qqofficial()
    +-- 调用原始方法获取 abm (AstrBotMessage)
    +-- 查询缓存获取 msg_elements
    +-- _parse_quoted_message() 构造 Reply 组件
    +-- Reply 组件插入 abm.message 消息链头部
    |
    v
进入 AstrBot 管道 (Reply 组件在管道入口就存在)
    +-- PreProcessStage: 处理 Reply.chain 中的 Record(STT)/Image(格式转换)
    +-- astr_main_agent: 提取引用文本/图片注入 LLM 请求
```

### 三层 Patch

| Patch | 目标 | 作用 |
|-------|------|------|
| 1 | `botpy.connection.ConnectionState` 的 parser | WebSocket 模式下捕获 `msg_elements` |
| 2 | `QQOfficialWebhook.handle_callback` | Webhook 模式下捕获 `msg_elements` |
| 3 | `QQOfficialPlatformAdapter._parse_from_qqofficial` | 构造 `Reply` 组件并注入消息链 |

所有 patch 在 `on_platform_loaded` 钩子中应用,此时平台适配器已实例化但尚未开始运行。

## 安装

将本插件放入 AstrBot 的 `data/plugins/` 目录:

```bash
cd AstrBot/data/plugins
git clone https://github.com/yuweitk/astrbot_plugin_qqofficial_quote.git
```

然后在 AstrBot WebUI 的插件管理页面重载插件,或重启 AstrBot。

## 使用

安装后无需任何配置,插件会自动生效。当用户在 QQ 群聊或私聊中回复消息时:

- 机器人能感知被引用消息的**文本内容**
- 机器人能感知被引用消息的**图片/语音/视频/文件附件**
- 引用内容会被注入 LLM 请求,机器人可基于引用内容回复

## 兼容性

- AstrBot >= 4.26.0
- 平台: `qq_official` / `qq_official_webhook`
- 依赖: `botpy`(AstrBot 自带)

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [hermes-agent QQ 适配器](https://github.com/NousResearch/hermes-agent/tree/main/gateway/platforms/qqbot) — 引用消息处理逻辑参考
- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)

## License

AGPL-3.0
