# 项目现状与近期方向

更新时间：2026-06-15

> 本文是**持续更新**的项目状态入口，回答"我们现在在哪、当前重点是什么、还剩哪些大块"。它取代了原 `phase1/next_dev_steps.md`，并承载原 Phase 1 收尾总结里"还剩什么"的活的部分。
>
> 分工：稳定层看 [`roadmap.md`](roadmap.md)（产品愿景/原则/暂不做）、[`architecture_overview.md`](architecture_overview.md)（总体架构）、[`system_design.md`](system_design.md)（详细技术平面）和各 `tech_design/` / `product/` 专题；**易变的现状和近期队列只在本文维护**。需求-代码逐条映射的历史快照见 [`archive/phase1/phase1_traceability_matrix.md`](archive/phase1/phase1_traceability_matrix.md)。

## 1. 我们在哪

- Phase 1 的基础功能（注册扫码接入、账号隔离、陪伴聊天、记忆、提醒、主动消息、同步搜索、语音文本链路、贝壳计费底座）已经完成并上线内测，项目进入 **2.x 阶段**。
- "Phase 1" 现在是一个**已完成的历史里程碑**，不再是文档的组织轴。当时的完整状态快照冻结在 [`archive/phase1/phase1_closeout_summary.md`](archive/phase1/phase1_closeout_summary.md)（截至 2026-06-07，全量 424 passed）。
- 当前是**持续开发**模式，不再做里程碑式工作包规划；重点从"补齐基础功能"转向**架构完善、新功能补充和效果调优**。

## 2. 当前重点方向

1. **架构完善** — DB 访问与主服务解耦的排查已完成、设计待跟进（见 §4）；多机接入（central/node）已上线，按账号节点感知分发；继续硬化部署与观测。
2. **新功能补充** — 拉新送贝壳闭环（最大缺口）、权益扣减收口、图片理解转正等（见 §3）。
3. **效果调优** — 主动消息/内容邀请/陪伴跟进在真实数据上调 prompt、阈值与风控；陪伴质量回归集；默认 prompt 与人设。

## 3. 已知大缺口（按建议起点排序）

来自 closeout §3/§5，并按 2.x 现状校正。详细字段设计见对应专题文档。

1. **拉新送贝壳闭环（最大缺口，未动工）** — 底座已就绪（钱包/流水/cost_events、注册赠 1000 贝壳、聊天按 token 扣减）；完全未实现：邀请码生成校验、邀请关系记录（`referral_codes` / `referral_relationships` 表未建）、"新用户发 3 条有意义信息→判定→给邀请人发奖励"链路、反作弊。口径见 [权益 PRD §5](product/entitlement_growth_prd.md)。
2. **权益扣减收口** — 商业搜索成功固定扣 5 贝壳**未接线**（`web_search` 当前不扣费且默认关闭）；模型价格倍率恒传 1.0x、`model_price_rules` 未落地；基准模型仍硬编码、未对齐 PRD 口径；运营补发/冲正入口与客服记录未做。
3. **图片理解转正** — 能力已提前交付并上线，但生产依赖手术式改运行中 OpenClaw dist，**升级会静默退回空文本**且部署脚本写死哈希文件名；需稳定化部署（去硬编码、补升级回归清单）+ 真图质量回归，两份文档去掉"草稿"标注。见 [图片理解设计](tech_design/image_understanding_design.md)、[OpenClaw 补丁与部署机制](tech_design/openclaw_patches_maintenance.md)。
4. **主动消息观察期收口** — 周期提醒基础版已实现（`recur_rule` daily/weekly/monthly，含每周期独立幂等键修复）；仍剩自然语言取消/更新确认、用户级 timezone、多实例 scheduler lease、真实端到端调参。
5. **内测部署硬化** — PostgreSQL/Redis 迁移、独立 scheduler worker 生产化、结构化日志/trace id/告警体系。

## 4. 待跟进的架构设计

- **DB 解耦 / 文件系统是否同样处理** — 现有排查已完成（DB 耦合面、FS 集中度、迁移机制、风险排序），**设计尚未开展**。排查记录见 [`tmp/db_fs_decoupling_investigation.md`](tmp/db_fs_decoupling_investigation.md)，后续单独立项跟进。

## 5. 暂不做 / 后置

口径与 [roadmap 暂不做](roadmap.md) 一致：客服号模型、群聊 bot、图片生成、心理咨询工作流、多 Agent 工作流、小程序/H5 官方登录、支付与自动续费（正式后置）、后端 ASR fallback。100k DAU 扩展等前瞻草稿在 [`backlog/`](backlog/)。

## 维护规则

- 现状、近期队列、缺口状态变化，更新本文并刷新顶部日期。
- 长期有效的产品/技术结论沉淀回 `roadmap.md` / `architecture_overview.md` / `system_design.md` 或对应专题，不堆在本文。
- 一次性已完成的执行清单、阶段性收尾快照，归档到 `archive/`，不在本文保留长尾完成日志。
