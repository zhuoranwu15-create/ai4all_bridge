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
