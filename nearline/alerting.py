"""nearline 告警：复用 app.alerting 的飞书发送 + 脱敏（懒加载，失败不致命）。

与 scripts/backup_data.py 一致的接入方式：无 webhook 配置时静默跳过，
告警本身失败也不掩盖原始错误。
"""

import sys


def send_alert(message: str) -> bool:
    """向运维飞书通道发脱敏告警；无 webhook 或发送失败返回 False。"""
    try:
        from app.config import settings

        webhook = str(getattr(settings, "feishu_alert_webhook_url", "") or "").strip()
        if not webhook:
            return False
        from app.alerting import _send_feishu_text, redact_alert_text

        text = redact_alert_text(f"[ai4all][nearline] {message}")
        _send_feishu_text(webhook, text, 3.0)
        return True
    except Exception as err:  # 告警失败不能影响主流程
        print(f"nearline alert failed: {err}", file=sys.stderr)
        return False


def send_report(text: str) -> bool:
    """把每日报告摘要推送到运营飞书群（FEISHU_WEBSITE_WEBHOOK_URL）。

    与 send_alert 区分：报告走 feishu_website_webhook_url（运营群），
    告警走 feishu_alert_webhook_url（运维群）。无 webhook 或发送失败返回 False，
    不影响日报生成主流程。报告为内部聚合元数据，不脱敏（无用户正文）。
    """
    try:
        from app.config import settings

        webhook = str(getattr(settings, "feishu_website_webhook_url", "") or "").strip()
        if not webhook:
            return False
        from app.alerting import _send_feishu_text

        _send_feishu_text(webhook, text, 5.0)
        return True
    except Exception as err:  # 推送失败不能影响日报生成
        print(f"nearline report push failed: {err}", file=sys.stderr)
        return False
