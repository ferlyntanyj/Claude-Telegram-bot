"""
Email the weekly liquidity momentum summary (output/weekly_summary.md) with
the full workbook attached. Run after 05_weekly_diff.py.

Auth: reads the Gmail App Password from the SGX_SCREENER_GMAIL_APP_PASSWORD
environment variable. Never hardcode it here or pass it on the command line.
Set it once with (PowerShell):
    setx SGX_SCREENER_GMAIL_APP_PASSWORD "your-16-char-app-password"
(requires a new terminal / login to take effect)
"""
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FROM_ADDR = "ferlyntanyj@gmail.com"
TO_ADDR = "ferlyntanyj@gmail.com"
ENV_VAR = "SGX_SCREENER_GMAIL_APP_PASSWORD"

SUMMARY_PATH = "../output/weekly_summary.md"
WORKBOOK_PATH = "../output/SGX_Liquidity_Momentum_Screener.xlsx"


def markdown_to_html(md_text):
    lines = md_text.splitlines()
    html = []
    in_list = False
    for line in lines:
        line = line.rstrip()
        if line.startswith("## "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            if in_list:
                html.append("</ul>")
                in_list = False
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<p>{line}</p>")
    if in_list:
        html.append("</ul>")

    body = "\n".join(html)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
    return f"<html><body style='font-family:Arial,sans-serif;'>{body}</body></html>"


def main():
    app_password = os.environ.get(ENV_VAR)
    if not app_password:
        print(f"ERROR: environment variable {ENV_VAR} is not set. Skipping email send.", file=sys.stderr)
        sys.exit(1)

    with open(SUMMARY_PATH, "r", encoding="utf-8") as f:
        summary_text = f.read()

    first_line = summary_text.splitlines()[0].lstrip("# ").strip()
    date_match = re.search(r"Run date.*?:\*\*\s*(\S+)", summary_text)
    run_date = date_match.group(1) if date_match else ""
    subject = f"{first_line} — {run_date}" if run_date else first_line

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(summary_text, "plain", "utf-8"))
    alt.attach(MIMEText(markdown_to_html(summary_text), "html", "utf-8"))
    msg.attach(alt)

    if os.path.exists(WORKBOOK_PATH):
        with open(WORKBOOK_PATH, "rb") as f:
            part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(WORKBOOK_PATH))
        msg.attach(part)
    else:
        print(f"WARNING: workbook not found at {WORKBOOK_PATH}, sending without attachment.", file=sys.stderr)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(FROM_ADDR, app_password)
        server.sendmail(FROM_ADDR, [TO_ADDR], msg.as_string())

    print(f"Email sent to {TO_ADDR}: {subject}")


if __name__ == "__main__":
    main()
