"""朝夕 onboarding 身份预设的统一只读投影。"""
from dataclasses import dataclass
from typing import Optional

from app.products.zhaoxi.infrastructure.persistence.campaign import (
    get_campaign_attribution,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    get_account_creator_role_template_attribution,
)


@dataclass(frozen=True)
class OnboardingIdentityOverrides:
    """一个账号在 onboarding 中不可被用户抽取结果覆盖的身份配置。"""

    forced_ai_name: bool = False
    forced_personality: bool = False
    script_override: Optional[str] = None
    source: Optional[str] = None


def resolve_onboarding_identity_overrides(
    account_id: str,
) -> OnboardingIdentityOverrides:
    """按 creator template > 运营活码的优先级解析 onboarding 身份预设。"""
    creator_attribution = get_account_creator_role_template_attribution(
        account_id=account_id
    )
    if creator_attribution is not None:
        return OnboardingIdentityOverrides(
            forced_ai_name=True,
            forced_personality=True,
            script_override=None,
            source="creator_role_template",
        )

    campaign_attribution = get_campaign_attribution(account_id=account_id)
    if campaign_attribution is None:
        return OnboardingIdentityOverrides()
    return OnboardingIdentityOverrides(
        forced_ai_name=bool(campaign_attribution.get("ai_name_preset")),
        forced_personality=bool(campaign_attribution.get("soul_preset_key")),
        script_override=campaign_attribution.get("onboarding_script_variant"),
        source="operator_campaign",
    )


__all__ = [
    "OnboardingIdentityOverrides",
    "resolve_onboarding_identity_overrides",
]
