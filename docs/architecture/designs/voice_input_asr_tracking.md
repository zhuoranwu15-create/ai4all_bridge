# 微信语音输入开发追踪

更新时间：2026-06-02

状态：方案已收敛。当前不做后端 ASR，依赖 `openclaw-weixin` 上游语音转文字。

相关文档：

- [语音输入 PRD](../../product/voice_prd.md)
- [语音输入技术设计](voice_input_design.md)
- [OpenClaw Bridge 设计](openclaw_bridge_design.md)
- OpenClaw 微信插件文档：`/Users/suchong/workspace/openclaw/docs/channels/wechat.md`
- 已安装微信插件源码：`/Users/suchong/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin/`

## 1. 当前结论

当前 AI4ALL 微信语音输入已经可用，原因不是 Backend 已实现 ASR，而是唯一上游 `openclaw-weixin` 会在部分语音消息中提供 `voice_item.text`。

实测链路：

```text
WeChat voice
-> openclaw-weixin receives MessageItemType.VOICE
-> voice_item.text already contains transcript
-> openclaw-weixin maps transcript to inbound Body
-> OpenClaw before_agent_reply event.cleanedBody is transcript
-> ai4all-openclaw-bridge sends message_type=text + text=transcript
-> AI4ALL Backend uses existing text turn pipeline
-> WeChat receives text reply
```

因此，当前项目范围不需要接入豆包 ASR、不需要后端下载音频、不需要音频转码或重采样。

## 2. 实测证据

测试时间：2026-06-02 09:53 左右。

用户发送一条微信语音，内容被上游转写为：

```text
北京和上海哪个更好
```

OpenClaw / Bridge 日志关键结果：

- `openclaw-weixin` 收到 `types=3`，源码中 `3 = MessageItemType.VOICE`。
- `openclaw-weixin` 入站日志显示 `bodyLen=9 hasMedia=false`。
- Bridge voice debug 显示 `cleanedBody.length=9`、`hasMediaAttachedMarker=false`、`eventFieldCount=1`、`ctxFieldCount=0`。
- Backend `messages` 表中入站消息保存为 `message_type=text`、`content=北京和上海哪个更好`。
- 出站消息来源为 `ai4all_sync_reply`，确认回复由 AI4ALL Backend 生成。

相关源码：

- `openclaw-weixin/src/api/types.ts`
  - `MessageItemType.VOICE = 3`
- `openclaw-weixin/src/messaging/inbound.ts`
  - `bodyFromItemList()` 对 `item.type === VOICE && item.voice_item?.text` 直接返回 `voice_item.text`
- `openclaw-weixin/src/messaging/process-message.ts`
  - 只有 `VOICE` 且存在可下载媒体且没有 `voice_item.text` 时，才尝试下载音频媒体
- `openclaw-bridge/index.js`
  - 当前把 `event.cleanedBody` 作为 `message_type=text` 发给 Backend

## 3. 当前实现策略

保留现有文本链路，不新增语音专用 Backend 逻辑。

当前应支持：

- 用户发微信语音。
- `openclaw-weixin` 上游提供 `voice_item.text`。
- Bridge 以普通文本 turn 转发。
- Backend 正常按文本消息处理、回复、写入记忆。

当前不支持，也不作为本阶段目标：

- 后端豆包 ASR。
- 音频文件下载、读取、鉴权、allowed roots 校验。
- SILK / AMR / WAV 转码。
- ffmpeg 部署依赖。
- ASR 成本记录和扣费。
- 返回语音回复 / TTS。
- 无上游转写文本的语音 fallback。

## 4. 业务边界

当前项目只有一个微信上游：`openclaw-weixin`。

只要该上游持续提供 `voice_item.text`，AI4ALL Backend 不需要知道原始输入是语音还是文字；它只需要处理最终文本。

如果未来新增其他上游，或 `openclaw-weixin` 不再稳定提供 `voice_item.text`，再重新评估 Backend ASR fallback。届时可重新打开以下方向：

- Bridge 提取 `MediaPath` / `MediaType`。
- Backend 安全读取音频文件。
- 接入豆包或其他 ASR provider。
- 处理 SILK / WAV 格式兼容。

这些方向目前只作为未来预案，不进入当前开发计划。

## 5. 当前代码影响

Backend：

- 不需要修改 `app/turn_service.py`。
- 不需要新增 `app/asr.py`。
- 不需要扩展 `app/config.py` 和 `.env.example` 的 ASR 配置。
- 不需要新增 ASR 单元测试。

Bridge：

- 现有 `message_type=text` 转发逻辑已经满足当前需求。
- 本轮调研新增的 `voiceDebug` 是受控调试开关，默认关闭，不参与业务逻辑。
- 本地 OpenClaw runtime 已将 `voiceDebug` 关闭，避免持续产生日志噪音。

## 6. 验收标准

当前阶段验收标准：

- 微信发送语音后，AI4ALL 能基于语音转写内容给出文本回复。
- Backend `messages` 中保存的是转写文本。
- 不出现“语音消息我暂时处理不了”这类能力缺失回复。
- 不保存原始语音文件。
- 不新增 ASR provider 凭证或部署依赖。

已验证：

| 日期 | 状态 | 记录 |
| --- | --- | --- |
| 2026-06-02 | completed | 真实微信语音被 `openclaw-weixin` 识别为 `MessageItemType.VOICE`，并通过 `voice_item.text` 转为正文。 |
| 2026-06-02 | completed | AI4ALL Backend 收到 `message_type=text`、`content=北京和上海哪个更好`，并生成相关回复。 |
| 2026-06-02 | completed | 确认当前不需要豆包 ASR、不需要 Backend 音频 fallback。 |

## 7. 后续动作

当前不继续推进 ASR 开发。

建议后续只做两个收尾动作：

1. 保留本文档作为决策记录，避免后续重复调研豆包 ASR。
2. 观察几轮真实微信语音输入。如果都稳定带 `voice_item.text`，可考虑移除本轮新增的 Bridge `voiceDebug` 调试代码，只保留文档结论。
