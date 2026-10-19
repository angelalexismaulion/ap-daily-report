"""Daily AP report: read invoices from Google Sheets, calculate overdue items,
write a summary tab, have an AI draft a short brief, and email it.

All secrets come from environment variables (never hard-coded).
"""
import json
import os
import smtplib
from email.message import EmailMessage

import gspread
import pandas as pd
from google import genai

# Keep the model name in one place: AI model names get retired over time.
MODEL_NAME = "gemini-3.8-flash"
TIMEZONE = "Asia/Manila"


def get_today():
    # Servers run on UTC, so set the timezone explicitly or "today" can be off by a day.
    return pd.Timestamp.now(tz=TIMEZONE).normalize().tz_localize(None)


def load_invoices(sh):
    df = pd.DataFrame(sh.worksheet("Invoices").get_all_records())
    df["Due Date"] = pd.to_datetime(df["Due Date"])
    return df


def analyze(df, today):
    open_inv = df[df["Status"] != "Paid"].copy()
    open_inv["Days Overdue"] = (today - open_inv["Due Date"]).dt.days.clip(lower=0)

    def urgency(row):
        if row["Days Overdue"] > 0:
            return "OVERDUE"
        if (row["Due Date"] - today).days <= 7:
            return "DUE THIS WEEK"
        return "OK"

    open_inv["Urgency"] = open_inv.apply(urgency, axis=1)
    open_inv = open_inv.sort_values("Days Overdue", ascending=False)

    summary = (
        open_inv.groupby("Entity")
        .agg(
            Open_Invoices=("Invoice #", "count"),
            Total_Open=("Amount", "sum"),
            Max_Days_Overdue=("Days Overdue", "max"),
        )
        .reset_index()
    )
    summary.columns = ["Entity", "Open Invoices", "Total Open ($)", "Max Days Overdue"]
    return open_inv, summary


def write_summary(sh, summary):
    try:
        out = sh.worksheet("Summary")
        out.clear()
    except gspread.WorksheetNotFound:
        out = sh.add_worksheet("Summary", rows=100, cols=10)
    data = [summary.columns.tolist()] + summary.values.tolist()
    out.update(values=data, range_name="A1")


def build_brief(open_inv, summary, today):
    overdue = open_inv[open_inv["Days Overdue"] > 0]
    due_soon = open_inv[open_inv["Urgency"] == "DUE THIS WEEK"]

    key_facts = (
        f"Open invoices: {len(open_inv)}\n"
        f"Overdue invoices: {len(overdue)}, totaling ${overdue['Amount'].sum():,.2f}\n"
        f"Due within 7 days: {len(due_soon)}, totaling ${due_soon['Amount'].sum():,.2f}\n"
    )
    table = open_inv[
        ["Invoice #", "Vendor", "Entity", "Due Date", "Amount", "Status", "Days Overdue", "Urgency"]
    ].to_string(index=False)

    prompt = f"""You are an accounts payable assistant. Using ONLY the data below,
write a short morning brief for a finance manager:
1. One sentence overall status that includes the overdue count and total amount.
2. The most urgent overdue invoices (vendor, amount, days overdue, status). Say "top X of N" if you list fewer than all of them.
3. What is due in the next 7 days.
4. One recommended action. Only recommend paying invoices with Status "Approved". For Pending invoices, recommend getting approval first.
Do not invent numbers or priorities. Keep it under 170 words.

Date: {today.date()}
{key_facts}
Open invoices:
{table}"""

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return response.text.replace("**", "")


def send_email(brief, today):
    sender = os.environ["GMAIL_ADDRESS"]
    msg = EmailMessage()
    msg["Subject"] = f"AP Morning Brief - {today.date()}"
    msg["From"] = sender
    msg["To"] = sender
    msg.set_content(brief)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        server.send_message(msg)


def main():
    creds = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(os.environ["SHEET_ID"])

    today = get_today()
    df = load_invoices(sh)
    open_inv, summary = analyze(df, today)
    write_summary(sh, summary)

    if open_inv.empty:
        print("No open invoices today. Nothing to report.")
        return

    brief = build_brief(open_inv, summary, today)
    send_email(brief, today)
    print("Done: summary updated and email sent.")


if __name__ == "__main__":
    main()
