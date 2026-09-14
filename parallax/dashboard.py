from __future__ import annotations

from html import escape
from typing import Any

REFRESH_INTERVAL_MS = 10_000


def render_dashboard(
    inbox: dict[str, Any],
    health: dict[str, Any],
    alerts: dict[str, Any],
    track_record: dict[str, Any],
    social: dict[str, Any] | None = None,
) -> str:
    summary = inbox.get("summary", {})
    items = [
        item
        for item in inbox.get("items", [])
        if item.get("status") == "ACTIVE"
        and item.get("attention_class")
        in {"ACTIONABLE_PLAY", "PUBLIC_WORTHY", "PRIORITY_WATCH"}
    ]
    attention_count = sum(
        int(summary.get(key, 0) or 0)
        for key in ("actionable_plays", "public_worthy", "priority_watch")
    )
    empty_state = (
        """
        <section class="empty-state" data-testid="empty-state">
          <h2>NOTHING NEEDS YOUR ATTENTION</h2>
          <p>PARALLAX is monitoring the markets and filtering routine activity.</p>
        </section>
        """
        if attention_count == 0
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PARALLAX Operator Dashboard</title>
  <style>{_css()}</style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">Swarm Axis</p>
        <h1>PARALLAX Operator Dashboard</h1>
      </div>
      <div class="refresh">Refreshes every 10 seconds</div>
    </header>

    <section class="summary-grid" aria-label="Inbox summary">
      {_summary_card("ACTIONABLE", summary.get("actionable_plays", 0), "actionable")}
      {_summary_card("PUBLIC-WORTHY", summary.get("public_worthy", 0), "public")}
      {_summary_card("PRIORITY WATCH", summary.get("priority_watch", 0), "watch")}
      {_summary_card("FILTERED", summary.get("suppressed", 0), "filtered")}
    </section>

    <section id="attention" class="attention">
      {empty_state}
      <div id="inbox-list" class="inbox-list">
        {"".join(_render_card(item) for item in items)}
      </div>
    </section>

    <section class="lower-grid">
      {_system_status(health)}
      {_alert_status(alerts)}
      {_track_record(track_record)}
      {_social_status(social or health.get("social") or {})}
    </section>
  </main>
  <script>{_js()}</script>
</body>
</html>
"""


def _summary_card(label: str, value: Any, tone: str) -> str:
    return f"""
    <article class="summary-card {tone}">
      <div>{escape(label)}</div>
      <strong>{escape(str(value))}</strong>
    </article>
    """


def _render_card(item: dict[str, Any]) -> str:
    attention_class = str(item.get("attention_class", ""))
    if attention_class == "ACTIONABLE_PLAY":
        return _actionable_card(item)
    if attention_class == "PUBLIC_WORTHY":
        return _public_card(item)
    if attention_class == "PRIORITY_WATCH":
        return _watch_card(item)
    return ""


def _actionable_card(item: dict[str, Any]) -> str:
    verdict = str(item.get("verdict", ""))
    side = "BUY YES" if verdict == "BUY YES" else "BUY NO" if verdict == "BUY NO" else verdict
    current_price = _current_executable_price(item)
    price_row = (
        f"<dt>Current executable price</dt><dd>{escape(current_price)}</dd>"
        if current_price
        else ""
    )
    return f"""
    <article class="inbox-card actionable" data-inbox-id="{escape(str(item.get("inbox_id", "")))}">
      <div class="card-header">
        <p class="card-kicker">PARALLAX ACTIONABLE PLAY</p>
        {_seen_badge(item)}
      </div>
      <h2>{escape(str(item.get("market", item.get("display_title", ""))))}</h2>
      <div class="verdict buy">{escape(side)}</div>
      <dl class="facts">
        <dt>Trade confidence</dt><dd>{escape(str(item.get("trade_confidence", "")))}</dd>
        {price_row}
      </dl>
      {_field("Plain-English reason", item.get("why") or item.get("what_it_means"))}
      {_field("Operator instruction", item.get("operator_instruction"))}
      {_economics(item.get("economics"))}
      {_list_field("Risks", item.get("risks"))}
      {_list_field("Invalidation", item.get("invalidation"))}
      <p class="disclosure">Economics are before fees/costs.</p>
      {_seen_button(item)}
    </article>
    """


def _public_card(item: dict[str, Any]) -> str:
    source = item.get("source_signal") or {}
    return f"""
    <article class="inbox-card public-worthy" data-inbox-id="{escape(str(item.get("inbox_id", "")))}">
      <div class="card-header">
        <p class="card-kicker">PARALLAX PUBLIC-WORTHY</p>
        {_seen_badge(item)}
      </div>
      <h2>{escape(str(item.get("market", item.get("display_title", ""))))}</h2>
      <dl class="facts">
        <dt>Signal type</dt><dd>{escape(str(source.get("signal_type", "")))}</dd>
        <dt>Verdict</dt><dd>{escape(str(item.get("verdict", "")))}</dd>
        <dt>Trade confidence</dt><dd>{escape(str(item.get("trade_confidence", "")))}</dd>
      </dl>
      {_field("What happened", item.get("what_happened"))}
      {_field("What it means", item.get("what_it_means"))}
      <p class="not-buy">NOT A BUY RECOMMENDATION</p>
      {_field("Operator instruction", item.get("operator_instruction"))}
      {_seen_button(item)}
    </article>
    """


def _watch_card(item: dict[str, Any]) -> str:
    return f"""
    <article class="inbox-card priority-watch" data-inbox-id="{escape(str(item.get("inbox_id", "")))}">
      <div class="card-header">
        <p class="card-kicker">PARALLAX PRIORITY WATCH</p>
        {_seen_badge(item)}
      </div>
      <h2>{escape(str(item.get("market", item.get("display_title", ""))))}</h2>
      {_field("What happened", item.get("what_happened"))}
      {_field("What it means", item.get("what_it_means"))}
      <dl class="facts">
        <dt>Trade confidence</dt><dd>{escape(str(item.get("trade_confidence", "")))}</dd>
      </dl>
      {_field("Operator instruction", item.get("operator_instruction"))}
      {_seen_button(item)}
    </article>
    """


def _field(label: str, value: Any) -> str:
    if value in (None, "", []):
        return ""
    return f"""
    <section class="field">
      <h3>{escape(label)}</h3>
      <p>{escape(str(value))}</p>
    </section>
    """


def _list_field(label: str, value: Any) -> str:
    if not value:
        return ""
    values = value if isinstance(value, (list, tuple)) else (value,)
    rows = "".join(f"<li>{escape(str(row))}</li>" for row in values)
    return f"""
    <section class="field">
      <h3>{escape(label)}</h3>
      <ul>{rows}</ul>
    </section>
    """


def _economics(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    examples = value.get("examples") or []
    rows = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        stake = _money(example.get("stake"))
        if not example.get("available", True):
            rows.append(
                f"<tr><th>{escape(stake)}</th><td colspan=\"3\">{escape(str(example.get('reason', 'Unavailable')))}</td></tr>"
            )
            continue
        rows.append(
            "<tr>"
            f"<th>{escape(stake)} economics</th>"
            f"<td>{escape(_money(example.get('gross_profit_if_correct')))}</td>"
            f"<td>{escape(_money(example.get('maximum_loss')))}</td>"
            f"<td>{escape('before fees/costs' if example.get('before_fees_costs') else '')}</td>"
            "</tr>"
        )
    return f"""
    <section class="field economics">
      <h3>{escape(str(value.get("label", "Economics")))}</h3>
      <table>
        <thead><tr><th>Stake</th><th>Potential gross profit</th><th>Maximum loss</th><th>Disclosure</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <p>{escape(str(value.get("risk_reward_explanation", "")))}</p>
    </section>
    """


def _system_status(health: dict[str, Any]) -> str:
    metrics = health.get("metrics") or {}
    status = "LIVE" if health.get("status") == "ok" else "DEGRADED" if health.get("status") == "degraded" else "ERROR"
    return f"""
    <section class="panel">
      <h2>PARALLAX ENGINE</h2>
      <strong class="engine-state">{escape(status)}</strong>
      <dl class="facts compact">
        <dt>POLYMARKET</dt><dd>{escape(str(metrics.get("POLYMARKET.markets_observed", 0)))} markets observed</dd>
        <dt>KALSHI</dt><dd>{escape(str(metrics.get("KALSHI.markets_observed", 0)))} markets observed</dd>
        <dt>LAST REFRESH</dt><dd>{escape(str(health.get("last_refresh") or "unknown"))}</dd>
        <dt>ALERT DELIVERY</dt><dd>{escape(str(health.get("alert_mode") or "disabled"))}</dd>
        <dt>LIVE ORDERS</dt><dd>{escape(str(health.get("live_orders")))}</dd>
        <dt>EXECUTION ENABLED</dt><dd>{escape(str(health.get("execution_enabled")).lower())}</dd>
      </dl>
    </section>
    """


def _alert_status(alerts: dict[str, Any]) -> str:
    return f"""
    <section class="panel">
      <h2>Alert Status</h2>
      <dl class="facts compact">
        <dt>mode</dt><dd>{escape(str(alerts.get("mode", "disabled")))}</dd>
        <dt>pending</dt><dd>{escape(str(alerts.get("pending", 0)))}</dd>
        <dt>sent</dt><dd>{escape(str(alerts.get("sent", 0)))}</dd>
        <dt>failed</dt><dd>{escape(str(alerts.get("failed", 0)))}</dd>
        <dt>unknown</dt><dd>{escape(str(alerts.get("unknown", 0)))}</dd>
        <dt>last_delivery_at</dt><dd>{escape(str(alerts.get("last_delivery_at") or "never"))}</dd>
      </dl>
    </section>
    """


def _track_record(track_record: dict[str, Any]) -> str:
    published = int(track_record.get("published_plays", 0) or 0)
    if published == 0:
        body = "<p class=\"muted\">NO PUBLISHED PARALLAX PLAYS YET</p>"
    else:
        body = f"""
        <dl class="facts compact">
          <dt>published</dt><dd>{escape(str(published))}</dd>
          <dt>wins</dt><dd>{escape(str(track_record.get("wins", 0)))}</dd>
          <dt>losses</dt><dd>{escape(str(track_record.get("losses", 0)))}</dd>
          <dt>voids</dt><dd>{escape(str(track_record.get("voids", 0)))}</dd>
        </dl>
        """
    return f"""
    <section class="panel">
      <h2>Track Record</h2>
      {body}
    </section>
    """


def _social_status(social: dict[str, Any]) -> str:
    platforms = social.get("platforms") or {}
    def state(name: str) -> str:
        return str((platforms.get(name) or {}).get("status", "disabled")).upper()
    return f"""
    <section class="panel social-status">
      <h2>SOCIAL PUBLISHER</h2>
      <dl class="facts compact">
        <dt>mode</dt><dd>{escape(str(social.get("mode", "disabled")))}</dd>
        <dt>X</dt><dd>{escape(state("x"))}</dd>
        <dt>LinkedIn</dt><dd>{escape(state("linkedin"))}</dd>
        <dt>Instagram</dt><dd>{escape(state("instagram"))}</dd>
        <dt>sent count</dt><dd>{escape(str(social.get("sent", 0)))}</dd>
        <dt>failed count</dt><dd>{escape(str(social.get("failed", 0)))}</dd>
      </dl>
    </section>
    """


def _current_executable_price(item: dict[str, Any]) -> str | None:
    verdict = item.get("verdict")
    side = "YES" if verdict == "BUY YES" else "NO" if verdict == "BUY NO" else None
    prices = ((item.get("current_market") or {}).get("current_executable_buy_prices") or {})
    if side is None or prices.get(side) is None:
        return None
    return _cents(prices[side])


def _seen_badge(item: dict[str, Any]) -> str:
    return "<span class=\"seen\">Seen</span>" if item.get("seen") else "<span class=\"unseen\">Unseen</span>"


def _seen_button(item: dict[str, Any]) -> str:
    if item.get("seen"):
        return ""
    inbox_id = escape(str(item.get("inbox_id", "")))
    return f"<button type=\"button\" class=\"mark-seen\" data-inbox-id=\"{inbox_id}\">MARK SEEN</button>"


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "unknown"


def _cents(value: Any) -> str:
    try:
        cents = round(float(value) * 100, 10)
    except (TypeError, ValueError):
        return "unknown"
    return f"{cents:.0f}¢" if cents.is_integer() else f"{cents:.1f}¢"


def _css() -> str:
    return """
:root { color-scheme: light dark; --bg: #f7f7f4; --panel: #ffffff; --ink: #141414; --muted: #62645f; --line: #d9d9d2; --action: #0d5f4b; --public: #315a7d; --watch: #755f21; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
.shell { width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 40px; }
.topbar { display: flex; justify-content: space-between; gap: 20px; align-items: end; margin-bottom: 22px; }
.eyebrow { margin: 0 0 4px; color: var(--muted); text-transform: uppercase; font-size: 12px; letter-spacing: 0; }
h1 { margin: 0; font-size: 30px; line-height: 1.1; }
h2 { margin: 0; font-size: 20px; line-height: 1.2; }
h3 { margin: 0 0 6px; font-size: 13px; text-transform: uppercase; color: var(--muted); letter-spacing: 0; }
.refresh, .muted { color: var(--muted); }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.summary-card { background: var(--panel); border: 1px solid var(--line); border-top: 4px solid var(--muted); border-radius: 8px; padding: 14px; min-height: 104px; display: flex; flex-direction: column; justify-content: space-between; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
.summary-card strong { color: var(--ink); font-size: 38px; line-height: 1; }
.summary-card.actionable { border-top-color: var(--action); }
.summary-card.public { border-top-color: var(--public); }
.summary-card.watch { border-top-color: var(--watch); }
.attention { margin-bottom: 18px; }
.empty-state { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 28px; margin-bottom: 14px; }
.empty-state h2 { font-size: 28px; margin-bottom: 8px; }
.empty-state p { margin: 0; color: var(--muted); font-size: 16px; }
.inbox-list { display: grid; gap: 14px; }
.inbox-card, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
.inbox-card.actionable { border-left: 6px solid var(--action); }
.inbox-card.public-worthy { border-left: 6px solid var(--public); }
.inbox-card.priority-watch { border-left: 6px solid var(--watch); }
.card-header { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 10px; }
.card-kicker { margin: 0; color: var(--muted); font-weight: 800; font-size: 12px; }
.seen, .unseen { border: 1px solid var(--line); border-radius: 999px; padding: 4px 8px; font-size: 12px; color: var(--muted); }
.verdict { display: inline-flex; margin: 12px 0; padding: 8px 10px; border-radius: 6px; font-weight: 900; color: #fff; background: var(--action); }
.facts { display: grid; grid-template-columns: 190px 1fr; gap: 8px 14px; margin: 12px 0; }
.facts dt { color: var(--muted); font-weight: 700; }
.facts dd { margin: 0; }
.facts.compact { grid-template-columns: 150px 1fr; }
.field { margin-top: 14px; }
.field p { margin: 0; line-height: 1.45; }
ul { margin: 0; padding-left: 20px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 8px 6px; vertical-align: top; }
.disclosure, .not-buy { color: var(--muted); font-weight: 800; }
.mark-seen { margin-top: 16px; border: 0; border-radius: 6px; background: #141414; color: #fff; padding: 10px 14px; font-weight: 800; cursor: pointer; }
.lower-grid { display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 14px; align-items: start; }
.engine-state { display: inline-block; margin-top: 10px; font-size: 22px; }
@media (max-width: 820px) { .topbar, .lower-grid { display: block; } .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .panel { margin-top: 14px; } .facts, .facts.compact { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) { :root { --bg: #111210; --panel: #191a18; --ink: #f2f1ec; --muted: #a8aaa2; --line: #34362f; } .mark-seen { background: #f2f1ec; color: #111210; } }
"""


def _js() -> str:
    return f"""
const refreshIntervalMs = {REFRESH_INTERVAL_MS};

async function markSeen(inboxId, button) {{
  const response = await fetch(`/inbox/${{encodeURIComponent(inboxId)}}/seen`, {{ method: 'POST' }});
  if (!response.ok) throw new Error('Unable to mark inbox item seen');
  const card = button.closest('[data-inbox-id]');
  button.remove();
  const badge = card && card.querySelector('.unseen');
  if (badge) {{
    badge.className = 'seen';
    badge.textContent = 'Seen';
  }}
}}

document.addEventListener('click', (event) => {{
  const button = event.target.closest('.mark-seen');
  if (!button) return;
  button.disabled = true;
  markSeen(button.dataset.inboxId, button).catch(() => {{
    button.disabled = false;
    button.textContent = 'MARK SEEN';
  }});
}});

async function refreshDashboard() {{
  const response = await fetch('/dashboard', {{ cache: 'no-store' }});
  if (!response.ok) return;
  const text = await response.text();
  const next = new DOMParser().parseFromString(text, 'text/html');
  const currentMain = document.querySelector('main.shell');
  const nextMain = next.querySelector('main.shell');
  if (currentMain && nextMain) currentMain.replaceWith(nextMain);
}}

setInterval(refreshDashboard, refreshIntervalMs);
"""
