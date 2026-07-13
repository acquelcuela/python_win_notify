import logging
import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
MAX_RETRIES = 3
RETRY_INTERVAL_SECONDS = 10


def send_html_mail(gmail_address: str, app_password: str, mail_to: str, subject: str, html_body: str) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = gmail_address
    message["To"] = mail_to
    message.attach(MIMEText(html_body, "html", "utf-8"))

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(gmail_address, app_password)
                server.sendmail(gmail_address, [mail_to], message.as_string())
            logging.info("[mail_gmail] sent to %s", mail_to)
            return
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                logging.warning("[mail_gmail] retry %s/%s: %s", attempt, MAX_RETRIES, exc)
                time.sleep(RETRY_INTERVAL_SECONDS)

    raise RuntimeError(f"Failed to send email after {MAX_RETRIES} attempts: {last_error}")


def run(root: Path) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    mail_to = os.getenv("MAIL_TO")
    missing = [
        key
        for key, value in {
            "GMAIL_ADDRESS": gmail_address,
            "GMAIL_APP_PASSWORD": app_password,
            "MAIL_TO": mail_to,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing Gmail settings: {', '.join(missing)}")

    report_path = root / "output" / "report.html"
    if not report_path.exists():
        raise FileNotFoundError(f"Report does not exist: {report_path}")

    html_body = report_path.read_text(encoding="utf-8")
    now = datetime.now(JST)
    subject = f"[NightlyBatchNotify] {now.strftime('%Y-%m-%d')} morning report"
    send_html_mail(gmail_address, app_password, mail_to, subject, html_body)
