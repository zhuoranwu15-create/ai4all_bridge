# 语音输入技术设计

更新时间：2026-06-02

本文承接 [语音输入 PRD](../product/voice_prd.md)，定义当前 Phase 1 内测的微信语音输入技术口径。

当前决策：不做后端 ASR。AI4ALL Backend 依赖 `openclaw-weixin` 上游在微信语音消息中提供的 `voice_item.text` / `cleanedBody` 转写正文，并复用现有文本 turn 链路。

详细调研记录见 [微信语音输入开发追踪](voice_input_asr_tracking.md)。

## 1. 设计目标

- 支持用户在微信私聊发送语音消息。
- 上游已转写时，Bridge 将转写文本按普通文本消息转发给 Backend。
- Backend 复用现有文本对话、上下文、记忆、限流和扣费链路。
- 不保存原始语音文件。
- 当前不接入豆包 ASR、不下载媒体、不转码、不新增 ASR 成本事件。

## 2. 当前链路

```text
WeChat voice
-> openclaw-weixin receives voice item
-> voice_item.text contains transcript when available
-> openclaw-weixin maps transcript into cleanedBody
-> ai4all-openclaw-bridge sends message_type=text + text=transcript
-> /openclaw/turn normal text pipeline
-> LLM reply
-> WeChat text reply
```

## 3. Bridge 与 Backend 边界

Bridge：

- 读取 OpenClaw event 中的 `cleanedBody`。
- 有正文时按普通文本 turn 转发。
- 可保留受控 voice debug，用于排查上游是否提供转写文本；默认关闭。
- 不把原始语音媒体作为业务 payload 主路径。

Backend：

- 不需要新增 `app/asr.py`。
- 不需要新增 ASR provider 配置。
- 不需要读取语音媒体文件或 URL。
- 不需要做 60 秒语音时长校验。
- 转写正文进入现有 `message_type=text` 链路。

## 4. 数据与隐私

- `messages.content` 保存上游转写文本。
- 不保存原始语音文件。
- daily notes 只保存文字化材料。
- 转写文本按用户聊天正文处理，Admin/Debug 默认脱敏，明文查看必须走授权和审计。
- 如果后续 Bridge 记录 voice debug，debug 内容必须脱敏、截断，并默认关闭。

## 5. 失败体验

如果上游没有提供可用正文，当前 Backend 不进行 ASR fallback。

建议用户侧提示：

```text
这条语音我没听清，可以再发一次，或者打字告诉我。
```

禁止行为：

- 不得让 LLM 基于空文本猜测语音内容。
- 不得声称已调用后端 ASR。
- 不得保存或暴露不必要的语音媒体信息。

## 6. 成本与贝壳

- 当前不产生 ASR 成本事件。
- 用户语音被上游转成文本后，后续 AI 回复按普通聊天 token 规则扣减。
- 如果未来接入后端 ASR fallback，再补充 ASR cost event、duration、provider request id 和贝壳扣减规则。

## 7. 后续 Fallback 预案

以下能力当前不实现，仅在 `openclaw-weixin` 上游转写不稳定、或新增其他微信/语音通道时重新评估：

- Bridge 提取 `MediaPath` / `MediaType` / `media_id`。
- Backend 安全读取或下载临时语音文件。
- 接入豆包或其他 ASR provider。
- SILK / AMR / WAV 格式兼容和转码。
- 单条语音最大时长限制。
- ASR 失败体验、ASR 成本事件和贝壳扣减。

## 8. 验收点

- 真实微信语音在上游提供转写文本时，AI4ALL 能生成文本回复。
- Backend `messages` 保存转写文本，不保存原始语音文件。
- 语音转写文本进入普通上下文和记忆链路。
- 无转写文本时，不进入 LLM 猜测语音内容。
- 不新增 ASR provider 凭证或部署依赖。
