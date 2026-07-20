"""
notifications.py

Categorized, filterable email notifications, plus a monthly HTML
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
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum

import pandas as pd

from ..core.smtp_auth import authenticate as authenticate_smtp, smtp_ready, connect as smtp_connect, send_with_retry

logger = logging.getLogger("notifications")


class NotificationCategory(str, Enum):
    CRITICAL = "critical"   # red, always sent, not filterable
    STANDARD = "standard"   # green, routine BUY/SELL/HOLD, filterable
    PERIODIC = "periodic"   # blue, scheduled monthly report, filterable
    DAILY = "daily"         # purple, daily performance report, filterable via
                             # notifications.send_daily (default False), separate from
                             # PERIODIC/monthly on purpose, so the two cadences can be toggled
                             # independently; should_send()'s f"send_{category.value}" key
                             # derivation means this needed no changes there, just this entry.
    WARNING = "warning"     # amber, non-fatal risk signals (multi-portfolio
                             # capital over-allocation, ticker overlap), filterable via
                             # notifications.send_warning, unlike CRITICAL, these are review-
                             # when-convenient risks, not run-blocking failures


CATEGORY_COLORS = {
    NotificationCategory.CRITICAL: "#c0392b",  # red
    NotificationCategory.STANDARD: "#27ae60",  # green
    NotificationCategory.PERIODIC: "#2980b9",  # blue
    NotificationCategory.DAILY: "#8e44ad",     # purple
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
    CRITICAL is always sent regardless of config, it is deliberately NOT
    made filterable, since suppressing a stop-loss/circuit-breaker alert is
    exactly the failure mode this whole notification system exists to prevent.
    STANDARD/PERIODIC/WARNING default to sending if unconfigured. DAILY is the one
    exception, it defaults to NOT sending if unconfigured, since it's a real recurring
    cost/inbox-volume feature (full indicator dashboard, generated every day) that should be
    a deliberate opt-in, not something a config.yaml predating this feature silently starts
    doing. This must hold even if `send_daily` is entirely absent from notification_config,
    not just explicitly set false, hence the per-category default below rather than a single
    shared default.
    """
    if category == NotificationCategory.CRITICAL:
        return True
    key = f"send_{category.value}"
    default = category != NotificationCategory.DAILY
    return bool(notification_config.get(key, default))


