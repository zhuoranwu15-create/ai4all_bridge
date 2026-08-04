# 朝夕产品架构

归属：`product:zhaoxi`。本目录描述朝夕独有的微信陪伴、使命与关系、主动消息、运营活动、
用户元属性和语音输入等领域设计。

产品需求与渠道/API 交接见 [`../../../products/zhaoxi/`](../../../products/zhaoxi/README.md)。
共享能力应引用 `../../shared/`；Runtime 接缝应引用 `../../agent-runtime/`，不得把朝夕概念下沉到其中。

原 Companion World 设计文件在文档迁移完成前暂留本目录，但自 2026-08-04 起其 owner 已是
[`product:mingchan`](../mingchan/README.md)，不再指导朝夕新增实现。

当前关键设计：

- [主动消息与提醒](proactive_messaging_design.md)
- [关系状态](relationship_state_design.md)
- [使命与自我状态编排](agent_mission_and_orchestration_design.md)
- [用户自建角色模板与邀请链接](creator_role_template_referral_link_technical_design.md)
