import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_SECRET = "dev-secret";
// 同步调 /openclaw/turn 的超时（毫秒），同时用作 hook 执行预算（见文件末尾各 api.on 注册）。
// 工具轮会先 out-of-band 发暂态提示，最终回复也由后端 out-of-band 发送；bridge 必须等到
// 后端返回 no_reply，避免中途超时插入"卡住了"兜底再叠加正式答复。
// env AI4ALL_BRIDGE_TIMEOUT_MS / 插件配置 timeoutMs 仍可覆盖 fetch 超时。
const DEFAULT_TIMEOUT_MS = 45000;
const DEFAULT_ONLY_CHANNEL = "openclaw-weixin";
// 多机：读图片本地字节内联进 turn（base64），让中心无需访问 node 本地路径即可理解图片。
// 上限须 <= 中心 image_max_bytes 且 <= nginx client_max_body_size（见部署文档）。
const DEFAULT_IMAGE_MAX_BYTES = 5 * 1024 * 1024;
const DEFAULT_REQUEST_DUMP_DIR = "tmp/llm_request_bodies/openclaw";
const SHADOW_STATE_TTL_MS = 10 * 60 * 1000;
const VOICE_DEBUG_MAX_FIELDS = 80;
const VOICE_DEBUG_MAX_STRING = 240;
const REQUEST_DUMP_FETCH_STATE_KEY = Symbol.for("ai4all.openclawBridge.llmRequestDumpFetch");

function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function pluginConfigFromRuntimeConfig(api) {
  const fullConfig = asRecord(api.config);
  const plugins = asRecord(fullConfig.plugins);
  const entries = asRecord(plugins.entries);
  const entry = asRecord(entries["ai4all-openclaw-bridge"]);
  return asRecord(entry.config);
}

function resolveConfig(api) {
  const cfg = {
    ...asRecord(api.config),
    ...pluginConfigFromRuntimeConfig(api),
    ...asRecord(api.pluginConfig),
  };
  return {
    backendUrl: String(cfg.backendUrl || process.env.AI4ALL_BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/+$/, ""),
    secret: String(cfg.secret || process.env.AI4ALL_BRIDGE_SECRET || DEFAULT_SECRET),
    timeoutMs: Number(cfg.timeoutMs || process.env.AI4ALL_BRIDGE_TIMEOUT_MS || DEFAULT_TIMEOUT_MS),
    onlyChannel: String(cfg.onlyChannel || process.env.AI4ALL_ONLY_CHANNEL || DEFAULT_ONLY_CHANNEL),
    shadowTraceAccountIds: String(cfg.shadowTraceAccountIds || process.env.AI4ALL_SHADOW_TRACE_ACCOUNT_IDS || ""),
    voiceDebug: parseOptionalBoolean(process.env.AI4ALL_VOICE_DEBUG) ?? parseOptionalBoolean(cfg.voiceDebug) ?? false,
    imageMaxBytes: Number(cfg.imageMaxBytes || process.env.AI4ALL_IMAGE_MAX_BYTES || DEFAULT_IMAGE_MAX_BYTES),
    requestDumpEnabled: parseOptionalBoolean(process.env.AI4ALL_LLM_REQUEST_DUMP_ENABLED)
      ?? parseOptionalBoolean(cfg.requestDumpEnabled)
      ?? false,
    requestDumpDir: String(cfg.requestDumpDir || process.env.AI4ALL_LLM_REQUEST_DUMP_DIR || DEFAULT_REQUEST_DUMP_DIR),
  };
}

