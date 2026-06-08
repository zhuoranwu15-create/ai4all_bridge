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
