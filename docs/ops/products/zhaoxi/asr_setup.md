# 豆包大模型 ASR 极速版配置

朝夕语音消息采用火山引擎「大模型录音文件识别极速版」。客户端继续上传 M4A/AAC；服务端使用
`imageio-ffmpeg` wheel 内置二进制生成临时 16kHz/16-bit/单声道 PCM WAV，原媒体不会被改写。

## 控制台前置条件

为对应火山应用开通 Resource ID：

```text
volc.bigasr.auc_turbo
```

2026-07-31 使用旧 `ai_counselor` App ID/Access Token 做脱敏探测时，接口返回 HTTP 403、
厂商码 `45000030 requested resource not granted`。开通资源后需要重新探测，不能只凭旧
`/api/v2/asr` 可用就判断极速版已就绪。

## 环境变量

生产 `.env`：

```dotenv
ASR_PROVIDER=volcengine_flash
ASR_TIMEOUT_SECONDS=20
VOLCENGINE_ASR_ENDPOINT=https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash
VOLCENGINE_ASR_RESOURCE_ID=volc.bigasr.auc_turbo
```

鉴权二选一：

```dotenv
# 新版 API Key
VOLCENGINE_ASR_API_KEY=<secret>

# 或旧版 App ID + Access Token
VOLCENGINE_ASR_APP_ID=<secret>
VOLCENGINE_ASR_ACCESS_TOKEN=<secret>
```

不要把凭据提交到 Git、粘贴到工单或输出到日志。旧项目的
`DOUBAO_ASR_APP_ID` / `DOUBAO_ASR_ACCESS_TOKEN` 分别映射到后两项，不能填入原
OpenAI-compatible 的 `ASR_API_KEY`。

`ASR_FFMPEG_PATH` 默认留空，使用 requirements 固定的内置二进制；只有运维明确提供系统
ffmpeg 时才填绝对路径。`ASR_TRANSCODE_TIMEOUT_SECONDS` 默认 15 秒。

## 上线与验收

1. 安装固定依赖：`.venv/bin/pip install -r requirements.txt`。
2. 重启后确认 `/v1/app/config` 的 `features.voice_input=true`，响应中不出现任何 ASR 凭据。
3. 用真实 1～10 秒 M4A 调用 `POST /v1/media/uploads`：返回 HTTP 200，`mime=audio/m4a`，
   `transcript` 非空。
4. 原样 GET 返回的签名 URL：HTTP 200、`Content-Type: audio/m4a`、非空字节且可播放。
5. 发送该 `media_id` 后，AI 能按 transcript 内容回复。
6. 临时改错 Resource ID 做降级验证：上传仍为 HTTP 200、音频仍可播放，只有
   `transcript=null`；恢复配置后重启。

日志只允许记录 request ID、`X-Tt-Logid`、供应商状态码、输入/临时 WAV 字节数和错误类型；
不得记录原音频、base64 请求体、转写全文或凭据。
