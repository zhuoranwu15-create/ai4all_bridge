# Companion World App 后续项

更新时间：2026-07-31

状态：**待决策 / 待排期 / 待运维闭环**

来源：已上线的 M1、M2、v1.5 服务端交付计划。本文只承接真实剩余项，不作为当前 API 或产品行为的
权威说明。当前行为分别以
[`companion_world_app_prd.md`](../../../products/zhaoxi/capabilities/companion_world_app_prd.md)、
[`app_api_handoff.md`](../../../products/zhaoxi/app_api_handoff.md) 和提交的 OpenAPI snapshot 为准。

## 1. 当前需要先处理

| ID | 事项 | 当前事实与下一步 | Owner / 验收 |
| --- | --- | --- | --- |
| APP-OPS-001 | 司辰头像同步到生产 CDN | 本仓 `app/static/companion_world/avatars/sichen.png` 已存在；2026-07-31 对 `https://ai4company.top/companion_world/avatars/sichen.png` 的只读核验返回 `200 text/html`，不是图片。需同步 CDN 资产并排除站点 fallback | 运维；响应为图片 Content-Type，内容可正常解码，候选卡片不再 404/误收 HTML |
| APP-LEGAL-001 | 注销后的第三方相关数据处置 | 当前刻意保留真人会话、来访/邀请记录、财务审计及被第三方会话引用的自发媒体，避免删除对方仍需读取的副本。需由产品与法务冻结留存期限、用户告知和删除/匿名化规则 | 产品 + 法务；PRD、隐私协议、服务端行为一致并有迁移/清理验收 |

## 2. 已明确后置，等待产品排期

| ID | 事项 | 已有决定 / 开工前置 |
| --- | --- | --- |
| M5-NOTIFY-001 | 真人会话通知 | **采用方案 1：明确后置。** 当前没有真人消息站内通知或 Push 投递链路，不先增加会话静音 DTO 等无效契约。只有在投递面、默认策略、红点与免打扰范围冻结后再立项 |
| MEDIA-RETRACT-001 | 撤回/删除已发送媒体消息 | v1.5 明确不做。需同时设计消息终态、双方可见性、媒体物理删除时机、审核下架和审计保留 |
| CONTENT-003 | 角色任务章节 | 不是给现有 mission 加字段即可完成；需先冻结 resident 维度的任务归属、章节模型、不可变规则和 App 读取契约 |
| ROLE-201 | 候选角色示例对话 `sample_dialogue` | M1 只交付 `long_summary`。若继续做，需先定义内容审核、版本管理和客户端展示契约；未确认价值前不加字段 |
| PRESENTATION-201 | 对外公开的居民展示摘要 | 需先定义哪些内容是 owner 审核过、可向访客公开的投影；禁止直接 join 私有 profile 作为捷径 |

## 3. 按触发条件再立项

| ID | 事项 | 触发条件 / 边界 |
| --- | --- | --- |
| CONFIG-201 | 分平台灰度与维护态 | 当前只有 `minimum_supported_version_by_platform`。出现真实分平台灰度、强制维护或分批放量需求时，先设计配置优先级和客户端失败语义 |
| FEED-RESTORE-001 | 取消隐藏与运营查看隐藏动态 | M2 仅支持主人隐藏 AI 动态，不支持取消隐藏；数据和审计原因仍保留。出现用户恢复或运营检索诉求时，设计 owner-scoped、可审计接口 |
| CONTRACT-201 | 补齐 App 路由响应模型 | M1 主链路、Feed、真人会话列表/举报选项已逐步冻结；visits、mailbox 等路由族仍需按批次补 `response_model` 和 snapshot 键集门禁 |
| MEDIA-STORAGE-201 | 对象存储 / CDN 化上传媒体 | 当前本地存储抽象保留 `storage_path`。上传带宽、磁盘或应用进程内存成为瓶颈时再迁移，并补跨节点读取与删除一致性设计 |
| PERSONA-SEED-201 | persona seed 显示名占位 | 当前内容用“名字由用户决定”的写法规避模板自称写死。只有内容规模证明人工纪律不足时再设计 `{display_name}` 替换 |
| DESIGN-201 | 司辰头像统一正面构图 | 当前侧脸图技术可用；是否与其余四位统一为正面、居中、半身由设计决定，不阻塞服务端 |

## 4. 已从遗留项关闭

| 原事项 | 关闭依据 |
| --- | --- |
| FEED-201 主人删除/隐藏动态 | M2 已上线删除自己的文字动态、隐藏 AI 动态；farewell 明确不可隐藏 |
| MEDIA-201 Feed 与会话媒体 | v1.5 已上线图片/语音聊天与 Feed 图片，生产能力位已开启 |
| Q15 生产打开语音输入 | 2026-07-31 生产 `/app/config` 核验为 `true` |
| M5-REPORT-001 / M5-CONV-001 | 举报原因契约和真人会话读模型已交付 |
| 司辰头像本仓资产 | 512×512 优化图已入本仓；仅生产 CDN 同步仍见 APP-OPS-001 |
