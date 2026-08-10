"""朝夕公开 API 的机器错误码与用户消息契约。"""


def test_public_http_error_returns_code_and_localized_message(client):
    response = client.post(
        "/web/sms/send-otp",
        json={"phone": "123", "captcha_verify_param": "unused"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "invalid_phone",
        "message": "请输入有效的中国大陆手机号码。",
    }
    assert response.headers["cache-control"] == "no-store"


def test_public_validation_error_uses_stable_envelope(client):
    response = client.post("/web/sms/send-otp", json={})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "invalid_request",
        "message": "请求内容无效，请检查后重试。",
    }
    assert response.headers["cache-control"] == "no-store"


def test_binding_projection_hides_upstream_payload_and_localizes_error():
    from app.routers.web import _binding_intent_for_public

    public = _binding_intent_for_public(
        {
            "id": "bind-1",
            "status": "failed",
            "error": "provider stack trace and token",
            "raw_result": {"provider": "private"},
        }
    )

    assert public == {
        "id": "bind-1",
        "status": "failed",
        "error": "service_unavailable",
        "error_message": "服务暂时不可用，请稍后重试。",
    }
