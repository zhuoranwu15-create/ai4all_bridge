"""朝夕 App 主链路的**冻结响应契约**（CONTRACT-001）。

这里的模型是客户端 OpenAPI snapshot 的唯一来源：`scripts/export_openapi.py` 导出的
`docs/products/zhaoxi/openapi/app_v1.json` 完全由它们决定，CI 断言导出结果与提交的
snapshot 一致。**改响应字段必须同时改这里并重新导出 snapshot**，否则 CI 红。

两条硬约定：

1. FastAPI 的 ``response_model`` 会按模型**过滤**未声明字段——模型漏写一个字段就会静默
   丢数据。`tests/test_app_openapi_contract.py` 拿真实响应体逐层比对声明 schema 的键集，
   多一个少一个都失败，把「静默丢字段」变成「测试红」。
2. 模型只描述**已经在返回的**形状，不在这里顺手改契约。契约变更走产品批次。
"""
from __future__ import annotations

from typing import Annotated, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field

# --- B 类统一信封 ---------------------------------------------------------

DataT = TypeVar("DataT")


class WorldEnvelope(BaseModel, Generic[DataT]):
    """世界类端点的成功信封（见交接文档 §2.2 B 类）。"""

    code: str = Field(examples=["ok"])
    request_id: str
    server_time: str
    data: DataT


class WorldErrorEnvelope(BaseModel):
    """世界类端点的失败信封：无 ``data``，``message`` 恒为 null，分支只看 ``code``。"""

    code: str
    request_id: str
    server_time: str
    message: Optional[str] = None


# --- A 类：/app/config ----------------------------------------------------


class AppConfigCaptcha(BaseModel):
    provider: str
    scene_id: str
    prefix: str
    configured: bool


class AppConfigFeatures(BaseModel):
    """公开能力位。新增 capability 必须在此登记，否则响应里会被过滤掉。"""

    voice_input: bool
    resident_world: bool
    world_feed: bool
    app_notifications: bool
    resident_lifecycle: bool
    mailbox: bool
    world_visits: bool
    human_chat_send: bool
    # v1.5（FLAG-001）：客户端按**可选**读取以下四位，缺失即视为关闭，不进启动必需字段校验。
    chat_image_message: bool
    chat_voice_message: bool
    feed_image_post: bool
    resident_wish_create: bool


class AppConfigLimits(BaseModel):
    message_chars: int
    # audio_* 是**语音输入转写（ASR）**的上限，沿用既有值不动；下面 voice_* 才是语音消息上限。
    # 两个口径分离：AAC-LC 32kbps 60 秒仅约 240KB，语音消息沿用 ASR 的 10MB 是错误的宽松。
    audio_bytes: int
    audio_duration_ms: int
    # v1.5 媒体消息限额（MEDIA-LIMIT-001）。
    image_bytes_max: int
    image_count_max: int          # 动态单条上限；聊天图片消息恒为 1 张
    voice_bytes_max: int
    voice_duration_ms_max: int
    # v1.5 许愿创建（WISH-001）：一句话许愿的字数上限与每人每日许愿次数上限。
    # ``wish_daily_max <= 0`` 表示服务端未设限，客户端不必自行做次数拦截。
    wish_text_chars: int
    wish_daily_max: int


class AppConfigMinimumVersionByPlatform(BaseModel):
    ios: str
    android: str


class AppConfigResponse(BaseModel):
    captcha: AppConfigCaptcha
    features: AppConfigFeatures
    limits: AppConfigLimits
    client_contract_version: str
    server_time: str
    minimum_supported_version: str
    minimum_supported_version_by_platform: AppConfigMinimumVersionByPlatform


# --- A 类：/me ------------------------------------------------------------


class PublicAccount(BaseModel):
    id: str
    status: Optional[str] = None
    ai_display_name: str
    ai_subtitle: str


class MePlatformUser(BaseModel):
    """真人公开身份。``display_name``/``avatar_key`` 未设置时为 null，客户端自行兜底展示。"""

    id: str
    phone_masked: str
    display_name: Optional[str] = None
    avatar_key: Optional[str] = None
    avatar_ref: Optional[str] = None


class MeWorld(BaseModel):
    id: str
    status: str
    onboarding_state: str


class MeResponse(BaseModel):
    """``account`` 与 ``world`` 都可为 null：P1 新用户确认居民前**恒无** account。"""

    status: str
    platform_user: MePlatformUser
    account: Optional[PublicAccount] = None
    world: Optional[MeWorld] = None
    server_time: str


