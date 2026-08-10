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
        "自定义角色模板管理",
        "可以自定义创建AI角色模板，可分享给好友或他人使用",
        ">进入</a>",
    ):
        assert marker in html


def test_creator_role_template_page_has_full_p0_lifecycle_and_no_runtime_surface():
    html = _read("app/static/creator_role_templates.html")

    for marker in (
        'id="role-name"',
        'id="role-personality"',
        'id="role-mission"',
        'id="role-opening-line"',
        "/web/me/creator-role-templates",
        "mutate(template.id, 'review')",
        "+ '/publish'",
        "mutate(template.id, 'disable')",
        "mutate(template.id, 'enable')",
        "method: 'DELETE'",
        "+ '/stats'",
        "复制注册链接",
        "t('creator_used_count'",
        "编辑模板内容",
        "template.effective_status_display",
        "latest.review_status_display",
        "latest.review_reason_display",
        "latest.public_summary",
        "latest.summary_edit_status === 'available'",
        "+ '/summary-edit'",
        "version_id: version.id, summary: summary",
        'id="role-test-note"',
        'style="display:none"',
        "document.getElementById('role-test-note').style.display = S.templates.length ? '' : 'none'",
        "当前暂不支持在线试玩",
        "尚未加入朝夕的新账号",
        "最多可以拥有 3 个有效模板。",
        "body.message || t('generic_error')",
        "config.messages || {}",
    ):
        assert marker in html
    assert "'槽位 ' + template.slot_no" not in html
    assert "编辑三字段" not in html
    assert "function statusLabel(" not in html
    assert "+ ' · ' + latest.review_status" not in html
    assert "appendField(fields, '审核说明', latest.review_reason)" not in html
    assert "intent.error" not in html
    assert "可以创建最多 3 个未删除模板。" not in html
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
    assert "t('role_template_unavailable')" in html
    for target in (
        "role-preview-name",
        "role-preview-summary",
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
    assert "body.message || t('generic_error')" in html
    assert "intent.error_message" not in html
    assert "t('binding_failed')" in html
    assert "intent.error ||" not in html
    assert "await res.text()" not in html
    assert "t('role_template_unavailable')" in html
    for target in (
        "role-preview-name",
        "role-preview-summary",
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
