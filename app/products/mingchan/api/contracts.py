"""鸣蝉 Native 身份 API 的公开契约。"""
from __future__ import annotations

from typing import Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.products.mingchan.api.world_contracts import MessageContent

DataT = TypeVar("DataT")


class MingchanOtpSendRequest(BaseModel):
    """鸣蝉短信 OTP 发送请求。"""

    model_config = ConfigDict(extra="forbid")

    phone: str
    captcha_verify_param: str


class MingchanOtpVerifyRequest(BaseModel):
    """鸣蝉短信 OTP 校验请求。"""

    model_config = ConfigDict(extra="forbid")

    phone: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class MingchanStatusResponse(BaseModel):
    """鸣蝉身份操作的基础成功响应。"""

    status: str


class MingchanOtpVerifyResponse(MingchanStatusResponse):
    """OTP 校验成功后返回的单次注册凭证。"""

    verified_token: str


class MingchanSessionRequest(BaseModel):
    """使用已验证 OTP token 换取鸣蝉产品 session。"""

    model_config = ConfigDict(extra="forbid")

    phone: str
    verified_token: str = Field(min_length=8, max_length=256)
    invite_code: Optional[str] = Field(default=None, max_length=64)


class MingchanPlatformUser(BaseModel):
    """鸣蝉身份接口允许客户端读取的真人字段。"""

    id: str
    phone_masked: str
    display_name: Optional[str] = None
    avatar_key: Optional[str] = None
    avatar_ref: Optional[str] = None


class MingchanSessionResponse(BaseModel):
    """鸣蝉 session 创建结果；居民账号由 World bootstrap 后续创建。"""

    status: str
    access_token: str
    expires_at: str
    is_new_user: bool
    platform_user: MingchanPlatformUser


class MingchanMeResponse(BaseModel):
    """当前鸣蝉 session 对应的真人身份。"""

    status: str
    platform_user: MingchanPlatformUser
    world: Optional["MingchanMeWorld"] = None
    server_time: str


class MingchanMeWorld(BaseModel):
    """当前真人的鸣蝉 home world；尚未 bootstrap 时为 null。"""

    id: str
    status: str
    onboarding_state: str


class MingchanLogoutResponse(BaseModel):
    """鸣蝉当前 session 注销结果。"""

    status: str


class MingchanAccountDeletionRequest(BaseModel):
    """立即注销鸣蝉产品资料的显式确认。"""

    model_config = ConfigDict(extra="forbid")

    confirm: bool
    reason_code: Optional[
        Literal[
            "not_useful",
            "privacy_concern",
            "too_expensive",
            "switching",
            "other",
        ]
    ] = None


class MingchanAccountDeletionRecord(BaseModel):
    """客户端可见的鸣蝉注销执行结果。"""

    request_id: str
    status: str
    reason_code: Optional[str] = None
    executed_at: Optional[str] = None


class MingchanAccountDeletionResponse(BaseModel):
    """鸣蝉注销已完成；收到响应后客户端必须清除本产品 token。"""

    status: str
    request: MingchanAccountDeletionRecord


class MingchanWorldEnvelope(BaseModel, Generic[DataT]):
    """鸣蝉 World API 的统一成功信封。"""

    code: str
    request_id: str
    server_time: str
    data: DataT


class MingchanWorldSummary(BaseModel):
    """客户端可见的 home world 状态。"""

    id: str
    status: str
    onboarding_state: str


class MingchanCandidateData(BaseModel):
    """不包含 persona seed 和内部 resident id 的候选 DTO。"""

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


class MingchanResidentData(BaseModel):
    """不暴露 runtime account id 的居民 DTO。"""

    resident_id: str
    name: str
    avatar_ref: Optional[str] = None
    status: str
    origin: str
    conversation_id: str
    conversation_state: str
    mission_display: str


class MingchanBootstrapData(BaseModel):
    """World bootstrap 返回数据。"""

    world: MingchanWorldSummary
    candidates: List[MingchanCandidateData]
    existing_residents: List[MingchanResidentData]


class MingchanCandidateListData(BaseModel):
    """候选列表数据。"""

    candidates: List[MingchanCandidateData]


class MingchanResidentListData(BaseModel):
    """居民列表数据。"""

    residents: List[MingchanResidentData]


class MingchanResidentSelectionRequest(BaseModel):
    """确认居民时的一项选择。"""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=20)


class MingchanConfirmResidentsRequest(BaseModel):
    """鸣蝉首次确认居民请求。"""

    model_config = ConfigDict(extra="forbid")

    selections: List[MingchanResidentSelectionRequest] = Field(max_length=10)


class MingchanBootstrapResponse(MingchanWorldEnvelope[MingchanBootstrapData]):
    """World bootstrap 成功响应。"""


class MingchanCandidateListResponse(
    MingchanWorldEnvelope[MingchanCandidateListData]
):
    """候选列表成功响应。"""


class MingchanResidentListResponse(
    MingchanWorldEnvelope[MingchanResidentListData]
):
    """居民列表或确认成功响应。"""


