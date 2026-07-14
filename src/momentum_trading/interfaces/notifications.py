"""
notifications.py

Epic 12: categorized, filterable email notifications, plus a monthly HTML
report with embedded charts.

Three categories, matching the color-coding scheme requested:
  - CRITICAL (always sent, cannot be disabled): stop-loss executions,
    circuit-breaker trips, margin/connection failures
  - STANDARD (filterable): routine BUY/SELL/HOLD rebalance summaries
  - PERIODIC (scheduled): the monthly performance report

Reuses the same SMTP pattern as daily_runner.py's send_alert_email() but
adds category filtering (via config.yaml's notifications: block) and richer
HTML formatting for the periodic report.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum

import pandas as pd

from ..core.smtp_auth import authenticate as authenticate_smtp, smtp_ready

logger = logging.getLogger("notifications")


class NotificationCategory(str, Enum):
    CRITICAL = "critical"   # red -- always sent, not filterable
    STANDARD = "standard"   # green -- routine BUY/SELL/HOLD, filterable
    PERIODIC = "periodic"   # blue -- scheduled reports, filterable
    WARNING = "warning"     # amber -- non-fatal risk signals (Epic 26/27: multi-portfolio
                             # capital over-allocation, ticker overlap), filterable via
                             # notifications.send_warning -- unlike CRITICAL, these are review-
                             # when-convenient risks, not run-blocking failures


CATEGORY_COLORS = {
    NotificationCategory.CRITICAL: "#c0392b",  # red
    NotificationCategory.STANDARD: "#27ae60",  # green
    NotificationCategory.PERIODIC: "#2980b9",  # blue
    NotificationCategory.WARNING: "#e67e22",   # amber
}


def _smtp_config() -> dict | None:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO_EMAIL")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not smtp_ready(host, user, to_addr, password):
        return None
    return {"host": host, "user": user, "password": password, "to": to_addr, "port": port}


def should_send(category: NotificationCategory, notification_config: dict) -> bool:
    """
    CRITICAL is always sent regardless of config -- it is deliberately NOT
    made filterable, since suppressing a stop-loss/circuit-breaker alert is
    exactly the failure mode this whole notification system exists to prevent.
    STANDARD and PERIODIC respect the config.yaml notifications: block.
    """
    if category == NotificationCategory.CRITICAL:
        return True
    key = f"send_{category.value}"
    return bool(notification_config.get(key, True))  # default to sending if unconfigured


def send_action_email(
    category: NotificationCategory, subject: str, body_html: str,
    notification_config: dict | None = None, plain_text_fallback: str | None = None,
) -> bool:
    """
    Sends a categorized HTML email. Returns True if actually sent, False if
    filtered out by config or if SMTP isn't configured (logged either way --
    a filtered CRITICAL email should never happen, logged loudly if it does).
    """
    notification_config = notification_config or {}
    if not should_send(category, notification_config):
        logger.info("Notification filtered by config (category=%s): %s", category.value, subject)
        return False

    smtp = _smtp_config()
    if smtp is None:
        logger.error("SMTP env vars not fully configured -- NOTIFICATION NOT SENT (category=%s). "
                     "Subject: %s", category.value, subject)
        return False

    color = CATEGORY_COLORS[category]
    full_html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
      <div style="border-left: 5px solid {color}; padding-left: 12px; margin-bottom: 16px;">
        <span style="color: {color}; font-weight: bold; text-transform: uppercase;">{category.value}</span>
      </div>
      {body_html}
      <hr style="border: none; border-top: 1px solid #ddd; margin-top: 24px;">
      <p style="color: #999; font-size: 11px;">Sent by daily_runner.py notification system at {datetime.now().isoformat()}</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{category.value.upper()}] {subject}"
    msg["From"] = smtp["user"]
    msg["To"] = smtp["to"]
    if plain_text_fallback:
        msg.attach(MIMEText(plain_text_fallback, "plain"))
    msg.attach(MIMEText(full_html, "html"))

    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=15) as server:
            server.starttls()
            authenticate_smtp(server, smtp["user"], smtp["password"])
            server.sendmail(smtp["user"], [smtp["to"]], msg.as_string())
        logger.info("Notification sent (category=%s): %s", category.value, subject)
        return True
    except Exception as e:
        logger.error("Failed to send notification (category=%s, %s): %s", category.value, subject, e)
        return False


def send_critical_action(subject: str, body_html: str, plain_text_fallback: str | None = None) -> bool:
    """Convenience wrapper -- always sends, no config needed since CRITICAL is never filterable."""
    return send_action_email(NotificationCategory.CRITICAL, subject, body_html, {}, plain_text_fallback)


def send_standard_action(subject: str, body_html: str, notification_config: dict,
                          plain_text_fallback: str | None = None) -> bool:
    return send_action_email(NotificationCategory.STANDARD, subject, body_html, notification_config,
                              plain_text_fallback)


def build_rebalance_summary_html(portfolio_name: str, orders: dict) -> str:
    """Standard-category HTML table summarizing a rebalance's BUY/SELL/HOLD decisions."""
    rows = ""
    for ticker, order in orders.items():
        action_color = {"BUY": "#27ae60", "SELL": "#c0392b", "HOLD": "#7f8c8d"}.get(order["action"], "#333")
        rows += (
            f"<tr><td style='padding:4px 8px;'>{ticker}</td>"
            f"<td style='padding:4px 8px; color:{action_color}; font-weight:bold;'>{order['action']}</td>"
            f"<td style='padding:4px 8px;'>{order.get('shares', 0)}</td>"
            f"<td style='padding:4px 8px;'>{order.get('reason', '')}</td></tr>"
        )
    return f"""
    <h3>Rebalance Summary: {portfolio_name}</h3>
    <table style="border-collapse: collapse; width: 100%;">
      <tr style="background:#f4f4f4;"><th style='padding:4px 8px; text-align:left;'>Ticker</th>
          <th style='padding:4px 8px; text-align:left;'>Action</th>
          <th style='padding:4px 8px; text-align:left;'>Shares</th>
          <th style='padding:4px 8px; text-align:left;'>Reason</th></tr>
      {rows}
    </table>
    """


