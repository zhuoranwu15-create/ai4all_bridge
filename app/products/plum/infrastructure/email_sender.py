"""Plum email delivery port.

Production delivery is intentionally fail-closed until an approved provider
adapter replaces ``send_login_code`` or a supported sender mode is configured.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


class PlumEmailDeliveryUnavailable(RuntimeError):
    pass


def send_login_code(*, email: str, code: str, expires_minutes: int) -> None:
    mode = str(settings.plum_email_sender_mode or "disabled").strip().lower()
    if mode == "disabled":
        raise PlumEmailDeliveryUnavailable("plum_email_provider_unavailable")
    if mode == "smtp":
        host = str(settings.plum_email_smtp_host or "").strip()
        sender = str(settings.plum_email_smtp_from or "").strip()
        if not host or not sender:
            raise PlumEmailDeliveryUnavailable("plum_email_provider_unavailable")
        message = EmailMessage()
        message["Subject"] = "Your Plum sign-in code"
        message["From"] = sender
        message["To"] = email
        message.set_content(
            f"Your Plum sign-in code is {code}. It expires in {expires_minutes} minutes.\n\n"
            "If you did not request this code, you can ignore this email."
        )
        try:
            with smtplib.SMTP(
                host,
                int(settings.plum_email_smtp_port),
                timeout=float(settings.plum_email_smtp_timeout_seconds),
            ) as smtp:
                if bool(settings.plum_email_smtp_starttls):
                    smtp.starttls()
                username = str(settings.plum_email_smtp_username or "").strip()
                password = str(settings.plum_email_smtp_password or "")
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as err:
            raise PlumEmailDeliveryUnavailable(
                "plum_email_provider_unavailable"
            ) from err
        return
    raise PlumEmailDeliveryUnavailable("plum_email_provider_unsupported")


__all__ = ["PlumEmailDeliveryUnavailable", "send_login_code"]