// 读入站图片本地字节并 base64 内联（多机：中心读不到 node 本地路径）。任何失败/超限
// 都返回 null —— 调用方降级为仅转发 path（单机 aliyun1 中心可直读，向后兼容）。
function readInboundImageBytes(api, path, maxBytes) {
  try {
    const size = statSync(path).size;
    if (size > maxBytes) {
      api.logger.warn(`ai4all bridge image too large, forwarding path only size=${size} max=${maxBytes} path=${path}`);
      return null;
    }
    const buf = readFileSync(path);
    return { dataBase64: buf.toString("base64"), size: buf.length };
  } catch (err) {
    api.logger.warn(`ai4all bridge image read failed, forwarding path only: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}

function parseOptionalBoolean(value) {
  if (value === undefined || value === null || value === "") {
    return undefined;
  }
  const text = String(value ?? "").trim().toLowerCase();
  if (text === "1" || text === "true" || text === "yes" || text === "on") {
    return true;
  }
  if (text === "0" || text === "false" || text === "no" || text === "off") {
    return false;
  }
  return undefined;
}

function parseCsvSet(value) {
  return new Set(String(value || "").split(",").map((item) => item.trim()).filter(Boolean));
}

function safeFilenamePart(value) {
  const text = String(value || "unknown").trim();
  const cleaned = text.replace(/[^a-zA-Z0-9_.-]+/g, "_").replace(/^_+|_+$/g, "");
  return (cleaned || "unknown").slice(0, 80);
}

function requestMethod(input, init) {
  return String(init?.method || input?.method || "GET").toUpperCase();
}

function requestUrl(input) {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.href;
  }
  if (input && typeof input.url === "string") {
    return input.url;
  }
  return "";
}

function bytesFromFetchBody(body) {
  if (body == null) {
    return null;
  }
  if (typeof body === "string") {
    return Buffer.from(body, "utf8");
  }
  if (Buffer.isBuffer(body)) {
    return Buffer.from(body);
  }
  if (body instanceof Uint8Array) {
    return Buffer.from(body);
  }
  if (body instanceof ArrayBuffer) {
    return Buffer.from(new Uint8Array(body));
  }
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
    return Buffer.from(body.toString(), "utf8");
  }
  return null;
}

async function requestBodyBytes(input, init) {
  const direct = bytesFromFetchBody(init?.body);
  if (direct) {
    return direct;
  }
  if (typeof Request !== "undefined" && input instanceof Request) {
    try {
      const cloned = input.clone();
      const buffer = await cloned.arrayBuffer();
      return Buffer.from(buffer);
    } catch {
      return null;
    }
  }
  return null;
}

function llmJsonBodyInfo(bodyBytes) {
  try {
    const parsed = JSON.parse(bodyBytes.toString("utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    const hasModel = typeof parsed.model === "string" && parsed.model.trim();
    const hasLlmShape = Array.isArray(parsed.messages)
      || Array.isArray(parsed.input)
      || typeof parsed.system === "string"
      || Array.isArray(parsed.contents);
    if (!hasModel || !hasLlmShape) {
      return null;
    }
    return {
      model: parsed.model,
      hasMessages: Array.isArray(parsed.messages),
      hasInput: Array.isArray(parsed.input),
      hasSystem: typeof parsed.system === "string",
      hasContents: Array.isArray(parsed.contents),
    };
  } catch {
    return null;
  }
}

function writeLlmRequestDump(api, config, details) {
  try {
    mkdirSync(config.requestDumpDir, { recursive: true });
    const createdAt = new Date().toISOString();
    const state = globalThis[REQUEST_DUMP_FETCH_STATE_KEY] || {};
    state.seq = Number(state.seq || 0) + 1;
    globalThis[REQUEST_DUMP_FETCH_STATE_KEY] = state;
    const digest = createHash("sha256").update(details.bodyBytes).digest("hex");
    const stamp = createdAt.replace(/[-:.]/g, "").replace("T", "-").replace("Z", "Z");
    const urlHost = (() => {
      try {
        return new URL(details.url).host;
      } catch {
        return "unknown-host";
      }
    })();
    const stem = [
      stamp,
      "openclaw",
      String(state.seq).padStart(6, "0"),
      safeFilenamePart(urlHost),
      safeFilenamePart(details.info.model),
    ].join("-");
    const bodyFile = `${config.requestDumpDir.replace(/\/+$/, "")}/${stem}.body.json`;
    const metaFile = `${config.requestDumpDir.replace(/\/+$/, "")}/${stem}.meta.json`;
    writeFileSync(bodyFile, details.bodyBytes);
    writeFileSync(
      metaFile,
      JSON.stringify(
        {
          source: "openclaw",
          created_at: createdAt,
          url: details.url,
          method: details.method,
          model: details.info.model,
          body_file: bodyFile,
          body_bytes: details.bodyBytes.length,
          body_sha256: digest,
          body_shape: {
            has_messages: details.info.hasMessages,
            has_input: details.info.hasInput,
            has_system: details.info.hasSystem,
            has_contents: details.info.hasContents,
          },
        },
        null,
        2,
      ),
      "utf8",
    );
  } catch (err) {
    api.logger.warn(`ai4all bridge failed to dump llm request body: ${err instanceof Error ? err.message : String(err)}`);
  }
}

function installLlmRequestDumpFetch(api) {
  const initialConfig = resolveConfig(api);
  process.env.AI4ALL_LLM_REQUEST_DUMP_ENABLED = initialConfig.requestDumpEnabled ? "true" : "false";
  process.env.AI4ALL_LLM_REQUEST_DUMP_DIR = initialConfig.requestDumpDir;
  const state = globalThis[REQUEST_DUMP_FETCH_STATE_KEY] || {};
  if (state.installed) {
    return;
  }
  const originalFetch = globalThis.fetch;
  if (typeof originalFetch !== "function") {
    api.logger.warn("ai4all bridge request dump skipped: global fetch is unavailable");
    return;
  }
  state.installed = true;
  state.originalFetch = originalFetch;
  state.seq = Number(state.seq || 0);
  globalThis[REQUEST_DUMP_FETCH_STATE_KEY] = state;
  globalThis.fetch = async function ai4allLlmRequestDumpFetch(input, init) {
    const config = resolveConfig(api);
    if (config.requestDumpEnabled && requestMethod(input, init) === "POST") {
      const bodyBytes = await requestBodyBytes(input, init);
      if (bodyBytes && bodyBytes.length > 0) {
        const info = llmJsonBodyInfo(bodyBytes);
        if (info) {
          writeLlmRequestDump(api, config, {
            bodyBytes,
            info,
            method: requestMethod(input, init),
            url: requestUrl(input),
          });
        }
      }
    }
    return originalFetch(input, init);
  };
  api.logger.info("ai4all bridge llm request dump fetch wrapper installed");
}

function isShadowTraceAccount(config, accountId) {
  if (!accountId) {
    return false;
  }
  return parseCsvSet(config.shadowTraceAccountIds).has(String(accountId));
}

function normalizeAccountId(value) {
  return value ? String(value) : undefined;
}

function buildSessionParts(sessionKey) {
  const raw = typeof sessionKey === "string" ? sessionKey : "";
  return {
    sessionKey: raw || undefined,
    senderId: raw || undefined,
    chatId: raw || undefined,
  };
}

function extractAccountId(ctx, provider) {
  if (ctx.accountId) {
    return String(ctx.accountId);
  }
  if (ctx.providerAccountId) {
    return String(ctx.providerAccountId);
  }

  const sessionKey = typeof ctx.sessionKey === "string" ? ctx.sessionKey : "";
  const parts = sessionKey.split(":");
  const providerIndex = parts.indexOf(provider);
  if (provider && providerIndex >= 0 && parts.length > providerIndex + 1) {
    const candidate = parts[providerIndex + 1];
    if (candidate && candidate !== "direct") {
      return candidate;
    }
  }

  return provider || undefined;
}

function buildAccountCandidates(ctx) {
  return {
    messageProvider: ctx.messageProvider || undefined,
    channelId: ctx.channelId || undefined,
    sessionKey: ctx.sessionKey || undefined,
    sessionId: ctx.sessionId || undefined,
    accountId: ctx.accountId || undefined,
    providerAccountId: ctx.providerAccountId || undefined,
    botId: ctx.botId || undefined,
  };
}

function truncateDebugString(value) {
  const text = String(value ?? "");
  if (text.length <= VOICE_DEBUG_MAX_STRING) {
    return text;
  }
  return `${text.slice(0, VOICE_DEBUG_MAX_STRING)}...`;
}

function extractMediaMarkers(text) {
  const markers = [];
  const pattern = /\[media attached(?:\s+\d+\/\d+)?:\s*([^\]]+)\]/gi;
  for (const match of text.matchAll(pattern)) {
    markers.push(truncateDebugString(match[1] || match[0]));
    if (markers.length >= 5) {
      break;
    }
  }
  return markers;
}

// Parse the first `[media attached: <path> (<type>)]` marker that the OpenClaw
// core get-reply patch injects into cleanedBody for inbound media. Returns
// { path, type, caption } or null. We intentionally do NOT reuse
// extractMediaMarkers: it truncates values for debug logging, which would
// corrupt a long absolute media path.
function parseInboundMediaMarker(rawText) {
  const text = typeof rawText === "string" ? rawText : "";
  const match = text.match(/\[media attached(?:\s+\d+\/\d+)?:\s*([^\]]+)\]/i);
  if (!match) {
    return null;
  }
  const inner = match[1].trim();
  let path = inner;
  let type = "";
  const withType = inner.match(/^(.+?)\s+\(([^)]+)\)$/);
  if (withType) {
    path = withType[1].trim();
    type = withType[2].trim();
  }
  if (!path) {
    return null;
  }
  const caption = text.replace(match[0], "").trim();
  return { path, type, caption };
}

// Treat missing type as image (WeChat photos may omit a MIME type); audio/voice
// markers are left to the existing text/voice handling.
function isImageMediaType(type) {
  return !type || /^image\//i.test(type);
}

function summarizePotentialText(value) {
  const text = typeof value === "string" ? value : "";
  const markers = extractMediaMarkers(text);
  return {
    kind: "text_summary",
    length: text.length,
    hasMediaAttachedMarker: markers.length > 0,
    mediaMarkers: markers,
  };
}

function isSensitiveDebugKey(key) {
  const lower = String(key || "").toLowerCase();
  return (
    lower.includes("token") ||
    lower.includes("secret") ||
    lower.includes("authorization") ||
    lower.includes("accesskey") ||
    lower.includes("aes_key") ||
    lower === "key"
  );
}

function isTextBodyDebugKey(key) {
  const lower = String(key || "").toLowerCase();
  return (
    lower === "body" ||
    lower === "rawbody" ||
    lower === "bodyforagent" ||
    lower === "bodyforcommands" ||
    lower === "cleanedbody" ||
    lower === "content" ||
    lower === "text"
  );
}

function isVoiceDebugKey(key) {
  const lower = String(key || "").toLowerCase();
  return (
    lower.includes("media") ||
    lower.includes("voice") ||
    lower.includes("audio") ||
    lower.includes("transcript") ||
    isTextBodyDebugKey(lower)
  );
}

function isVoiceDebugPath(path) {
  const lower = String(path || "").toLowerCase();
  return (
    lower.includes("media") ||
    lower.includes("voice") ||
    lower.includes("audio") ||
    lower.includes("transcript")
  );
}

function isPrimitiveDebugValue(value) {
  return value == null || typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

function sanitizeDebugValue(key, value) {
  if (isSensitiveDebugKey(key)) {
    return "[redacted]";
  }
  if (typeof value === "string") {
    return isTextBodyDebugKey(key) ? summarizePotentialText(value) : truncateDebugString(value);
  }
  if (Array.isArray(value)) {
    return value.slice(0, 8).map((item) => {
      if (typeof item === "string") {
        return truncateDebugString(item);
      }
      if (item == null || typeof item === "number" || typeof item === "boolean") {
        return item;
      }
      return `[${typeof item}]`;
    });
  }
  if (value == null || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  return `[${typeof value}]`;
}

function collectVoiceDebugFields(value, rootName) {
  const output = [];
  const seen = new WeakSet();
  const stack = [{ value, path: rootName, depth: 0, inVoiceContext: false }];
  while (stack.length && output.length < VOICE_DEBUG_MAX_FIELDS) {
    const current = stack.pop();
    if (!current || current.value == null || typeof current.value !== "object") {
      continue;
    }
    if (seen.has(current.value)) {
      continue;
    }
    seen.add(current.value);

    const entries = Array.isArray(current.value)
      ? current.value.slice(0, 12).map((child, index) => [`[${index}]`, child])
      : Object.entries(asRecord(current.value));

    for (const [key, child] of entries) {
      const childPath = key.startsWith("[") ? `${current.path}${key}` : `${current.path}.${key}`;
      const childIsVoiceKey = isVoiceDebugKey(key);
      const childInVoiceContext = current.inVoiceContext || childIsVoiceKey || isVoiceDebugPath(childPath);
      const hasMediaMarker = typeof child === "string" && extractMediaMarkers(child).length > 0;
      if (childIsVoiceKey || hasMediaMarker || (current.inVoiceContext && isPrimitiveDebugValue(child))) {
        output.push({
          path: childPath,
          value: sanitizeDebugValue(key, child),
        });
        if (output.length >= VOICE_DEBUG_MAX_FIELDS) {
          break;
        }
      }
      if (child && typeof child === "object" && current.depth < 5) {
        stack.push({ value: child, path: childPath, depth: current.depth + 1, inVoiceContext: childInVoiceContext });
      }
    }
  }
  return output;
}

function buildVoiceDebugSummary(event, ctx) {
  const cleanedBody = typeof event?.cleanedBody === "string" ? event.cleanedBody : "";
  const eventFields = collectVoiceDebugFields(event, "event");
  const ctxFields = collectVoiceDebugFields(ctx, "ctx");
  return {
    cleanedBody: summarizePotentialText(cleanedBody),
    eventFieldCount: eventFields.length,
    ctxFieldCount: ctxFields.length,
    eventFields,
    ctxFields,
  };
}

function firstPresentRecordValue(record, keys) {
  for (const key of keys) {
    const value = record?.[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      return String(value);
    }
  }
  return undefined;
}

function buildIdDiagnostics(event, ctx) {
  const safeEvent = asRecord(event);
  const safeCtx = asRecord(ctx);
  return {
    ctxRunId: firstPresentRecordValue(safeCtx, ["runId", "run_id"]),
    ctxSessionId: firstPresentRecordValue(safeCtx, ["sessionId", "session_id"]),
    ctxMessageId: firstPresentRecordValue(safeCtx, ["messageId", "message_id"]),
    ctxTurnId: firstPresentRecordValue(safeCtx, ["turnId", "turn_id"]),
    eventId: firstPresentRecordValue(safeEvent, ["id", "eventId", "event_id"]),
    eventMessageId: firstPresentRecordValue(safeEvent, ["messageId", "message_id"]),
    eventMsgId: firstPresentRecordValue(safeEvent, ["msgId", "msg_id", "MsgId"]),
    eventNewMsgId: firstPresentRecordValue(safeEvent, ["newMsgId", "NewMsgId"]),
    eventKeys: Object.keys(safeEvent).sort(),
    ctxKeys: Object.keys(safeCtx).sort(),
  };
}

async function postTurn(config, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await fetch(`${config.backendUrl}/openclaw/turn`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${config.secret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`backend returned ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function postDebugTrace(config, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await fetch(`${config.backendUrl}/openclaw/debug-traces`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${config.secret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`backend returned ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function textFromMessage(message) {
  if (!message || typeof message !== "object") {
    return "";
  }
  const content = message.content;
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content.map((part) => {
      if (typeof part === "string") {
        return part;
      }
      if (part && typeof part === "object") {
        if (typeof part.text === "string") {
          return part.text;
        }
        if (typeof part.content === "string") {
          return part.content;
        }
      }
      return "";
    }).filter(Boolean).join("\n");
  }
  return "";
}

function lastAssistantText(messages) {
  if (!Array.isArray(messages)) {
    return "";
  }
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message && typeof message === "object" && message.role === "assistant") {
      const text = textFromMessage(message).trim();
      if (text) {
        return text;
      }
    }
  }
  return "";
}

function compactMessages(messages) {
  if (!Array.isArray(messages)) {
    return [];
  }
  return messages.map((message) => {
    if (!message || typeof message !== "object") {
      return { role: "unknown", content: String(message) };
    }
    return {
      role: typeof message.role === "string" ? message.role : "unknown",
      content: textFromMessage(message),
    };
  });
}

function shadowKey(type, ...parts) {
  const cleanParts = parts
    .map((part) => (typeof part === "string" ? part.trim() : ""))
    .filter(Boolean);
  if (!type || cleanParts.length === 0) {
    return undefined;
  }
  return JSON.stringify([type, ...cleanParts]);
}

export default definePluginEntry({
  id: "ai4all-openclaw-bridge",
  name: "AI4ALL OpenClaw Bridge",
  description: "Routes OpenClaw chat turns to the AI4ALL backend and returns a synthetic reply.",
  register(api) {
    const initialConfig = resolveConfig(api);
    const hookTimeoutMs = Number.isFinite(initialConfig.timeoutMs) && initialConfig.timeoutMs > 0
      ? initialConfig.timeoutMs
      : DEFAULT_TIMEOUT_MS;
    const shadowAccountCount = parseCsvSet(initialConfig.shadowTraceAccountIds).size;
    api.logger.info(
      `ai4all bridge registered hooks backend=${initialConfig.backendUrl} onlyChannel=${initialConfig.onlyChannel} timeoutMs=${hookTimeoutMs} shadowTraceAccountCount=${shadowAccountCount}`
    );
    installLlmRequestDumpFetch(api);
    const shadowRuns = new Map();

    function stateKeys(...keys) {
      return Array.from(new Set(keys.filter((key) => typeof key === "string" && key.trim())));
    }

    function rememberShadowState(state, ...keys) {
      state.keys = stateKeys(...keys);
      for (const key of state.keys) {
        shadowRuns.set(key, state);
      }
      state.cleanupTimer = setTimeout(() => {
        forgetShadowState(state);
      }, SHADOW_STATE_TTL_MS);
    }

    function findShadowState(...keys) {
      for (const key of stateKeys(...keys)) {
        const state = shadowRuns.get(key);
        if (state) {
          return state;
        }
      }
      return undefined;
    }

    function forgetShadowState(state) {
      if (!state) {
        return;
      }
      if (state.cleanupTimer) {
        clearTimeout(state.cleanupTimer);
      }
      for (const key of state.keys || []) {
        if (shadowRuns.get(key) === state) {
          shadowRuns.delete(key);
        }
      }
    }

    api.on("before_agent_reply", async (event, ctx) => {
      const config = resolveConfig(api);
      const provider = ctx.messageProvider || "";
      const channel = provider || ctx.channelId || "";
      if (config.onlyChannel && provider && provider !== config.onlyChannel) {
        return;
      }

      const session = buildSessionParts(ctx.sessionKey);
      const accountCandidates = buildAccountCandidates(ctx);
      const channelAccountId = extractAccountId(ctx, provider);
      const shadowTrace = isShadowTraceAccount(config, channelAccountId);
      const voiceDebugSummary = config.voiceDebug ? buildVoiceDebugSummary(event, ctx) : undefined;
      const idDiagnostics = buildIdDiagnostics(event, ctx);
      // Detect an inbound image via the media marker injected by the OpenClaw
      // core patch; forward the local path so the backend can run VL on it.
      const inboundMedia = parseInboundMediaMarker(event.cleanedBody);
      const isImageTurn = Boolean(inboundMedia && isImageMediaType(inboundMedia.type));
      // 图片轮：把 node 本地图片读成 base64 内联（path 保留作单机回退/排查面包屑）。
      let imageMedia;
      if (isImageTurn) {
        imageMedia = { path: inboundMedia.path, format: inboundMedia.type || "image" };
        const bytes = readInboundImageBytes(api, inboundMedia.path, config.imageMaxBytes);
        if (bytes) {
          imageMedia.data_base64 = bytes.dataBase64;
          imageMedia.size = bytes.size;
        }
      }
      const payload = {
        event_id: ctx.runId || undefined,
        message_id: ctx.runId || undefined,
        channel: channel || config.onlyChannel,
        channel_account_id: channelAccountId,
        account_id: channelAccountId,
        sender_id: session.senderId,
        chat_id: ctx.channelId || session.chatId,
        chat_type: "private",
        session_key: session.sessionKey,
        message_type: isImageTurn ? "image" : "text",
        text: isImageTurn ? inboundMedia.caption : event.cleanedBody || "",
        ...(isImageTurn ? { media: imageMedia } : {}),
        timestamp: Math.floor(Date.now() / 1000),
        raw: {
          ctx,
          event,
          ai4all_bridge: {
            account_candidates: accountCandidates,
            resolved_account_id: channelAccountId,
            channel_account_id: channelAccountId,
            ...(voiceDebugSummary ? { voice_debug: voiceDebugSummary } : {}),
          },
        },
      };

      let backendTurnStartedAt = 0;
      try {
        if (voiceDebugSummary) {
          api.logger.info(
            `ai4all bridge voice debug channel=${payload.channel} session=${payload.session_key || ""} ` +
            `summary=${JSON.stringify(voiceDebugSummary)}`
          );
        }
        api.logger.info(
          `ai4all bridge forwarding turn channel=${payload.channel} session=${payload.session_key} ` +
          `messageId=${payload.message_id || ""} ids=${JSON.stringify(idDiagnostics)} ` +
          `candidates=${JSON.stringify(accountCandidates)}`
        );
        backendTurnStartedAt = Date.now();
        const result = await postTurn(config, payload);
        const backendRoundtripMs = Date.now() - backendTurnStartedAt;
        api.logger.info(
          `ai4all bridge turn result channel=${payload.channel} session=${payload.session_key || ""} ` +
          `messageId=${payload.message_id || ""} status=${result?.status || "unknown"} ` +
          `noReply=${Boolean(result?.no_reply)} backendRoundtripMs=${backendRoundtripMs} ` +
          `backendLatencyMs=${result?.metadata?.latency_ms ?? ""} replyChars=${typeof result?.reply === "string" ? result.reply.length : 0}`
        );
        if (shadowTrace) {
          const state = {
            channelAccountId,
            channel: payload.channel,
            conversationId: ctx.channelId || payload.chat_id,
            sessionKey: payload.session_key,
            messageId: payload.message_id,
            runId: ctx.runId,
            ai4allReply: typeof result?.reply === "string" ? result.reply : "",
            ai4allStatus: result?.status || "unknown",
            ai4allTraceId: result?.metadata?.debug_trace_id || undefined,
            startedAt: Date.now(),
            deliveryCount: 0,
            tracePosted: false,
            trace: {
              source: "openclaw",
              metadata: {
                mode: "path_b_native_run_suppressed",
                ai4all_status: result?.status || "unknown",
                ai4all_trace_id: result?.metadata?.debug_trace_id || undefined,
                account_candidates: accountCandidates,
              },
            },
          };
          rememberShadowState(
            state,
            ctx.runId,
            payload.session_key,
            payload.message_id,
            shadowKey("account", channelAccountId),
            shadowKey("account-conversation", channelAccountId, ctx.channelId),
            shadowKey("account-conversation", channelAccountId, payload.chat_id)
          );
          api.logger.info(
            `ai4all bridge shadow trace enabled account=${channelAccountId} run=${ctx.runId || ""}; allowing OpenClaw native run`
          );
          return;
        }
        if (result?.no_reply) {
          return { handled: true, reply: { text: "NO_REPLY" }, reason: "ai4all_no_reply" };
        }
        const replyText = typeof result?.reply === "string" && result.reply.trim()
          ? result.reply.trim()
          : "我这边刚刚有点卡住了，你可以稍后再发我一次。";
        return {
          handled: true,
          reply: { text: replyText },
          reason: `ai4all_${result?.status || "ok"}`,
        };
      } catch (err) {
        const backendRoundtripMs = backendTurnStartedAt ? Date.now() - backendTurnStartedAt : 0;
        api.logger.error(
          `ai4all bridge failed after ${backendRoundtripMs}ms: ${err instanceof Error ? err.message : String(err)}`
        );
        return {
          handled: true,
          reply: { text: "我这边刚刚有点卡住了，你可以稍后再发我一次。" },
          reason: "ai4all_error",
        };
      }
    }, { timeoutMs: hookTimeoutMs, priority: 100 });

    api.on("llm_input", async (event, ctx) => {
      const provider = ctx.messageProvider || "";
      const channelAccountId = extractAccountId(ctx, provider);
      const state = findShadowState(
        event.runId,
        ctx.runId,
        ctx.sessionKey,
        event.sessionId,
        ctx.sessionId,
        shadowKey("account-conversation", channelAccountId, ctx.channelId),
        shadowKey("account", channelAccountId)
      );
      if (!state) {
        return;
      }
      state.trace.llm_model = event.model || [event.provider, event.model].filter(Boolean).join("/");
      state.trace.system_prompt = event.systemPrompt || "";
      state.trace.messages = compactMessages(event.historyMessages);
      if (event.prompt) {
        state.trace.messages.push({ role: "user", content: String(event.prompt), source: "llm_input.prompt" });
      }
      state.trace.metadata = {
        ...state.trace.metadata,
        openclaw_run_id: event.runId || ctx.runId,
        openclaw_session_id: event.sessionId || ctx.sessionId,
        provider: event.provider,
        model: event.model,
        prompt: event.prompt,
        images_count: event.imagesCount,
        hook_llm_input_at: new Date().toISOString(),
      };
    }, { timeoutMs: hookTimeoutMs, priority: 100 });

    api.on("agent_end", async (event, ctx) => {
      const provider = ctx.messageProvider || "";
      const channelAccountId = extractAccountId(ctx, provider);
      const state = findShadowState(
        event.runId,
        ctx.runId,
        ctx.sessionKey,
        ctx.sessionId,
        shadowKey("account-conversation", channelAccountId, ctx.channelId),
        shadowKey("account", channelAccountId)
      );
      if (!state) {
        return;
      }
      state.trace.reply = lastAssistantText(event.messages);
      state.trace.messages = state.trace.messages && state.trace.messages.length
        ? state.trace.messages
        : compactMessages(event.messages);
      state.trace.latency_ms = typeof event.durationMs === "number"
        ? event.durationMs
        : Date.now() - state.startedAt;
      state.trace.error = event.error || undefined;
      state.trace.metadata = {
        ...state.trace.metadata,
        openclaw_run_id: event.runId || ctx.runId,
        success: event.success,
        message_count: Array.isArray(event.messages) ? event.messages.length : 0,
        hook_agent_end_at: new Date().toISOString(),
      };
    }, { timeoutMs: hookTimeoutMs, priority: 100 });

    api.on("message_sending", async (event, ctx) => {
      const config = resolveConfig(api);
      const metadata = asRecord(event.metadata);
      const channelAccountId = normalizeAccountId(ctx.accountId || metadata.accountId);
      const state = findShadowState(
        ctx.runId,
        ctx.sessionKey,
        ctx.messageId,
        ctx.conversationId,
        event.to,
        shadowKey("account-conversation", channelAccountId, ctx.conversationId),
        shadowKey("account-conversation", channelAccountId, event.to),
        shadowKey("account", channelAccountId)
      );
      if (!state) {
        if (isShadowTraceAccount(config, channelAccountId)) {
          api.logger.info(
            `ai4all bridge shadow message_sending missed account=${channelAccountId || ""} conversation=${ctx.conversationId || ""} to=${event.to || ""} activeStates=${shadowRuns.size}`
          );
        }
        return;
      }
      if (state.deliveryCount > 0) {
        return {
          cancel: true,
          cancelReason: "ai4all_shadow_trace_extra_native_reply",
          metadata: {
            ai4allShadowTrace: true,
            ai4allTraceId: state.ai4allTraceId,
          },
        };
      }
      state.deliveryCount += 1;
      const openclawReply = event.content || state.trace.reply || "";
      const replyText = state.ai4allReply && state.ai4allReply.trim()
        ? state.ai4allReply.trim()
        : "我这边刚刚有点卡住了，你可以稍后再发我一次。";
      state.trace.reply = state.trace.reply || openclawReply;
      state.trace.metadata = {
        ...state.trace.metadata,
        native_reply_before_rewrite: openclawReply,
        delivered_reply_source: "ai4all",
        message_sending_to: event.to,
        message_sending_thread_id: event.threadId,
        message_sending_reply_to_id: event.replyToId,
        message_sending_metadata: metadata,
        message_sending_context: ctx,
        hook_message_sending_at: new Date().toISOString(),
      };
      try {
        await postDebugTrace(config, {
          trace_id: `openclaw-${ctx.runId || state.runId || state.messageId || Date.now()}`,
          channel_account_id: state.channelAccountId,
          account_id: state.channelAccountId,
          channel: state.channel,
          session_key: state.sessionKey,
          message_id: state.messageId,
          source: "openclaw",
          llm_model: state.trace.llm_model,
          system_prompt: state.trace.system_prompt,
          messages: state.trace.messages || [],
          reply: state.trace.reply || openclawReply,
          metadata: state.trace.metadata,
          latency_ms: state.trace.latency_ms,
          error: state.trace.error,
        });
        state.tracePosted = true;
        api.logger.info(
          `ai4all bridge shadow trace posted account=${state.channelAccountId} trace=${state.ai4allTraceId || ""} openclawReplyChars=${openclawReply.length}`
        );
      } catch (err) {
        api.logger.error(`ai4all bridge failed to post OpenClaw debug trace: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setTimeout(() => forgetShadowState(state), 5000);
      }
      return {
        content: replyText,
        metadata: {
          ai4allShadowTrace: true,
          ai4allTraceId: state.ai4allTraceId,
          originalOpenClawReplyLength: openclawReply.length,
        },
      };
    }, { timeoutMs: hookTimeoutMs, priority: 1000 });
  },
});
