import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_SECRET = "dev-secret";
const DEFAULT_TIMEOUT_MS = 8000;
const DEFAULT_ONLY_CHANNEL = "openclaw-weixin";

function asRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function resolveConfig(api) {
  const cfg = asRecord(api.config);
  return {
    backendUrl: String(cfg.backendUrl || process.env.AI4ALL_BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/+$/, ""),
    secret: String(cfg.secret || process.env.AI4ALL_BRIDGE_SECRET || DEFAULT_SECRET),
    timeoutMs: Number(cfg.timeoutMs || process.env.AI4ALL_BRIDGE_TIMEOUT_MS || DEFAULT_TIMEOUT_MS),
    onlyChannel: String(cfg.onlyChannel || process.env.AI4ALL_ONLY_CHANNEL || DEFAULT_ONLY_CHANNEL),
  };
}

function buildSessionParts(sessionKey) {
  const raw = typeof sessionKey === "string" ? sessionKey : "";
  return {
    sessionKey: raw || undefined,
    senderId: raw || undefined,
    chatId: raw || undefined,
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

export default definePluginEntry({
  id: "ai4all-openclaw-bridge",
  name: "AI4ALL OpenClaw Bridge",
  description: "Routes OpenClaw chat turns to the AI4ALL backend and returns a synthetic reply.",
  register(api) {
    api.logger.info("ai4all bridge registered before_agent_reply hook");

    api.on("before_agent_reply", async (event, ctx) => {
      const config = resolveConfig(api);
      const provider = ctx.messageProvider || "";
      const channel = provider || ctx.channelId || "";
      if (config.onlyChannel && provider && provider !== config.onlyChannel) {
        return;
      }

      const session = buildSessionParts(ctx.sessionKey);
      const payload = {
        event_id: ctx.runId || undefined,
        message_id: ctx.runId || undefined,
        channel: channel || config.onlyChannel,
        account_id: provider || undefined,
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
        },
      };

      try {
        api.logger.info(`ai4all bridge forwarding turn channel=${payload.channel} session=${payload.session_key}`);
        const result = await postTurn(config, payload);
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
  },
});
