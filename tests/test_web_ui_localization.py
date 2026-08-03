from pathlib import Path

from app.products.zhaoxi.application.web_ui_localization import (
    faq_groups,
    web_ui_messages,
)


def test_web_ui_catalogs_have_identical_keys():
    catalogs = {
        language: web_ui_messages(language=language)
        for language in ("zh-CN", "en-US", "ja-JP")
    }
    assert set(catalogs["zh-CN"]) == set(catalogs["en-US"])
    assert set(catalogs["zh-CN"]) == set(catalogs["ja-JP"])
    assert all(value.strip() for catalog in catalogs.values() for value in catalog.values())


def test_faq_catalogs_keep_the_same_structure():
    catalogs = {
        language: faq_groups(language=language)
        for language in ("zh-CN", "en-US", "ja-JP")
    }
    shape = [len(group["items"]) for group in catalogs["zh-CN"]]
    assert shape == [2, 5, 3, 4]
    for groups in catalogs.values():
        assert [len(group["items"]) for group in groups] == shape
        assert all(group["category"].strip() for group in groups)
        assert all(
            item["question"].strip() and item["answer"].strip()
            for group in groups
            for item in group["items"]
        )


def test_web_config_and_faq_content_expose_default_language(client, fresh_db):
    config = client.get("/web/config")
    faq = client.get("/web/faq/content")

    assert config.status_code == 200
    assert config.json()["product"]["default_language"] == "zh-CN"
    assert config.json()["messages"]["dashboard_role_templates"] == "自定义角色模板管理"
    assert faq.status_code == 200
    assert faq.json()["product"]["default_language"] == "zh-CN"
    assert [len(group["items"]) for group in faq.json()["groups"]] == [2, 5, 3, 4]


def test_public_pages_load_shared_i18n_runtime():
    for path in (
        "app/static/home.html",
        "app/static/dashboard.html",
        "app/static/creator_role_templates.html",
        "app/static/onboarding.html",
        "app/static/faq.html",
    ):
        source = open(path, encoding="utf-8").read()
        assert 'src="product-i18n.js"' in source
        assert "data-i18n" in source


def test_captcha_language_follows_the_supported_product_language_mapping():
    for path in ("app/static/home.html", "app/static/onboarding.html"):
        source = open(path, encoding="utf-8").read()
        assert "'zh-CN':'cn','en-US':'en','ja-JP':'ja'" in source
        assert "language: 'cn'" not in source


# ── product-i18n.js 交付契约 ──────────────────────────────────────────────
# 2026-08-03 线上故障：product-i18n.js 随本能力新增，但官网 nginx 是逐文件白名单代理，
# 漏加后 /product-i18n.js 落到 SPA catch-all 返回 200 HTML；浏览器把 HTML 当 JS 执行，
# window.CXProductI18n 缺失导致落地页初始化链中断并禁用发送验证码按钮，Web 注册全断。

#: 引用 product-i18n.js 的页面 → 该页面在官网上的相对解析路径。
I18N_ASSET_PAGES = {
    "app/static/home.html": "/product-i18n.js",
    "app/static/dashboard.html": "/user/product-i18n.js",
    "app/static/creator_role_templates.html": "/user/product-i18n.js",
    "app/static/faq.html": "/product-i18n.js",
}


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_pages_referencing_product_i18n_are_whitelisted_by_nginx():
    nginx = _read("deploy/nginx/ai4company.top.conf")

    for page, url in sorted(set(I18N_ASSET_PAGES.items())):
        assert 'src="product-i18n.js"' in _read(page), page
        location = f"location = {url} {{"
        assert location in nginx, f"{page} 依赖 {url}，nginx 未放行"
        block = nginx.split(location, 1)[1].split("}", 1)[0]
        assert "proxy_pass http://127.0.0.1:8180/ui/product-i18n.js;" in block


def test_pages_never_call_product_i18n_without_a_guard():
    """静态资源缺失只能降级展示，不能打断页面初始化链。"""
    pages = sorted(set(I18N_ASSET_PAGES) | {"app/static/onboarding.html"})
    for page in pages:
        guard_indent = None
        for line in _read(page).splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if stripped.startswith("if (window.CXProductI18n)") and stripped.endswith("{"):
                guard_indent = indent
                continue
            if guard_indent is not None and stripped == "}" and indent == guard_indent:
                guard_indent = None
                continue
            if "window.CXProductI18n.setConfig(" in stripped or "window.CXProductI18n.apply(" in stripped:
                assert guard_indent is not None, f"{page}: 无保护调用 {stripped}"