class ProfileAvatarOption(BaseModel):
    key: str
    avatar_ref: str


class ProfileLimits(BaseModel):
    nickname_chars: int
    nickname_min_chars: int


class ProfileOptionsResponse(BaseModel):
    """ME-01 受控取值表；头像库为空时 ``avatars`` 是空数组，客户端应隐藏头像选择器。"""

    status: str
    avatars: List[ProfileAvatarOption]
    limits: ProfileLimits


class ProfileUpdateResponse(BaseModel):
    status: str
    platform_user: MePlatformUser


class AccountDeletionRequestData(BaseModel):
    """注销流水公开视图；``purge_stats_json`` 等运营字段刻意不出现在客户端契约里。"""

    request_id: str
    status: str
    reason_code: Optional[str] = None
    executed_at: Optional[str] = None


class AccountDeletionResponse(BaseModel):
    """注销已**立即完成**。客户端收到后必须就地登出——本次 token 已在服务端失效。"""

    status: str
    request: AccountDeletionRequestData


# --- B 类 data：世界引导 ---------------------------------------------------


class WorldSummary(BaseModel):
    id: str
    status: str
    onboarding_state: str


class CandidateData(BaseModel):
    """候选居民公开字段；刻意不含 persona_seed_json 与内部 resident id。"""

    template_id: str
    template_version: str
    name: str
    avatar_ref: Optional[str] = None
    summary: Optional[str] = None
    long_summary: Optional[str] = None
    tags: List[str]
    origin: str
    status: str
    persona_key: Optional[str] = None
    suggested_display_name: Optional[str] = None
    naming_version: Optional[str] = None
    naming_status: str


class ResidentData(BaseModel):
    """owner 可见的居民字段；不暴露 runtime account id。"""

    resident_id: str
    name: str
    avatar_ref: Optional[str] = None
    status: str
    origin: str
    conversation_id: Optional[str] = None
    conversation_state: Optional[str] = None
    # CONTENT-004：使命展示形态。``countable`` 走可数进度，``narrative`` 只展示长期使命文案、
    # 不触发计数 UI。服务端持有名单，客户端不再按角色 ID 硬编码。
    mission_display: str = "countable"


class BootstrapData(BaseModel):
    world: WorldSummary
    candidates: List[CandidateData]
    existing_residents: List[ResidentData]


class CandidateListData(BaseModel):
    candidates: List[CandidateData]


class ResidentListData(BaseModel):
    residents: List[ResidentData]


# --- B 类 data：会话 -------------------------------------------------------


class ConversationResident(BaseModel):
    id: str
    name: str
    avatar_ref: Optional[str] = None
    status: str


class ConversationItem(BaseModel):
    """``sort_time`` 是排序与分页锚，``last_message_at`` 是最近一条消息时间。

    没聊过的居民 ``last_message_at``/``last_preview`` 为 null 但 ``sort_time`` 有值——
    两者刻意分开，服务端不伪造消息时间（CONV-001）。
    """

    conversation_id: str
    resident: ConversationResident
    state: str
    last_preview: Optional[str] = None
    unread: int
    last_message_at: Optional[str] = None
    sort_time: Optional[str] = None
    can_send: bool
    read_only_reason: Optional[str] = None


class ConversationListData(BaseModel):
    items: List[ConversationItem]
    next_cursor: Optional[str] = None


class TextMessageContent(BaseModel):
    """纯文本消息。存量消息（v1.5 之前的全部消息）都投影成这一支。"""

    type: Literal["text"] = "text"
    text: str = ""


class ImageMessageContent(BaseModel):
    """图片消息。

    ``url`` 是**短 TTL 签名地址**，每次读接口现签，不要持久化或跨会话复用。为 null 表示
    服务端此刻签不出（部署缺 secret，或访客拜访已结束）——按占位渲染，不要降级成文本。
    ``text`` 是用户自己写的 caption，**不含**服务端生成的图片描述（D-2 红线）。
    """

    type: Literal["image"] = "image"
    text: str = ""
    media_id: str
    url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class AudioMessageContent(BaseModel):
    """语音消息。``transcript`` 是上传时同步转写的结果，供"长按转文字"；转写失败为 null。"""

    type: Literal["audio"] = "audio"
    text: str = ""
    media_id: str
    url: Optional[str] = None
    duration_ms: Optional[int] = None
    transcript: Optional[str] = None


