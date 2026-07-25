# 项目现状与近期方向

更新时间：2026-07-25

> 本文是**持续更新**的项目状态入口，回答"我们现在在哪、当前重点是什么、还剩哪些大块"。它取代了原 `phase1/next_dev_steps.md`，并承载原 Phase 1 收尾总结里"还剩什么"的活的部分。
>
> 分工：稳定层看 [`roadmap.md`](roadmap.md)（产品愿景/原则/暂不做）、[`architecture/overview.md`](architecture/overview.md)（总体架构）、[`architecture/system_design.md`](architecture/system_design.md)（详细技术平面）和各 `architecture/designs/` / `product/` 专题；**易变的现状和近期队列只在本文维护**。需求-代码逐条映射的历史快照见 [`archive/phase1/phase1_traceability_matrix.md`](archive/phase1/phase1_traceability_matrix.md)。

## 1. 我们在哪

- Phase 1 的基础功能（注册扫码接入、账号隔离、陪伴聊天、记忆、提醒、主动消息、同步搜索、语音文本链路、贝壳计费底座）已经完成并上线内测，项目进入 **2.x 阶段**。
- 多产品 Phase 1（membership、session、计费、配额、邀请隔离）已完成双后端验收并生产发布；
  目录边界实体化正在按独立提交推进，Fatetell 仍等待 PRD 冻结，不创建占位产品实现。
- "Phase 1" 现在是一个**已完成的历史里程碑**，不再是文档的组织轴。当时的完整状态快照冻结在 [`archive/phase1/phase1_closeout_summary.md`](archive/phase1/phase1_closeout_summary.md)（截至 2026-06-07，全量 424 passed）。
- 当前是**持续开发**模式，不再做里程碑式工作包规划；重点从"补齐基础功能"转向**架构完善、新功能补充和效果调优**。

## 2. 当前重点方向

1. **架构完善** — 完成朝夕产品、Agent Runtime 与共享 Platform 的物理边界；收口仍混合的
   legacy API/tool composition；DB 与文件系统解耦另行设计。多机接入（central/node）已上线，
   继续硬化部署与观测。
2. **新功能补充** — 拉新送贝壳闭环已落地，继续补运营复核体验；权益扣减收口、图片理解转正等（见 §3）。
3. **效果调优** — 主动消息/内容邀请/陪伴跟进在真实数据上调 prompt、阈值与风控；陪伴质量回归集；默认 prompt 与人设。

## 3. 已知大缺口（按建议起点排序）

来自 closeout §3/§5，并按 2.x 现状校正。详细字段设计见对应专题文档。

1. **拉新送贝壳闭环（已落地，继续观察）** — 已实现个人邀请码、邀请码预览/注册带入、邀请关系记录、绑定后 3 条有意义消息启发式判定、邀请人 1000 贝壳奖励、奖励幂等和后台查看；同一邀请人 7 天内超过 5 个成功注册后，第 6 个起达标奖励进入软风控延迟发放，默认 3 天后释放。剩余：运营人工复核 UI、风控原因分层和真实数据阈值调参。
2. **权益扣减收口** — 商业搜索成功固定扣 5 贝壳**未接线**（`web_search` 当前不扣费且默认关闭）；模型价格倍率恒传 1.0x、`model_price_rules` 未落地；基准模型仍硬编码、未对齐 PRD 口径；运营补发/冲正入口与客服记录未做。
3. **图片理解转正** — 能力已提前交付并上线。部署脚本已去硬编码（`deploy_image_understanding.sh` 按内容自发现 bundle/dist/node，2026-06-13 修复），两份文档也已去掉"草稿"标注。**仍在的缺口**：生产依赖手术式改运行中 OpenClaw dist（升级仍有静默退回空文本风险，回归清单待补）+ 真图质量回归。见 [图片理解设计](architecture/designs/image_understanding_design.md)、[OpenClaw 补丁与部署机制](architecture/designs/openclaw_patches_maintenance.md)。
4. **主动消息观察期收口** — 周期提醒基础版已实现（`recur_rule` daily/weekly/monthly，含每周期独立幂等键修复）；自然语言取消/更新已有工具（`cancel_reminder`/`update_reminder` 已注册，直接执行，仍缺二次确认交互）；仍剩用户级 timezone 真正接线（列已建但调度未消费）、多实例 scheduler lease、真实端到端调参。**关键约束（2026-07-08）**：微信对沉默联系人有一个我方无法绕过的送达窗口（官方口径 24 小时，超窗静默拒收），详见 [`proactive_prd.md` §9](product/proactive_prd.md) 与 [`weixin_context_token_send_semantics.md`](troubleshooting/weixin_context_token_send_semantics.md)。已落地 `get_account_touch_state()` 并接入全部主动消息触发点：陪伴跟进/内容邀请/拉活在候选生成前阻断，**用户提醒/承诺在触发前阻断（2026-07-08 追加，反转此前"提醒必达、仍照常尝试发送"的决策）**——一次性提醒/承诺过期直接终态 `cancelled`，周期提醒跳过本次正常推进到下一周期；剩余缺口是事后告知（用户下次开口时知会"之前有条提醒因为太久没聊天没发出去"），跟踪在临时文档 P3。对应的可观测性修复（假成功→显式失败）已手术式打在 aliyun1 但未固化为正式补丁、aliyun2 未确认，见 [`openclaw_patches_maintenance.md` §2.7](architecture/designs/openclaw_patches_maintenance.md)。已确认的应对方案与开发跟进见 [`主动消息送达窗口对齐.md`](plans/主动消息送达窗口对齐.md)。
5. **内测部署硬化** — **已落地**：PostgreSQL 迁移（aliyun1+aliyun2 自 2026-06-21 全量切 PG，`app/db/_backend.py` 双后端垫片）、独立 scheduler worker（`scripts/run_proactive_scheduler.py`，含 dreaming 编排与时区校验）、飞书告警体系（`app/platform/observability/alerting.py`，ERROR 日志脱敏+冷却，main 与 scheduler 双处挂载）、per-turn trace_id 与计时结构化行。**仍在的缺口**：Redis 迁移（当前无 redis 依赖）、全局结构化日志框架、用户级 timezone 真正接线、多实例 scheduler lease（现仅行级 `claim_due_*` 幂等、无调度器租约）。

## 4. 待跟进的架构设计

- **DB 解耦 / 文件系统是否同样处理** — 现有排查已完成（DB 耦合面、FS 集中度、迁移机制、风险排序），**设计尚未开展**。排查结论见 [数据库与文件系统解耦调研](architecture/designs/database_filesystem_decoupling_research.md)，后续单独立项跟进。

## 5. 暂不做 / 后置

口径与 [roadmap 暂不做](roadmap.md) 一致：客服号模型、群聊 bot、图片生成、心理咨询工作流、多 Agent 工作流、小程序/H5 官方登录、支付与自动续费（正式后置）、后端 ASR fallback。100k DAU 扩展等前瞻草稿在 [`backlog/`](backlog/)。

## 维护规则

- 现状、近期队列、缺口状态变化，更新本文并刷新顶部日期。
- 长期有效的产品/技术结论沉淀回 `roadmap.md` / `architecture/overview.md` / `architecture/system_design.md` 或对应专题，不堆在本文。
- 一次性已完成的执行清单、阶段性收尾快照，归档到 `archive/`，不在本文保留长尾完成日志。