def send_action_email(
    category: NotificationCategory, subject: str, body_html: str,
    notification_config: dict | None = None, plain_text_fallback: str | None = None,
) -> bool:
    """
    Sends a categorized HTML email. Returns True if actually sent, False if
    filtered out by config or if SMTP isn't configured (logged either way,
    a filtered CRITICAL email should never happen, logged loudly if it does).
    """
    notification_config = notification_config or {}
    if not should_send(category, notification_config):
        logger.info("Notification filtered by config (category=%s): %s", category.value, subject)
        return False

    smtp = _smtp_config()
    if smtp is None:
        logger.error("SMTP env vars not fully configured, NOTIFICATION NOT SENT (category=%s). "
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
    msg["X-Momentum-Trading-Bot"] = "1"
    if plain_text_fallback:
        msg.attach(MIMEText(plain_text_fallback, "plain"))
    msg.attach(MIMEText(full_html, "html"))

    def _do_send():
        with smtp_connect(smtp["host"], smtp["port"]) as server:
            authenticate_smtp(server, smtp["user"], smtp["password"])
            server.sendmail(smtp["user"], [smtp["to"]], msg.as_string())

    try:
        send_with_retry(_do_send)
        logger.info("Notification sent (category=%s): %s", category.value, subject)
        return True
    except Exception as e:
        logger.error("Failed to send notification (category=%s, %s): %s", category.value, subject, e)
        return False


def send_critical_action(subject: str, body_html: str, plain_text_fallback: str | None = None) -> bool:
    """Convenience wrapper, always sends, no config needed since CRITICAL is never filterable."""
    return send_action_email(NotificationCategory.CRITICAL, subject, body_html, {}, plain_text_fallback)


def send_standard_action(subject: str, body_html: str, notification_config: dict,
                          plain_text_fallback: str | None = None) -> bool:
    return send_action_email(NotificationCategory.STANDARD, subject, body_html, notification_config,
                              plain_text_fallback)


def _describe_fill_outcome(order: dict, dry_run: bool) -> tuple[str, str]:
    """
    Returns (display_text, color) describing what actually happened to a single order,
    as distinct from what the signal *intended* (order['action']/order['reason']).

    Reads the fill_status/fill_price/fill_shares fields that execution/live_signal.py's
    run() merges back onto each order after place_orders_ibkr() returns (live mode only),
    including the "dropped before ever reaching IBKR" statuses (DROPPED_FRACTIONAL,
    DROPPED_INSUFFICIENT_CASH) that place_orders_ibkr() now tracks separately since those
    tickers never get a real IBKR orderId and would otherwise be silently missing here.
    """
    if order["action"] == "HOLD":
        return "—", "#7f8c8d"

    status = order.get("fill_status")
    if status is None:
        if dry_run:
            return "Dry-run, no order sent", "#7f8c8d"
        return "No order sent", "#7f8c8d"

    if status == "Filled":
        filled = order.get("fill_shares", 0)
        filled_str = str(int(filled)) if filled == int(filled) else f"{filled:.4f}"
        price = order.get("fill_price", 0.0)
        return f"Filled {filled_str} @ ${price:.2f}", "#27ae60"
    if status == "DROPPED_FRACTIONAL":
        return "Dropped, rounds to 0 whole shares", "#e67e22"
    if status == "DROPPED_INSUFFICIENT_CASH":
        return "Dropped, insufficient cash", "#e67e22"
    if status.startswith("ERROR"):
        return f"Rejected, {status[len('ERROR: '):]}", "#c0392b"
    if status in ("Cancelled", "Inactive"):
        return f"{status}, not filled", "#c0392b"
    return f"Still open, status {status}", "#e67e22"


def build_rebalance_summary_html(portfolio_name: str, orders: dict, dry_run: bool = False) -> str:
    """
    Standard-category HTML table summarizing a rebalance's BUY/SELL/HOLD decisions, plus
    a "What Actually Happened" column showing the REAL execution outcome per ticker (filled,
    dropped, still open, rejected, dry-run, ...), distinct from the signal's intended
    action/reason, since a live order can be intended but never actually fill.

    dry_run should be True whenever this rebalance ran without --live (no orders were ever
    sent to IBKR), so the new column reads correctly as "Dry-run" rather than "No order sent".
    """
    rows = ""
    for ticker, order in orders.items():
        action_color = {"BUY": "#27ae60", "SELL": "#c0392b", "HOLD": "#7f8c8d"}.get(order["action"], "#333")
        outcome_text, outcome_color = _describe_fill_outcome(order, dry_run)
        rows += (
            f"<tr><td style='padding:4px 8px;'>{ticker}</td>"
            f"<td style='padding:4px 8px; color:{action_color}; font-weight:bold;'>{order['action']}</td>"
            f"<td style='padding:4px 8px;'>{order.get('shares', 0)}</td>"
            f"<td style='padding:4px 8px;'>{order.get('reason', '')}</td>"
            f"<td style='padding:4px 8px; color:{outcome_color};'>{outcome_text}</td></tr>"
        )
    return f"""
    <h3>Rebalance Summary: {portfolio_name}</h3>
    <table style="border-collapse: collapse; width: 100%;">
      <tr style="background:#f4f4f4;"><th style='padding:4px 8px; text-align:left;'>Ticker</th>
          <th style='padding:4px 8px; text-align:left;'>Action</th>
          <th style='padding:4px 8px; text-align:left;'>Shares</th>
          <th style='padding:4px 8px; text-align:left;'>Reason</th>
          <th style='padding:4px 8px; text-align:left;'>What Actually Happened</th></tr>
      {rows}
    </table>
    """


def build_no_action_summary_html(portfolio_name: str) -> str:
    """
    Standard-category HTML notice for a rebalance day (or --force-rebalance) that ran to
    completion but produced zero orders (e.g. AGGREGATE_DRIFT_SKIP, or every computed drift
    fell below min_trade_size). Reuses the same rich-HTML look as
    build_rebalance_summary_html() rather than a bare plain-text line, so this portfolio's
    "we checked, nothing to report" confirmation reads consistently with every other
    portfolio email. The specific reason (if any) is already in logs/alerts_log.csv via
    log_alert(), not repeated here.
    """
    return f"""
    <h3>Rebalance Summary: {portfolio_name}</h3>
    <p>This portfolio's rebalance ran to completion today with no order changes, every
    computed drift was either zero or below the configured trading thresholds. See
    logs/alerts_log.csv for this portfolio if a specific skip reason (e.g. aggregate drift
    below threshold) was logged.</p>
    """


def build_comparison_bar_chart(window_data: dict, title: str) -> bytes | None:
    """
    Grouped bar chart, portfolio vs. benchmark return per trailing window, one bar-pair per
    window label. Shared by both the monthly report (core/functions.py's trailing_returns(),
    windows like "1 Month"/"3 Month"/"6 Month"/"YTD"/"1 Year") and the daily report
    (core/functions_quant_extensions.py's daily_window_comparison(), windows like "1 Day"/
    "1 Week"/"2 Week"/"3 Week"), both are normalized to the same shape before reaching here:
    {window_label: {"portfolio": fraction, "benchmark": fraction}, ...}, plus non-window keys
    like "as_of_date"/"error" which are ignored (not plotted).

    Returns None (not an exception) if matplotlib is unavailable or there are no plottable
    windows yet (e.g. a portfolio too new for even a "1 Month" comparison), same graceful-
    degradation contract as the existing portfolio-value chart below.
    """
    labels = [k for k, v in window_data.items() if isinstance(v, dict) and "portfolio" in v]
    if not labels:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io

        portfolio_vals = [window_data[l]["portfolio"] * 100 for l in labels]
        benchmark_vals = [window_data[l]["benchmark"] * 100 for l in labels]

        x = list(range(len(labels)))
        width = 0.35
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar([i - width / 2 for i in x], portfolio_vals, width, label="Portfolio", color="#2980b9")
        ax.bar([i + width / 2 for i in x], benchmark_vals, width, label="Benchmark", color="#95a5a6")
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_ylabel("Return (%)")
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except ImportError:
        logger.warning("matplotlib not available, comparison chart omitted.")
        return None


def _build_value_chart(snapshot_df: pd.DataFrame, title: str) -> bytes | None:
    """The portfolio-value-over-time line chart, factored out so both the monthly and daily
    report builders share the exact same charting code instead of two copies drifting apart."""
    if len(snapshot_df) < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(snapshot_df["date"], snapshot_df["total_value"], marker="o", color="#2980b9")
        ax.set_title(title)
        ax.set_ylabel("$")
        fig.autofmt_xdate()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except ImportError:
        logger.warning("matplotlib not available, value chart omitted.")
        return None


def _strategy_stats_rows(since_inception: dict | None) -> str:
    """Total Return/CAGR/Max Drawdown/Std Dev/Sharpe/Sortino table rows, shared by both report
    builders. since_inception is functions_quant_extensions.py's since_inception_performance()
    output, any stat that's None (e.g. Sharpe/Sortino before a year of history exists) shows
    as 'Not enough history yet' rather than a blank cell or a crash."""
    if since_inception is None or "error" in since_inception:
        return ""

    def _fmt_pct(key: str) -> str:
        v = since_inception.get(key)
        return f"{v:+.2%}" if v is not None else "Not enough history yet"

    def _fmt_ratio(key: str) -> str:
        v = since_inception.get(key)
        return f"{v:.2f}" if v is not None else "Not enough history yet"

    inception = since_inception.get("inception_date")
    inception_str = inception.strftime("%Y-%m-%d") if inception is not None else "?"
    rows = f"""
    <tr><td style='padding:4px 8px;'>Since Inception</td><td style='padding:4px 8px;'>{inception_str}</td></tr>
    <tr><td style='padding:4px 8px;'>Total Return</td><td style='padding:4px 8px;'>{_fmt_pct('total_return')}</td></tr>
    <tr><td style='padding:4px 8px;'>CAGR</td><td style='padding:4px 8px;'>{_fmt_pct('cagr')}</td></tr>
    <tr><td style='padding:4px 8px;'>Max Drawdown</td><td style='padding:4px 8px;'>{_fmt_pct('max_drawdown')}</td></tr>
    <tr><td style='padding:4px 8px;'>Standard Deviation (annualized)</td><td style='padding:4px 8px;'>{_fmt_pct('std_dev')}</td></tr>
    <tr><td style='padding:4px 8px;'>Sharpe Ratio</td><td style='padding:4px 8px;'>{_fmt_ratio('sharpe_ratio')}</td></tr>
    <tr><td style='padding:4px 8px;'>Sortino Ratio</td><td style='padding:4px 8px;'>{_fmt_ratio('sortino_ratio')}</td></tr>
    """
    return f"""
    <h3>Strategy Performance (Since Inception)</h3>
    <table style="border-collapse: collapse;">{rows}</table>
    """


def _technical_indicators_html(indicators: dict[str, dict] | None) -> str:
    """One row per held ticker, one column per indicator, indicators is
    {ticker: core/technical_indicators.py's compute_latest_indicators() output}. Tickers with
    too little OHLCV history to compute indicators yet (empty dict) are omitted from the table
    entirely rather than shown with blank cells."""
    if not indicators:
        return ""
    tickers_with_data = {t: v for t, v in indicators.items() if v}
    if not tickers_with_data:
        return ""

    cols = ["sma_20", "ema_20", "rsi_14", "macd", "atr_14", "bollinger_upper", "bollinger_lower",
            "std_dev_20", "adx_14", "vwap", "obv"]
    header = "".join(f"<th style='padding:4px 8px; text-align:left;'>{c}</th>" for c in cols)
    body_rows = ""
    for ticker, vals in tickers_with_data.items():
        cells = "".join(
            f"<td style='padding:4px 8px;'>{vals[c]:,.2f}</td>" if c in vals else "<td></td>"
            for c in cols
        )
        body_rows += f"<tr><td style='padding:4px 8px;'><b>{ticker}</b></td>{cells}</tr>"

    return f"""
    <h3>Technical Indicators (held positions)</h3>
    <table style="border-collapse: collapse; font-size: 12px;">
      <tr><th style='padding:4px 8px; text-align:left;'>Ticker</th>{header}</tr>
      {body_rows}
    </table>
    """


def _fundamental_indicators_html(fundamentals: dict[str, dict] | None) -> str:
    """One row per held ticker, one column per indicator, fundamentals is
    {ticker: core/fundamentals.py's get_cached_or_fetch_fundamentals() output}. A ticker with no
    fundamentals access from either vendor (empty dict, e.g. your FMP/EODHD plan doesn't
    include fundamentals) is omitted from the table entirely, same contract as
    _technical_indicators_html() above. The whole section is omitted if no ticker has any data."""
    if not fundamentals:
        return ""
    tickers_with_data = {t: v for t, v in fundamentals.items() if v}
    if not tickers_with_data:
        return ""

    cols = [("pe_ratio", "P/E"), ("peg_ratio", "PEG"), ("roe", "ROE"),
            ("debt_to_equity", "Debt/Equity"), ("current_ratio", "Current Ratio")]
    header = "".join(f"<th style='padding:4px 8px; text-align:left;'>{label}</th>" for _, label in cols)
    body_rows = ""
    for ticker, vals in tickers_with_data.items():
        cells = "".join(
            f"<td style='padding:4px 8px;'>{vals[key]:,.2f}</td>" if vals.get(key) is not None else "<td>—</td>"
            for key, _ in cols
        )
        body_rows += f"<tr><td style='padding:4px 8px;'><b>{ticker}</b></td>{cells}</tr>"

    return f"""
    <h3>Fundamental Indicators (held positions)</h3>
    <table style="border-collapse: collapse; font-size: 12px;">
      <tr><th style='padding:4px 8px; text-align:left;'>Ticker</th>{header}</tr>
      {body_rows}
    </table>
    """


def _macro_indicators_html(macro: dict | None) -> str:
    """Portfolio-independent, one Fed Funds Rate / CPI reading per report, not per ticker,
    since macro conditions apply market-wide. macro is core/macro_data.py's
    get_cached_or_fetch_macro_indicators() output: {"fed_funds_rate": {"value":..., "date":...},
    "cpi": {"value":..., "date":...}}. Omitted entirely if empty (FRED_API_KEY not configured,
    or both FRED calls failed)."""
    if not macro:
        return ""

    rows = ""
    if macro.get("fed_funds_rate"):
        ffr = macro["fed_funds_rate"]
        rows += (f"<tr><td style='padding:4px 8px;'>Fed Funds Rate</td>"
                 f"<td style='padding:4px 8px;'>{ffr['value']:.2f}%</td>"
                 f"<td style='padding:4px 8px; color:#999;'>as of {ffr['date']}</td></tr>")
    if macro.get("cpi"):
        cpi = macro["cpi"]
        rows += (f"<tr><td style='padding:4px 8px;'>CPI</td>"
                 f"<td style='padding:4px 8px;'>{cpi['value']:.2f}</td>"
                 f"<td style='padding:4px 8px; color:#999;'>as of {cpi['date']}</td></tr>")
    if not rows:
        return ""

    return f"""
    <h3>Macro Context</h3>
    <table style="border-collapse: collapse;">{rows}</table>
    """


def _position_performance_html(position_performance: dict[str, dict] | None) -> str:
    """One row per held ticker, position_performance is
    execution/live_signal.py's build_position_performance() output: return since entry on the
    CURRENTLY open position (unrealized, mark-to-market), distinct from the "Actual P&L"
    section above (which is realized+unrealized P&L across the whole trade history, including
    closed lots). Omitted entirely if not provided or empty, e.g. dry-run mode, where
    current_positions is never populated from a real broker connection."""
    if not position_performance:
        return ""

    body_rows = ""
    for ticker, vals in position_performance.items():
        entry_date = vals.get("entry_date")
        entry_date_str = entry_date.strftime("%Y-%m-%d") if entry_date is not None else "Unknown"
        return_color = "#0a7d2c" if vals["return_pct"] >= 0 else "#c0392b"
        body_rows += (
            f"<tr>"
            f"<td style='padding:4px 8px;'><b>{ticker}</b></td>"
            f"<td style='padding:4px 8px;'>{entry_date_str}</td>"
            f"<td style='padding:4px 8px;'>${vals['entry_price']:,.2f}</td>"
            f"<td style='padding:4px 8px;'>${vals['current_price']:,.2f}</td>"
            f"<td style='padding:4px 8px;'>{vals['shares']:,.4f}</td>"
            f"<td style='padding:4px 8px; color:{return_color};'>{vals['return_pct']:+.2%}</td>"
            f"<td style='padding:4px 8px;'>${vals['market_value']:,.2f}</td>"
            f"</tr>"
        )

    return f"""
    <h3>Position Performance (since entry)</h3>
    <table style="border-collapse: collapse; font-size: 12px;">
      <tr>
        <th style='padding:4px 8px; text-align:left;'>Ticker</th>
        <th style='padding:4px 8px; text-align:left;'>Entry Date</th>
        <th style='padding:4px 8px; text-align:left;'>Entry Price</th>
        <th style='padding:4px 8px; text-align:left;'>Current Price</th>
        <th style='padding:4px 8px; text-align:left;'>Shares</th>
        <th style='padding:4px 8px; text-align:left;'>Return</th>
        <th style='padding:4px 8px; text-align:left;'>Market Value</th>
      </tr>
      {body_rows}
    </table>
    """


def _build_report_html(
    portfolio_name: str, report_label: str, period_line: str, snapshot_df: pd.DataFrame,
    comparison: dict, real_pnl: dict | None = None, since_inception: dict | None = None,
    window_comparison: dict | None = None, indicators: dict[str, dict] | None = None,
    fundamentals: dict[str, dict] | None = None, macro: dict | None = None,
    position_performance: dict[str, dict] | None = None,
) -> tuple[str, bytes | None, bytes | None]:
    """
    Shared HTML/chart builder for both the monthly and daily reports (build_monthly_report_html()
    / build_daily_report_html() below are thin wrappers over this), the two reports differ only
    in cadence and which trailing windows their `comparison`/`window_comparison` dicts cover, not
    in structure, so this stays as one implementation rather than two copies that could drift
    apart. Returns (html_body, value_chart_bytes_or_None, comparison_chart_bytes_or_None).

    Both chart_bytes values are None if matplotlib isn't available or there's not enough data
    to chart yet, the HTML report still renders without them in that case, degrading
    gracefully rather than failing the whole report.

    real_pnl : dict, optional
        Output of execution/live_signal.py's measure_live_performance(),
        REAL realized+unrealized P&L from FIFO-matched trade log rows, distinct
        from "Current Position"'s unrealized_pnl below (which only marks
        currently-open positions from the latest snapshot, not cumulative
        realized gains from trades that have since closed). Omitted from the
        report if not provided (e.g. no trade log exists yet this period).
    since_inception : dict, optional
        core/functions_quant_extensions.py's since_inception_performance() output, Total
        Return/CAGR/Max Drawdown/Std Dev/Sharpe/Sortino since the first snapshot. Omitted
        section if not provided.
    window_comparison : dict, optional
        {window_label: {"portfolio": fraction, "benchmark": fraction}}, e.g. trailing_returns()'s
        "1 Month"/"3 Month"/etc. columns for the monthly report, or
        daily_window_comparison()'s "1 Day"/"1 Week"/etc. for the daily report, reshaped into
        this uniform dict shape. Charted via build_comparison_bar_chart(); omitted if not
        provided or empty.
    indicators : dict, optional
        {ticker: core/technical_indicators.py's compute_latest_indicators() output} for the
        portfolio's currently held positions. Omitted section if not provided.
    fundamentals : dict, optional
        {ticker: core/fundamentals.py's get_cached_or_fetch_fundamentals() output} for the
        portfolio's currently held positions. Omitted section if not provided, or if no ticker
        has any data (e.g. your FMP/EODHD plan doesn't include fundamentals access).
    macro : dict, optional
        core/macro_data.py's get_cached_or_fetch_macro_indicators() output (Fed Funds Rate,
        CPI), portfolio-independent, the same dict is passed to every portfolio's report in a
        given run. Omitted section if not provided (e.g. FRED_API_KEY not configured).
    position_performance : dict, optional
        {ticker: execution/live_signal.py's build_position_performance() output}, per-ticker
        return since entry on the currently open position (entry date/price, current price,
        shares, return %, market value). Distinct from real_pnl above (realized+unrealized P&L
        across the whole trade history, including closed lots), this is unrealized,
        mark-to-market return on what's open right now. Omitted section if not provided (e.g.
        dry-run mode, where current_positions is never populated from a real broker).
    """
    value_chart_bytes = _build_value_chart(snapshot_df, f"{portfolio_name}: Portfolio Value")
    comparison_chart_bytes = (
        build_comparison_bar_chart(window_comparison, f"{portfolio_name}: vs. Benchmark")
        if window_comparison else None
    )

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

    real_pnl_rows = ""
    if real_pnl is not None:
        real_pnl_rows = f"""
        <tr><td style='padding:4px 8px;'>Realized P&L</td><td style='padding:4px 8px;'>${real_pnl['realized_pnl']:,.2f}</td></tr>
        <tr><td style='padding:4px 8px;'>Unrealized P&L</td><td style='padding:4px 8px;'>${real_pnl['unrealized_pnl']:,.2f}</td></tr>
        <tr><td style='padding:4px 8px;'>Total P&L</td><td style='padding:4px 8px;'>${real_pnl['total_pnl']:,.2f}</td></tr>
        <tr><td style='padding:4px 8px;'>Trade Count</td><td style='padding:4px 8px;'>{real_pnl['trade_count']}</td></tr>
        {"<tr><td style='padding:4px 8px;'>Total Return</td><td style='padding:4px 8px;'>" + f"{real_pnl['total_return_pct']:+.2%}" + "</td></tr>" if "total_return_pct" in real_pnl else ""}
        """
    real_pnl_html = f"""
    <h3>Actual P&L (from trade log, FIFO)</h3>
    <table style="border-collapse: collapse;">{real_pnl_rows}</table>
    """ if real_pnl is not None else ""

    value_chart_html = '<img src="cid:portfolio_chart" style="max-width:100%;">' if value_chart_bytes else ""
    comparison_chart_html = '<img src="cid:comparison_chart" style="max-width:100%;">' if comparison_chart_bytes else ""
    strategy_stats_html = _strategy_stats_rows(since_inception)
    indicators_html = _technical_indicators_html(indicators)
    fundamentals_html = _fundamental_indicators_html(fundamentals)
    macro_html = _macro_indicators_html(macro)
    position_performance_html = _position_performance_html(position_performance)

    html = f"""
    <h2>{report_label} Report: {portfolio_name}</h2>
    <p>{period_line}</p>
    {value_chart_html}
    <h3>Current Position</h3>
    <table style="border-collapse: collapse;">{summary_rows}</table>
    {real_pnl_html}
    {position_performance_html}
    {strategy_stats_html}
    <h3>Performance vs. Benchmark</h3>
    <table style="border-collapse: collapse;">{comparison_rows}</table>
    {comparison_chart_html}
    {indicators_html}
    {fundamentals_html}
    {macro_html}
    <p style="color:#999; font-size:11px;">This report reflects backtested/paper/live results as configured,
    verify which mode this portfolio is running in before treating these numbers as real returns.</p>
    """
    return html, value_chart_bytes, comparison_chart_bytes


def build_monthly_report_html(
    portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict,
    real_pnl: dict | None = None, since_inception: dict | None = None,
    window_comparison: dict | None = None, indicators: dict[str, dict] | None = None,
    fundamentals: dict[str, dict] | None = None, macro: dict | None = None,
    position_performance: dict[str, dict] | None = None,
) -> tuple[str, bytes | None, bytes | None]:
    """Monthly cadence, see _build_report_html() for the full parameter docs (shared)."""
    return _build_report_html(
        portfolio_name, "Monthly", f"Period ending {datetime.now().strftime('%Y-%m-%d')}",
        snapshot_df, comparison, real_pnl, since_inception, window_comparison, indicators,
        fundamentals, macro, position_performance,
    )


def build_daily_report_html(
    portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict,
    real_pnl: dict | None = None, since_inception: dict | None = None,
    window_comparison: dict | None = None, indicators: dict[str, dict] | None = None,
    fundamentals: dict[str, dict] | None = None, macro: dict | None = None,
    position_performance: dict[str, dict] | None = None,
) -> tuple[str, bytes | None, bytes | None]:
    """
    Daily cadence, same content depth as the monthly report (technical indicators, since-
    inception strategy stats, benchmark comparison chart), generated every day instead of
    monthly. Gated behind config.yaml's notifications.send_daily (default False) precisely
    because of this depth, see docs/EMAIL_REPORTING.md. See _build_report_html() for the full
    parameter docs (shared); window_comparison here is expected to be
    core/functions_quant_extensions.py's daily_window_comparison() output ("1 Day"/"1 Week"/
    "2 Week"/"3 Week"), not trailing_returns()'s monthly windows.
    """
    return _build_report_html(
        portfolio_name, "Daily", f"As of {datetime.now().strftime('%Y-%m-%d')}",
        snapshot_df, comparison, real_pnl, since_inception, window_comparison, indicators,
        fundamentals, macro, position_performance,
    )


def send_monthly_report(
    portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict,
    notification_config: dict, real_pnl: dict | None = None,
    since_inception: dict | None = None, window_comparison: dict | None = None,
    indicators: dict[str, dict] | None = None,
    fundamentals: dict[str, dict] | None = None, macro: dict | None = None,
    position_performance: dict[str, dict] | None = None,
) -> bool:
    """PERIODIC category, filterable, but distinct from CRITICAL/STANDARD filtering."""
    html, value_chart_bytes, comparison_chart_bytes = build_monthly_report_html(
        portfolio_name, snapshot_df, comparison, real_pnl,
        since_inception, window_comparison, indicators, fundamentals, macro,
        position_performance,
    )

    if not should_send(NotificationCategory.PERIODIC, notification_config):
        logger.info("Monthly report filtered by config: %s", portfolio_name)
        return False

    smtp = _smtp_config()
    if smtp is None:
        logger.error("SMTP not configured, monthly report NOT SENT for %s", portfolio_name)
        return False

    msg = MIMEMultipart("related")
    msg["Subject"] = f"[PERIODIC] Monthly Report: {portfolio_name}"
    msg["From"] = smtp["user"]
    msg["To"] = smtp["to"]
    msg["X-Momentum-Trading-Bot"] = "1"
    msg.attach(MIMEText(html, "html"))
    if value_chart_bytes:
        img = MIMEImage(value_chart_bytes)
        img.add_header("Content-ID", "<portfolio_chart>")
        msg.attach(img)
    if comparison_chart_bytes:
        img = MIMEImage(comparison_chart_bytes)
        img.add_header("Content-ID", "<comparison_chart>")
        msg.attach(img)

    def _do_send():
        with smtp_connect(smtp["host"], smtp["port"]) as server:
            authenticate_smtp(server, smtp["user"], smtp["password"])
            server.sendmail(smtp["user"], [smtp["to"]], msg.as_string())

    try:
        send_with_retry(_do_send)
        logger.info("Monthly report sent: %s", portfolio_name)
        return True
    except Exception as e:
        logger.error("Failed to send monthly report for %s: %s", portfolio_name, e)
        return False


def send_daily_report(
    portfolio_name: str, snapshot_df: pd.DataFrame, comparison: dict,
    notification_config: dict, real_pnl: dict | None = None,
    since_inception: dict | None = None, window_comparison: dict | None = None,
    indicators: dict[str, dict] | None = None,
    fundamentals: dict[str, dict] | None = None, macro: dict | None = None,
    position_performance: dict[str, dict] | None = None,
) -> bool:
    """DAILY category, filterable via notifications.send_daily, defaults to NOT sending
    unless explicitly enabled (see should_send()'s per-category default). Structurally
    identical to send_monthly_report(), same graceful degradation, same two-image MIME
    attachment pattern, just a different category/subject and (typically) different-shaped
    comparison/window_comparison inputs (daily windows, not monthly)."""
    html, value_chart_bytes, comparison_chart_bytes = build_daily_report_html(
        portfolio_name, snapshot_df, comparison, real_pnl,
        since_inception, window_comparison, indicators, fundamentals, macro,
        position_performance,
    )

    if not should_send(NotificationCategory.DAILY, notification_config):
        logger.info("Daily report filtered by config: %s", portfolio_name)
        return False

    smtp = _smtp_config()
    if smtp is None:
        logger.error("SMTP not configured, daily report NOT SENT for %s", portfolio_name)
        return False

    msg = MIMEMultipart("related")
    msg["Subject"] = f"[DAILY] Daily Report: {portfolio_name}"
    msg["From"] = smtp["user"]
    msg["To"] = smtp["to"]
    msg["X-Momentum-Trading-Bot"] = "1"
    msg.attach(MIMEText(html, "html"))
    if value_chart_bytes:
        img = MIMEImage(value_chart_bytes)
        img.add_header("Content-ID", "<portfolio_chart>")
        msg.attach(img)
    if comparison_chart_bytes:
        img = MIMEImage(comparison_chart_bytes)
        img.add_header("Content-ID", "<comparison_chart>")
        msg.attach(img)

    def _do_send():
        with smtp_connect(smtp["host"], smtp["port"]) as server:
            authenticate_smtp(server, smtp["user"], smtp["password"])
            server.sendmail(smtp["user"], [smtp["to"]], msg.as_string())

    try:
        send_with_retry(_do_send)
        logger.info("Daily report sent: %s", portfolio_name)
        return True
    except Exception as e:
        logger.error("Failed to send daily report for %s: %s", portfolio_name, e)
        return False