# D-1：``content.type`` 是**唯一权威判别字段**。客户端遇到未知 type 按占位降级，
# 不要再拿 ``message_type`` 做分支。
MessageContent = Annotated[
    Union[TextMessageContent, ImageMessageContent, AudioMessageContent],
    Field(discriminator="type"),
]


class ConversationMessageItem(BaseModel):
    """``message_type`` 与 ``text`` 是 v1.5 之前的老字段，服务端保证与 ``content`` 一致：

    - ``message_type`` **deprecated**，恒等于 ``content.type``（``audio`` 除外——历史上
      库内 ``message_type`` 用 ``voice``，这里统一投影成 ``content.type`` 的取值）；
    - ``text`` 恒等于 ``content.text``，即用户自己写的正文/caption。

    新客户端只读 ``content``。
    """

    id: int
    message_id: Optional[str] = None
    role: str
    message_type: str
    text: Optional[str] = None
    content: MessageContent
    created_at: Optional[str] = None


class ConversationMessagesData(BaseModel):
    state: str
    messages: List[ConversationMessageItem]
    next_cursor: Optional[int] = None


class ConversationReadData(BaseModel):
    conversation_id: str
    last_read_message_id: Optional[int] = None
    unread: int


class TurnReply(BaseModel):
    """``reply`` 非 null 时 ``text`` 必非空；重放回放原持久化 ``message_id``。"""

    text: str
    message_id: Optional[str] = None


class TurnData(BaseModel):
    """``no_reply=true`` 时 ``reply`` 整体为 null，不存在「有对象但 text 为 null」。"""

    reply: Optional[TurnReply] = None
    no_reply: bool
    deduplicated: bool


# --- B 类 data：世界 Feed ---------------------------------------------------


class FeedAuthor(BaseModel):
    """``type='human'`` 是主人本人，``'resident'`` 是 AI 居民；客户端据此决定删除/隐藏入口。"""

    type: str
    resident_id: Optional[str] = None
    name: Optional[str] = None
    avatar_ref: Optional[str] = None


class FeedImage(BaseModel):
    """图文动态里的一张图；``url`` 语义同 :class:`ImageMessageContent`（短 TTL 现签，可为 null）。"""

    media_id: str
    url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class FeedTextContent(BaseModel):
    """纯文字动态。AI 居民动态与 v1.5 之前的全部动态都投影成这一支。"""

    type: Literal["text"] = "text"
    text: Optional[str] = None


class FeedImageContent(BaseModel):
    """图文动态（v1.5）。

    正文属于整条动态而不属于某张图，所以 ``text`` 在这一层、``images`` 是有序列表
    （按主人发布时的排版顺序，最多 4 张）。只发图时 ``text`` 是空串。
    """

    type: Literal["image"] = "image"
    text: str = ""
    images: List[FeedImage]


# D-1 同款：``content.type`` 是唯一权威判别字段，客户端遇到未知 type 按占位降级。
FeedContent = Annotated[
    Union[FeedTextContent, FeedImageContent], Field(discriminator="type")
]


class FeedItem(BaseModel):
    post_id: str
    author: FeedAuthor
    content: FeedContent
    post_type: str
    source: str
    published_at: Optional[str] = None


class FeedListData(BaseModel):
    items: List[FeedItem]
    next_cursor: Optional[str] = None


class FeedPostData(BaseModel):
    post: FeedItem


class FeedRetireData(BaseModel):
    """下架结果。``status`` 是对外语义投影：主人删自己的是 ``deleted``，隐藏 AI 的是
    ``hidden``；``replayed=true`` 表示本次是重放，未产生新的状态变更。"""

    post_id: str
    status: str
    replayed: bool


# --- B 类 data：自建角色草稿预览 -------------------------------------------


class ResidentDraftPreviewData(BaseModel):
    """草稿预览回显（表单与许愿两条路径同形）。

    这些字段就是最终会落进居民人设的值——「所见即所存」；``draft_token`` 一次性，
    在 ``expires_at`` 前拿去确认创建。许愿路径重放同一 ``client_request_id`` 时逐字段等值。
    """

    draft_id: str
    draft_token: str
    expires_at: str
    name: str
    avatar_ref: Optional[str] = None
    relationship_display: str
    tags: List[str]
    normalized_summary: str
    ai_identity_notice: str


# --- B 类 data：媒体上传（v1.5 S1）-----------------------------------------


