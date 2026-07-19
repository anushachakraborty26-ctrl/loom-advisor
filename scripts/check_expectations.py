"""Deadline checker — run from cron / Task Scheduler each morning.

    uv run python scripts/check_expectations.py

Prints the data-contract status. If anything is overdue and SMTP is
configured (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_FROM env
vars), emails every owner with an address in config/expectations.yaml.
Exit code 1 when something is overdue, so schedulers can chain on it.

Example crontab line (check every day at 10:05):
    5 10 * * * cd /path/to/loom-advisor && uv run python scripts/check_expectations.py
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.alerts import Overdue, overdue_expectations

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get("LOOM_DB_URL", f"sqlite:///{ROOT / 'loom_advisor.db'}")


def send_alerts(items: list[Overdue]) -> None:
    host = os.environ.get("SMTP_HOST")
    recipients = sorted(
        {o["email"] for item in items for o in item.owners if o.get("email")}
    )
    if not host or not recipients:
        print(
            "(no SMTP_HOST configured or no owner emails set — "
            "fill owners in config/expectations.yaml and set SMTP_* env vars to email alerts)"
        )
        return
    message = EmailMessage()
    message["Subject"] = "Loom Advisor: data upload overdue"
    message["From"] = os.environ.get("ALERT_FROM", "loom-advisor@localhost")
    message["To"] = ", ".join(recipients)
    message.set_content(
        "The following expected documents have not been uploaded:\n\n"
        + "\n".join(f"- {item.headline()}" for item in items)
        + "\n\nPlease upload via the Data entry page."
    )
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        user = os.environ.get("SMTP_USER")
        if user:
            smtp.login(user, os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)
    print(f"alert emailed to: {', '.join(recipients)}")


def main() -> int:
    engine = create_engine(DB_URL)
    with sessionmaker(bind=engine)() as session:
        items = overdue_expectations(session)
    if not items:
        print("Data contract honoured — nothing overdue.")
        return 0
    for item in items:
        print(item.headline())
        for owner in item.owners:
            contact = f" <{owner['email']}>" if owner.get("email") else ""
            print(f"  owner: {owner.get('name', '?')}{contact}")
    send_alerts(items)
    return 1


if __name__ == "__main__":
    sys.exit(main())
