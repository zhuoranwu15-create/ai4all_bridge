# 语音输入技术设计

更新时间：2026-05-28

本文承接 [语音输入 PRD](../product/voice_prd.md)，定义 Phase 1 微信语音输入、豆包 ASR、60 秒限制和转写后进入统一对话链路的技术设计。

搜索与异步任务有独立技术文档，见 [搜索与异步任务技术设计](search_async_tasks_design.md)。语音能力可以复用任务、provider adapter、成本事件等底层能力，但产品流程和验收独立维护。

## 1. 设计目标

- 支持用户在微信私聊发送语音消息。
- 对齐微信语音最大时长 60 秒。
- 通过豆包 ASR 转写语音。
- 转写文本进入统一文本对话链路。
- Phase 1 只返回文本回复，不做语音回复/TTS。
- ASR 失败、语音过长、格式不支持时给用户明确提示。
- 不保存不必要的长期语音文件。

## 2. 范围

本文覆盖：

- OpenClaw / Bridge 语音 payload。
- 媒体获取或媒体引用处理。
- 时长、格式和可访问性校验。
- 豆包 ASR provider adapter。
- 转写文本进入 session/messages。
- ASR 成本事件。
- 失败体验。

本文不覆盖：

- Web Search 和搜索 provider，见 [搜索与异步任务技术设计](search_async_tasks_design.md)。
- TTS 或语音回复。
- 长期媒体归档策略的合规细节。

## 3. 用户体验链路

```text
user voice message
-> OpenClaw / Bridge payload
-> AI4ALL media resolver
-> duration / format check
-> Doubao ASR
-> transcript
-> normal text turn
-> text reply
```

失败提示：

- ASR 失败：`这条语音我没听清，可以再发一次，或者打字告诉我。`
- 超过 60 秒：`这条语音有点长，我现在最多能处理 60 秒内的语音。可以分段发我，或者改用文字。`
- 格式或媒体不可访问：`这条语音我暂时打不开，可以再发一次吗？`

## 4. OpenClaw / Bridge Payload

目标 payload 至少需要提供：

```text
message_id
account_id / channel_account_id
session_key
sender_id
chat_id
message_type: voice
media_id 或 media_url / media_ref
duration_seconds
format / mime_type
raw metadata
```

实现要求：

- `ai4all_account_id` 仍由 identity resolver 解析，不能直接使用 OpenClaw 原生账号作为业务主键。
- 媒体引用必须绑定当前账号和消息。
- 如果 payload 缺失 duration，需要尝试从媒体 metadata 读取；读取失败时按不可处理失败。
- raw payload 默认按隐私设计脱敏展示。

## 5. 媒体处理

Phase 1 策略：

- 优先使用 OpenClaw / Bridge 提供的可访问媒体引用。
- 如需下载临时文件，仅用于 ASR 调用。
- 不把原始语音文件长期保存到 daily notes。
- daily notes 只保存 ASR 转写文本。
- 媒体临时文件应有短 TTL，处理完成后可删除。

校验：

- 时长必须 `<= 60s`。
- 格式必须在豆包 ASR 支持范围内。
- 媒体大小需要设置后端上限，避免异常大文件。
- 不可访问或下载失败时，不进入 LLM。

## 6. 豆包 ASR Adapter

建议封装：

```python
transcribe_voice(
    *,
    account_id: str,
    message_id: str,
    media_ref: str,
    duration_seconds: float,
    mime_type: str | None,
) -> AsrResult
```

`AsrResult`：

```text
status: succeeded | failed
transcript
language
duration_seconds
provider: doubao
provider_request_id
latency_ms
error
metadata
```

要求：

- provider key 和配置由内部环境变量或后台配置管理。
- 不在 prompt、日志或 Admin 默认视图暴露密钥。
- ASR 调用失败要记录 provider、错误码、耗时。
- 空转写或低置信转写按 ASR 失败处理。

## 7. 同步与异步边界

Phase 1 可以先按两种路径实现：

| 场景 | 路径 |
| --- | --- |
| 短语音且 ASR 预计较快 | 同步完成 ASR，再进入普通文本 turn |
| ASR 较慢、provider 排队或媒体下载慢 | 先确认收到，创建语音转写 task，完成后补发文本回复 |

无论同步还是异步，最终都要形成统一的文本用户消息：

```text
role = user
content = transcript
metadata.modality = voice
metadata.asr_provider = doubao
metadata.source_voice_message_id = ...
```

如果走异步补发，补发属于用户触发任务结果投递，不算无触发主动推送。

## 8. 数据模型

建议在 messages 或 message metadata 中记录：

```text
message_type: voice
media_ref
duration_seconds
mime_type
asr_status
asr_provider
asr_request_id
transcript_message_id
error
```

如果复用 task 底座：

```text
task_type = voice_asr
source_message_id = original voice message
result_json.transcript = ...
```

成本事件：

```text
cost_type = asr
provider = doubao
duration_seconds = ...
computed_shell_micros = ...
status = pending | charged | waived | failed
```

具体贝壳换算由 [贝壳、增长与可选支付技术设计](entitlement_growth_design.md) 定义。

## 9. Prompt 与上下文处理

转写文本进入普通文本对话链路，但需要保留来源 metadata：

- 最近对话中可以把它当作用户文本。
- daily notes 保存 ASR 文本，不保存原始语音。
- Debug trace 默认不展示语音正文，明文查看按隐私权限控制。
- 如果 ASR 置信度低，不应让 LLM 基于不可靠转写继续发挥。

## 10. 失败与风控

失败时不继续调用 LLM：

- 超过 60 秒。
- 媒体不可访问。
- 格式不支持。
- ASR provider 超时或失败。
- 转写为空或明显低置信。

风控：

- 单账号语音 ASR 需要限频和成本预算。
- 单条语音最大 60 秒。
- 不保存长期原始语音。
- 明文转写文本按正文类敏感信息处理。

## 11. 与搜索异步任务的共用底座

可复用：

- `tasks` / `task_runs`。
- provider adapter 接口。
- worker claim / timeout / retry。
- cost events。
- outbound result delivery。

不合并：

- 语音强调媒体解析、60 秒限制、ASR provider 和 transcript。
- 搜索强调 query、provider 回退、引用和事实性。

## 12. 开发切分

1. 明确 OpenClaw 真实语音 payload。
2. 增加 voice message schema 和 metadata 保存。
3. 实现媒体 resolver 和 60 秒校验。
4. 实现豆包 ASR adapter。
5. 将 transcript 写入统一文本 turn。
6. ASR 失败时返回明确提示。
7. 记录 ASR cost event。
8. 如同步耗时不可接受，接入 task 底座和结果补发。

## 13. 验收点

- 微信语音输入可以通过豆包 ASR 完成转写。
- 超过 60 秒的语音不会进入 ASR 和 LLM 链路。
- ASR 失败有明确失败提示。
- 转写文本能进入统一对话链路。
- 用户收到基于语音内容生成的文本回复。
- daily notes 只保存 ASR 文本，不保存原始语音。
- ASR 成本能被记录，后续可映射到贝壳扣减。
