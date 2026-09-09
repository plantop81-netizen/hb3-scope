"""브리핑 발송: Slack Incoming Webhook / Telegram Bot / SMTP 이메일. 설정된 채널만 발송한다."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from .config import settings

log = logging.getLogger(__name__)


def send_slack(text: str) -> bool:
    if not settings.slack_webhook_url:
        return False
    # 슬랙 메시지 길이 제한(약 40k) 고려
    r = httpx.post(settings.slack_webhook_url, json={"text": text[:39000]}, timeout=30)
    r.raise_for_status()
    return True


def send_telegram(text: str) -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    # 텔레그램은 4096자 제한 → 분할 전송
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [text]
    for ch in chunks:
        r = httpx.post(url, json={"chat_id": settings.telegram_chat_id, "text": ch, "disable_web_page_preview": True}, timeout=30)
        r.raise_for_status()
    return True


def send_email(subject: str, text: str, html: str | None = None) -> bool:
    if not (settings.smtp_host and settings.mail_to):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.mail_from or settings.smtp_user
    msg["To"] = settings.mail_to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
        s.ehlo()
        if settings.smtp_port != 25:
            try:
                s.starttls()
            except smtplib.SMTPNotSupportedError:
                pass
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.sendmail(msg["From"], [a.strip() for a in settings.mail_to.split(",") if a.strip()], msg.as_string())
    return True


def broadcast(subject: str, text: str, html: str | None = None) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name, fn in (("slack", lambda: send_slack(text)), ("telegram", lambda: send_telegram(text)), ("email", lambda: send_email(subject, text, html))):
        try:
            results[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.error("%s 발송 실패: %s", name, e)
            results[name] = False
    return results
