# Roadmap

## Direction

The project should continue from a simple verified WeChat AI companion into an extensible personal AI companion and life assistant platform.

Near-term priority is not broad SaaS complexity. The immediate goal is to make one operated WeChat account reliably serve many private users, while keeping the architecture ready for multiple WeChat/OpenClaw instances later.

## Product Principles

- Private chat first.
- Emotional companionship and life assistance first.
- Keep psychological counseling out of early versions.
- Keep user configuration simple and operator-controlled at first.
- Prefer a stable small system over many partially-working features.

## Phase 1: Stabilize Multi-User Private Chat

Goal: one WeChat account can serve multiple users reliably.

Planned work:

- Harden Admin API.
- Add simple Admin UI.
- Improve profile editing.
- Add contact search and filters.
- Add session/message pagination.
- Add message delivery/failure status.
- Add better duplicate/retry handling around OpenClaw events.
- Add basic usage statistics per account/contact/session.

Success criteria:

- Operators can see users, sessions, and messages.
- Operators can disable users.
- Operators can adjust user style/prompt.
- Each user gets isolated context.
- Failures can be diagnosed from logs and Admin API.

## Phase 2: Voice Support

Voice is still in Phase 1 product scope, but should be implemented after the text flow is stable.

Planned work:

- Understand actual media payload from `openclaw-weixin`.
- Decide ASR path:
  - local conversion with `ffmpeg`, then ASR
  - or cloud ASR on media URL/binary
- Add `message_type=voice` handling.
- Store voice metadata.
- Return text replies first.
- Later consider voice replies/TTS.

Success criteria:

- User can send a WeChat voice message.
- Backend transcribes it.
- LLM replies in text.

## Phase 3: Deployment

Goal: reproducible deployment on Aliyun.

Planned work:

- Dockerfile for backend.
- docker-compose for backend + persistent volume.
- Production `.env` template.
- Health checks.
- Log path and retention.
- Decide SQLite vs PostgreSQL.
- Bridge backend URL and secret configuration for cloud.
- OpenClaw runtime strategy:
  - host process
  - or Docker container with persistent state

Success criteria:

- Fresh server can be provisioned from docs.
- Backend survives restart.
- OpenClaw reconnect flow is documented.
- Secrets are not committed.

## Phase 4: Admin UI

Goal: operators can manage users without curl.

Minimum UI:

- Account list.
- Contact list.
- Contact detail.
- Session/message viewer.
- Profile editor.
- Enable/disable controls.
- Reset session button.

This can be a simple server-rendered HTML page first. A full frontend app is not required immediately.

## Phase 5: Better Conversation Quality

Goal: improve companion/life assistant value.

Planned work:

- Better default prompt.
- More structured profile fields.
- Style presets.
- Basic user preference extraction.
- Summary-based session memory.
- Safety boundaries for medical/legal/psychological topics.
- Evaluation prompts and test conversation sets.

## Phase 6: Multiple WeChat Entrypoints

Goal: support multiple operated WeChat accounts/OpenClaw instances.

Planned work:

- Account-level config.
- Per-account bridge secret.
- Per-account prompt defaults.
- Account health and login state tracking.
- QR/login state reporting from Bridge if available.
- Deployment pattern for multiple OpenClaw instances.

This is different from full self-serve SaaS. Self-serve user-owned WeChat hosting should remain a later decision.

## Not Now

- Full public SaaS onboarding.
- Payment.
- Group chat.
- Image/multimodal.
- Long-term memory productization.
- Psychology/counseling workflows.
- Multi-agent workflows.