class MediaUploadData(BaseModel):
    """上传结果。

    ``url`` 是**短 TTL 签名地址**（D-3），客户端可直接加载但不要持久化：过期后重新读列表/详情
    会拿到新签名。``expires_at`` 是"这份未被引用的媒体什么时候被回收"，与 URL 过期是两件事。
    ``transcript`` 只在语音且转写成功时非空——转写失败不阻塞发送。
    """

    media_id: str
    kind: str
    mime: str
    bytes: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    transcript: Optional[str] = None
    expires_at: Optional[str] = None
    url: str
    url_expires_at: str


# --- B 类 data：真人一对一聊天 ---------------------------------------------


class HumanReportOption(BaseModel):
    """举报原因受控条目；``label`` 直接展示，客户端不自行翻译或发明分类。"""

    reason_code: str
    label: str
    details_required: bool


class HumanReportOptionsData(BaseModel):
    """``version`` 只在码集合或语义变化时递增，客户端可按它缓存。"""

    version: int
    options: List[HumanReportOption]


class HumanConversationItem(BaseModel):
    """会话列表条目。

    ``unread_count`` 是精确值不封顶，「99+」由客户端展示层决定。``read_only_reason``
    在 ``can_send=true`` 时为 ``None``，否则是稳定错误码，客户端按码分支不按文案。
    """

    conversation_id: str
    visit_id: str
    counterpart_display_name: Optional[str] = None
    status: str
    last_message_at: Optional[str] = None
    last_read_at: Optional[str] = None
    created_at: str
    last_preview: Optional[str] = None
    unread_count: int
    can_send: bool
    read_only_reason: Optional[str] = None
    expires_at: Optional[str] = None


class HumanConversationListData(BaseModel):
    items: List[HumanConversationItem]


# --- B 类 data：通知偏好 ---------------------------------------------------


class NotificationPreferencesData(BaseModel):
    """ME-10 通知偏好；``available_levels`` 由服务端下发，客户端不硬编码枚举。"""

    quiet_level: str
    available_levels: List[str]


# --- 各端点最终响应模型 ----------------------------------------------------

BootstrapResponse = WorldEnvelope[BootstrapData]
CandidateListResponse = WorldEnvelope[CandidateListData]
ResidentListResponse = WorldEnvelope[ResidentListData]
ConversationListResponse = WorldEnvelope[ConversationListData]
ConversationMessagesResponse = WorldEnvelope[ConversationMessagesData]
ConversationReadResponse = WorldEnvelope[ConversationReadData]
TurnResponse = WorldEnvelope[TurnData]
FeedListResponse = WorldEnvelope[FeedListData]
FeedPostResponse = WorldEnvelope[FeedPostData]
FeedRetireResponse = WorldEnvelope[FeedRetireData]
ResidentDraftPreviewResponse = WorldEnvelope[ResidentDraftPreviewData]
HumanConversationListResponse = WorldEnvelope[HumanConversationListData]
MediaUploadResponse = WorldEnvelope[MediaUploadData]
HumanReportOptionsResponse = WorldEnvelope[HumanReportOptionsData]
NotificationPreferencesResponse = WorldEnvelope[NotificationPreferencesData]

# 世界类端点的失败响应统一是错误信封。逐码含义见交接文档 §7；客户端按 code 分支。
WORLD_ERROR_RESPONSES = {
    401: {"model": WorldErrorEnvelope, "description": "未登录或 token 失效"},
    403: {"model": WorldErrorEnvelope, "description": "账号或世界被停用"},
    404: {"model": WorldErrorEnvelope, "description": "资源不存在或能力被关闭"},
    409: {"model": WorldErrorEnvelope, "description": "状态冲突（只读、并发、容量）"},
    422: {"model": WorldErrorEnvelope, "description": "参数校验失败"},
    429: {"model": WorldErrorEnvelope, "description": "触发限流"},
}

__all__ = [
    "AccountDeletionResponse",
    "AppConfigResponse",
    "BootstrapResponse",
    "CandidateListResponse",
    "ConversationListResponse",
    "ConversationMessagesResponse",
    "ConversationReadResponse",
    "FeedListResponse",
    "FeedPostResponse",
    "FeedRetireResponse",
    "HumanConversationListResponse",
    "HumanReportOptionsResponse",
    "MeResponse",
    "MediaUploadResponse",
    "NotificationPreferencesResponse",
    "ProfileOptionsResponse",
    "ProfileUpdateResponse",
    "ResidentDraftPreviewResponse",
    "ResidentListResponse",
    "TurnResponse",
    "WORLD_ERROR_RESPONSES",
    "WorldEnvelope",
    "WorldErrorEnvelope",
]
