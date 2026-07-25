# 跟进：绑定后 bot 主动问候（wxid 回填）

> 状态：🔴 待排期（2026-07-09 立项）
> 决策来源：营销闭环走查，用户选择「先接受用户先开口，wxid 回填另立跟进项」。
> 归属：onboarding / 绑定链路 / 主动出站

## 1. 现象与问题

「官网注册 → 微信扫码绑定完成 → bot 主动先打招呼」这一体验目前**不稳定**。

第一条破冰欢迎语 `ONBOARDING_WELCOME_TEXT`（`app/onboarding.py:42`，"你好，很高兴能成为微信好友，你希望我怎么称呼你？"）有两条触发路径：

- **Path A（理想：绑定完成即主动问候）** — `app/routers/web.py:312-323`，绑定完成后 `loop.call_later(5.0, ...)` 调 `_send_onboarding_welcome_if_pending`（`web.py:329-397`）。
  - **常失效**：微信绑定阶段只拿得到扫码时的 bot 账号 ID，拿不到用户 wxid（`sender_id`），`to_user_id` 为空 → 直接跳过（`web.py:365-371`）。
- **Path B（实际生效：用户先开口）** — `app/turn_service.py:888-922`。用户发第一条消息、`onboarding_state == ONBOARDING_PENDING` 且渠道 `openclaw-weixin` 时，才发欢迎语并推进到 `STEP1_SENT`，返回 `no_reply=True` 吸收首条消息。

两路共用幂等键 `onboarding-welcome-{account_id}`，不会重复。欢迎语是保必发常量，不过 proactive 政策/配额闸门。

**当前接受的行为**：用户扫码后需先发一句话，bot 才走 onboarding（Path B）。可用，但少了「bot 主动先问候」的惊喜感。

## 2. 待回答的问题

**wxid 回填能否让 Path A 稳定生效，实现「绑定完成即主动问候」？**

需要调研：
1. OpenClaw 绑定回调链路里，扫码成功后是否有任何时机能拿到用户（联系人）wxid？
   - 入口：`_complete_binding_intent_from_wait_result`（`web.py:179`）、`_wait_for_binding_intent`（`web.py:278`）、`_start_openclaw_qr_for_binding`（`web.py:415`）、`app/openclaw_gateway.py`。
2. 若绑定当下拿不到，微信侧首次收到用户任意事件（加好友通过、首条消息前的系统事件）时能否回填 wxid → 触发一次补发。
3. 回填后 Path A 与 Path B 的幂等与竞态：两路已共用幂等键，需确认补发不会与 Path B 抢跑或重复。
4. **微信 24h 送达窗口约束**（`app/products/zhaoxi/proactive/delivery/touch_state.py`）：主动消息要求联系人 24h 内有入站；若用户从未入站，`touch_state` 判 STALE，主动消息发不出。需确认「绑定后主动问候」是否属于欢迎语的保必发豁免路径（`enqueue_onboarding_welcome` 声明保必发，不过 touch_state），还是仍受窗口限制——这决定 wxid 回填是否真能突破「零互动用户」。

## 3. 候选方案（待评估，勿直接实现）

- **A. wxid 回填补发**：在绑定回调或首个微信事件里回填 `to_user_id`，回填成功后触发一次 `_send_onboarding_welcome_if_pending`。依赖 §2.1/§2.2 的可行性。
- **B. 维持现状（Path B）**：不改，接受「用户先开口」。零成本，当前默认。
- **C. 前端引导**：注册成功页明确提示「扫码加好友后，发一句 hi 开始聊天」，用产品话术补齐体验缺口，规避技术改造。

## 4. 关联

- 主动出站与 touch_state 闸门总览见 `app/products/zhaoxi/proactive/delivery/`。
- 营销活码闭环（本次走查的其余部分）已通过纯配置打通，不依赖本项。