class MingchanConversationTurnRequest(BaseModel):
    """鸣蝉居民 turn 请求；正文和媒体引用至少提供一项。"""

    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
    )
    text: str = Field(default="", max_length=4000)
    media_ref: Optional[str] = Field(default=None, max_length=64)


class MingchanConversationReadRequest(BaseModel):
    """将会话标记为已读到一条真实消息。"""

    model_config = ConfigDict(extra="forbid")

    last_message_id: int = Field(ge=1)


class MingchanConversationResident(BaseModel):
    """会话列表中的居民公开字段。"""

    id: str
    name: str
    avatar_ref: Optional[str] = None
    status: str


class MingchanConversationItem(BaseModel):
    """鸣蝉居民会话列表项。"""

    conversation_id: str
    resident: MingchanConversationResident
    state: str
    last_preview: Optional[str] = None
    unread: int
    last_message_at: Optional[str] = None
    sort_time: Optional[str] = None
    can_send: bool
    read_only_reason: Optional[str] = None


class MingchanConversationListData(BaseModel):
    """鸣蝉居民会话分页结果。"""

    items: List[MingchanConversationItem]
    next_cursor: Optional[str] = None


class MingchanConversationMessageItem(BaseModel):
    """客户端可见的鸣蝉会话消息。"""

    id: int
    message_id: Optional[str] = None
    role: str
    message_type: str
    text: Optional[str] = None
    content: MessageContent
    created_at: Optional[str] = None


class MingchanConversationMessagesData(BaseModel):
    """会话历史分页结果。"""

    state: str
    messages: List[MingchanConversationMessageItem]
    next_cursor: Optional[int] = None


class MingchanConversationReadData(BaseModel):
    """已读游标结果。"""

    conversation_id: str
    last_read_message_id: Optional[int] = None
    unread: int


class MingchanTurnReply(BaseModel):
    """居民回复；非空时正文一定存在。"""

    text: str
    message_id: Optional[str] = None


class MingchanTurnData(BaseModel):
    """鸣蝉文字 turn 的稳定响应数据。"""

    reply: Optional[MingchanTurnReply] = None
    no_reply: bool
    deduplicated: bool


class MingchanConversationListResponse(
    MingchanWorldEnvelope[MingchanConversationListData]
):
    """鸣蝉会话列表成功响应。"""


class MingchanConversationMessagesResponse(
    MingchanWorldEnvelope[MingchanConversationMessagesData]
):
    """鸣蝉会话历史成功响应。"""


class MingchanConversationReadResponse(
    MingchanWorldEnvelope[MingchanConversationReadData]
):
    """鸣蝉已读游标成功响应。"""


class MingchanConversationTurnResponse(MingchanWorldEnvelope[MingchanTurnData]):
    """鸣蝉居民 turn 成功响应。"""


class MingchanAppConfigResponse(BaseModel):
    """鸣蝉 Native 客户端启动配置。"""

    product: dict
    captcha: dict
    features: dict
    limits: dict
    client_contract_version: str
    server_time: str
    minimum_supported_version: str
    minimum_supported_version_by_platform: dict


class MingchanProfileAvatarOption(BaseModel):
    """一个可选真人头像。"""

    key: str
    avatar_ref: str


class MingchanProfileLimits(BaseModel):
    """鸣蝉用户资料输入限制。"""

    nickname_chars: int
    nickname_min_chars: int


class MingchanProfileOptionsResponse(BaseModel):
    """鸣蝉用户资料受控选项。"""

    status: str
    avatars: List[MingchanProfileAvatarOption]
    limits: MingchanProfileLimits


class MingchanProfileUpdateRequest(BaseModel):
    """鸣蝉用户资料局部更新请求。"""

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=20)
    avatar_key: Optional[str] = Field(default=None, max_length=64)


class MingchanProfileUpdateResponse(BaseModel):
    """鸣蝉用户资料更新结果。"""

    status: str
    platform_user: MingchanPlatformUser


__all__ = [
    "MingchanBootstrapResponse",
    "MingchanAppConfigResponse",
    "MingchanCandidateListResponse",
    "MingchanConversationListResponse",
    "MingchanConversationMessagesResponse",
    "MingchanConversationReadRequest",
    "MingchanConversationReadResponse",
    "MingchanConversationTurnRequest",
    "MingchanConversationTurnResponse",
    "MingchanConfirmResidentsRequest",
    "MingchanLogoutResponse",
    "MingchanMeResponse",
    "MingchanOtpSendRequest",
    "MingchanOtpVerifyRequest",
    "MingchanOtpVerifyResponse",
    "MingchanPlatformUser",
    "MingchanProfileOptionsResponse",
    "MingchanProfileUpdateRequest",
    "MingchanProfileUpdateResponse",
    "MingchanResidentListResponse",
    "MingchanSessionRequest",
    "MingchanSessionResponse",
    "MingchanStatusResponse",
]
