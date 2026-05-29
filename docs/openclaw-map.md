# OpenClaw Reference Map

Quick navigation for borrowing/referencing OpenClaw concepts.
Repo root: `/Users/suchong/workspace/openclaw/`

## Top-level layout

```
src/          Core TypeScript (channels, memory, gateway, agents, sessions…)
extensions/   Plugins/providers (anthropic, bedrock, browser, active-memory…)
packages/     SDK packages (plugin-sdk, sdk, memory-host-sdk)
ui/           Web UI (React)
apps/         App shells
docs/         User-facing docs
```

## Modules most relevant to AI4ALL

### Message flow (how a user message becomes a bot reply)

| Path | What it does |
|---|---|
| `src/channels/channels/inbound-event/` | Classify and parse an inbound event (message, media, reaction…) |
| `src/channels/channels/session.ts` + `session.types.ts` | Session envelope: who sent it, which channel, thread binding |
| `src/channels/channels/turn/kernel.ts` | **Core turn loop** — the main "receive → process → reply" cycle |
| `src/channels/channels/turn/history-window.ts` | How history is windowed before sending to the model |
| `src/channels/channels/turn/durable-delivery.ts` | Reliable message delivery with retry |
| `src/channels/channels/draft-stream-loop.ts` | Streaming reply draft loop |
| `src/channels/channels/typing.ts` | Typing indicator lifecycle |

### Memory & context

| Path | What it does |
|---|---|
| `src/memory/root-memory-files.ts` | Memory file resolution (SOUL/IDENTITY/USER style files) |
| `src/context-engine/init.ts` | Context assembly entry point |
| `src/context-engine/delegate.ts` | Context delegation / plugin contributions |
| `extensions/active-memory/` | Active memory plugin (the LLM-driven memory layer) |

### Agent & session lifecycle

| Path | What it does |
|---|---|
| `src/gateway/boot.ts` | Gateway startup / initialization |
| `src/gateway/auth.ts` + `auth-*.ts` | Auth resolution (token, mode, rate-limit) |
| `src/gateway/agent-prompt.ts` | How the system prompt is assembled per agent |
| `src/sessions/` | Session lifecycle (create, attach, teardown) |
| `src/agents/` | Agent definitions and runtime |

### Channel setup & health

| Path | What it does |
|---|---|
| `src/flows/channel-setup.ts` | First-time channel setup flow |
| `src/flows/doctor-*.ts` | Health checks and auto-repair |
| `src/flows/provider-flow.ts` | Provider selection / setup flow |

### Plugin / extension structure

| Path | What it does |
|---|---|
| `src/plugin-sdk/` | Public SDK for writing plugins |
| `extensions/anthropic/` | Anthropic provider (model calls, streaming, tool use) |
| `extensions/active-memory/` | Memory plugin (read/write long-term memory items) |

## How to explore deeper

```bash
# Find all files related to a concept
find /Users/suchong/workspace/openclaw/src/channels -name "*.ts" | grep -v test | grep -v ".d.ts"

# Search for a specific pattern
grep -r "durable-delivery" /Users/suchong/workspace/openclaw/src --include="*.ts" -l
```

## Key design principles (from CLAUDE.md)

- **Channels are implementation**; plugin authors get SDK seams only
- **Hot paths carry prepared facts** — don't re-discover provider/model/channel at request time
- **Gather → normalize → decide → act** — each layer has one job
- **Core stays plugin-agnostic** — plugins cross into core only via `plugin-sdk/*`
