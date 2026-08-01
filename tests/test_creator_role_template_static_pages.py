"""CRT-07：个人中心、模板页、落地预览与 Nginx 静态契约。"""
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_dashboard_keeps_plain_invite_and_adds_template_entry():
    html = _read("app/static/dashboard.html")

    for marker in (
        'id="d-invite-code"',
        'id="d-invite-link"',
        "copyInviteCode()",
        "copyInviteLink()",
        'href="/user/creator-role-templates.html"',
        "管理角色模板",
    ):
        assert marker in html


def test_creator_role_template_page_has_full_p0_lifecycle_and_no_runtime_surface():
    html = _read("app/static/creator_role_templates.html")

    for marker in (
        'id="role-name"',
        'id="role-personality"',
        'id="role-mission"',
        "/web/me/creator-role-templates",
        "mutate(template.id, 'review')",
        "+ '/publish'",
        "mutate(template.id, 'disable')",
        "mutate(template.id, 'enable')",
        "method: 'DELETE'",
        "+ '/stats'",
        "复制注册链接",
        "当前暂不支持在线试玩",
        "尚未加入朝夕的新账号",
    ):
        assert marker in html
    assert ".innerHTML" not in html
    for forbidden in (
        "/trial",
        "/debug",
        "/switch",
        "apply_to_my_account",
        "切换到该角色",
    ):
        assert forbidden not in html


def test_home_preserves_both_codes_and_fails_template_only_to_normal_onboarding():
    html = _read("app/static/home.html")

    assert "params.get('invite_code')" in html
    assert "params.get('campaign_code')" in html
    assert "payload.invite_code = inviteCode" in html
    assert "payload.campaign_code = campaignCode" in html
    assert "/web/creator-role-template-links/" in html
    assert "params.delete('campaign_code')" in html
    assert "params.delete('invite_code')" not in html
    assert "角色模板已不可用，将按普通流程创建朝夕伙伴" in html
    for target in (
        "role-preview-name",
        "role-preview-personality",
        "role-preview-mission",
        "role-preview-expiry",
    ):
        assert f"getElementById('{target}').textContent" in html


def test_legacy_onboarding_preserves_both_codes_and_uses_public_preview():
    html = _read("app/static/onboarding.html")

    assert "get('invite_code')" in html
    assert "get('campaign_code')" in html
    assert "invite_code: normalizedInviteCode() || null" in html
    assert "campaign_code: normalizedCampaignCode() || null" in html
    assert "/web/creator-role-template-links/" in html
    assert "params.delete('campaign_code')" in html
    assert "params.delete('invite_code')" not in html
    assert "角色模板已不可用，将按普通流程创建朝夕伙伴" in html
    for target in (
        "role-preview-name",
        "role-preview-personality",
        "role-preview-mission",
        "role-preview-expiry",
    ):
        assert f"getElementById('{target}').textContent" in html


def test_nginx_whitelists_creator_role_template_page_like_dashboard():
    nginx = _read("deploy/nginx/ai4company.top.conf")

    assert "location = /user/creator-role-templates.html {" in nginx
    assert "proxy_pass http://127.0.0.1:8180/ui/creator_role_templates.html;" in nginx
    block = nginx.split("location = /user/creator-role-templates.html {", 1)[1].split(
        "}", 1
    )[0]
    for header in (
        "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_set_header X-Forwarded-Proto https;",
    ):
        assert header in block
