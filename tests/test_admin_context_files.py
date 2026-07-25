ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


from tests.factories import create_account as _create_account


def test_context_files_returns_context_files_with_content(client):
    from app.products.zhaoxi.infrastructure.profiles import write_user_name

    account_id = "aid_ctxfiles"
    _create_account(account_id)
    write_user_name(account_id, "冲哥")  # 往 USER.md 写入可见内容

    res = client.get(
        f"/admin/accounts/{account_id}/context-files", headers=ADMIN_HEADERS
    )
    assert res.status_code == 200
    data = res.json()
    assert data["account_id"] == account_id
    files = {f["file"]: f for f in data["files"]}
    # RELATIONSHIP.md 已移除：上下文文件不再含它（关系状态改由 DB 维护、/meta 渲染）
    assert set(files) == {"SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"}
    # 明文返回，可直接看到内容
    assert "用户称呼：冲哥" in files["USER.md"]["content"]
    assert files["USER.md"]["chars"] > 0
    assert files["SOUL.md"]["exists"] is True


def test_context_files_requires_admin_auth(client):
    account_id = "aid_ctx_noauth"
    _create_account(account_id)
    res = client.get(f"/admin/accounts/{account_id}/context-files")
    assert res.status_code in (401, 403)


def test_context_files_404_for_unknown_account(client):
    res = client.get(
        "/admin/accounts/aid_does_not_exist/context-files", headers=ADMIN_HEADERS
    )
    assert res.status_code == 404