def build_monthly_report_html(portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict) -> tuple[str, bytes | None]:
    """
    Builds the monthly report's HTML body plus an embedded chart (as PNG bytes
    to attach inline). Returns (html_body, chart_png_bytes_or_None).

    chart_png_bytes is None if matplotlib isn't available or snapshot_df is
    too short to chart -- the HTML report still renders without the chart in
    that case, degrading gracefully rather than failing the whole report.
    """
    chart_bytes = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io

        if len(snapshot_df) >= 2:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.plot(snapshot_df["date"], snapshot_df["total_value"], marker="o", color="#2980b9")
            ax.set_title(f"{portfolio_name}: Portfolio Value")
            ax.set_ylabel("$")
            fig.autofmt_xdate()
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight")
            plt.close(fig)
            chart_bytes = buf.getvalue()
    except ImportError:
        logger.warning("matplotlib not available -- monthly report will omit the chart.")

    latest = snapshot_df.iloc[-1] if not snapshot_df.empty else None
    summary_rows = ""
    if latest is not None:
        summary_rows = f"""
        <tr><td style='padding:4px 8px;'>Total Value</td><td style='padding:4px 8px;'>${float(latest['total_value']):,.2f}</td></tr>
        <tr><td style='padding:4px 8px;'>Cash</td><td style='padding:4px 8px;'>${float(latest['cash']):,.2f}</td></tr>
        <tr><td style='padding:4px 8px;'>Unrealized P&L</td><td style='padding:4px 8px;'>${float(latest['unrealized_pnl']):,.2f}</td></tr>
        """

    comparison_rows = ""
    if "error" not in comparison and comparison.get("n_periods", 0) > 0:
        comparison_rows = f"""
        <tr><td style='padding:4px 8px;'>Cumulative Return</td><td style='padding:4px 8px;'>{comparison['portfolio_cumulative_return']:.2%}</td></tr>
        <tr><td style='padding:4px 8px;'>Benchmark Return</td><td style='padding:4px 8px;'>{comparison['benchmark_cumulative_return']:.2%}</td></tr>
        <tr><td style='padding:4px 8px;'>Outperformance</td><td style='padding:4px 8px;'>{comparison['outperformance']:+.2%}</td></tr>
        """

    chart_html = '<img src="cid:portfolio_chart" style="max-width:100%;">' if chart_bytes else ""

    html = f"""
    <h2>Monthly Report: {portfolio_name}</h2>
    <p>Period ending {datetime.now().strftime('%Y-%m-%d')}</p>
    {chart_html}
    <h3>Current Position</h3>
    <table style="border-collapse: collapse;">{summary_rows}</table>
    <h3>Performance vs. Benchmark</h3>
    <table style="border-collapse: collapse;">{comparison_rows}</table>
    <p style="color:#999; font-size:11px;">This report reflects backtested/paper/live results as configured --
    verify which mode this portfolio is running in before treating these numbers as real returns.</p>
    """
    return html, chart_bytes


def send_monthly_report(portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict,
                         notification_config: dict) -> bool:
    """PERIODIC category -- filterable, but distinct from CRITICAL/STANDARD filtering."""
    html, chart_bytes = build_monthly_report_html(portfolio_name, snapshot_df, comparison)

    if not should_send(NotificationCategory.PERIODIC, notification_config):
        logger.info("Monthly report filtered by config: %s", portfolio_name)
        return False

    smtp = _smtp_config()
    if smtp is None:
        logger.error("SMTP not configured -- monthly report NOT SENT for %s", portfolio_name)
        return False

    msg = MIMEMultipart("related")
    msg["Subject"] = f"[PERIODIC] Monthly Report: {portfolio_name}"
    msg["From"] = smtp["user"]
    msg["To"] = smtp["to"]
    msg.attach(MIMEText(html, "html"))
    if chart_bytes:
        img = MIMEImage(chart_bytes)
        img.add_header("Content-ID", "<portfolio_chart>")
        msg.attach(img)

    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=15) as server:
            server.starttls()
            authenticate_smtp(server, smtp["user"], smtp["password"])
            server.sendmail(smtp["user"], [smtp["to"]], msg.as_string())
        logger.info("Monthly report sent: %s", portfolio_name)
        return True
    except Exception as e:
        logger.error("Failed to send monthly report for %s: %s", portfolio_name, e)
        return False
