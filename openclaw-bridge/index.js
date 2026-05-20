import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_SECRET = "dev-secret";
const DEFAULT_TIMEOUT_MS = 8000;
const DEFAULT_ONLY_CHANNEL = "openclaw-weixin";
const SHADOW_STATE_TTL_MS = 10 * 60 * 1000;

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
  };
}

function parseCsvSet(value) {
  return new Set(String(value || "").split(",").map((item) => item.trim()).filter(Boolean));
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
    const shadowAccountCount = parseCsvSet(initialConfig.shadowTraceAccountIds).size;
    api.logger.info(
      `ai4all bridge registered hooks backend=${initialConfig.backendUrl} onlyChannel=${initialConfig.onlyChannel} shadowTraceAccountCount=${shadowAccountCount}`
    );
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
        message_type: "text",
        text: event.cleanedBody || "",
        timestamp: Math.floor(Date.now() / 1000),
        raw: {
          ctx,
          event,
          ai4all_bridge: {
            account_candidates: accountCandidates,
            resolved_account_id: channelAccountId,
            channel_account_id: channelAccountId,
          },
        },
      };

      try {
        api.logger.info(
          `ai4all bridge forwarding turn channel=${payload.channel} session=${payload.session_key} candidates=${JSON.stringify(accountCandidates)}`
        );
        const result = await postTurn(config, payload);
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
        api.logger.error(`ai4all bridge failed: ${err instanceof Error ? err.message : String(err)}`);
        return {
          handled: true,
          reply: { text: "我这边刚刚有点卡住了，你可以稍后再发我一次。" },
          reason: "ai4all_error",
        };
      }
    }, { timeoutMs: DEFAULT_TIMEOUT_MS, priority: 100 });

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
    }, { timeoutMs: DEFAULT_TIMEOUT_MS, priority: 100 });

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
    }, { timeoutMs: DEFAULT_TIMEOUT_MS, priority: 100 });

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
    }, { timeoutMs: DEFAULT_TIMEOUT_MS, priority: 1000 });
  },
});
