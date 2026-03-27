#!/usr/bin/env python3
"""
DWDM Network-Level AI Report Generator
=======================================
Pulls existing per-node AI briefings + raw data from the DB (no re-scraping),
aggregates them, then makes TWO network-level AI calls:

  1. Senior Network Analyst / Program Manager Assessment
     — Prioritised action plan for maintenance teams, manpower deployment, risk.

  2. Executive / ManCom Report
     — Board-level summary: availability, risk posture, recommendations.

Generates a single two-tab HTML report.

Usage:
    python3 DWDMAgent/network_report.py
    python3 DWDMAgent/network_report.py --model gemini-3-flash-preview
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import config
from DWDMAgent import db

# Re-use all the DB loaders and helpers from qwen_compare without duplication
from DWDMAgent.qwen_compare import (
    load_summaries_from_db,
    _parse_json_response,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger('network_report')

REPORT_DIR = os.path.join(os.path.dirname(__file__), 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Network-level prompt construction
# ---------------------------------------------------------------------------

_PM_SYSTEM_PROMPT = """\
You are the Telecoms DWDM Senior Network Analyst and Program Manager \
for the 19-node Metro Manila DWDM ring (Cisco NCS 2006 platform).

Your role: Synthesise per-node AI assessments into a network-wide operational plan \
that drives maintenance team deployment.

Domain knowledge:
- Power Thresholds: -8 to -18 dBm optimal, -25 dBm degraded, -40 dBm dark/cut.
- SQUELCHED ports = total customer blackout. FRNGSYNC = sync loss / fiber cut.
- Failed SNCP = backbone dark AND client SQUELCHED simultaneously.
- Metro Manila ring topology — a failed span isolates two adjacent nodes.

Output: Return ONLY a JSON object with these exact keys:
{
  "network_health_score": <integer 0-100>,
  "overall_status": "HEALTHY" | "DEGRADED" | "CRITICAL",
  "situation_report": "<2-3 sentence plain English network sitrep>",
  "critical_nodes": [{"tid": "<str>", "issue": "<str>"}],
  "at_risk_nodes":  [{"tid": "<str>", "issue": "<str>"}],
  "immediate_actions": [
    {
      "priority": <1-based integer>,
      "node": "<TID or 'ALL'>",
      "action": "<clear actionable task>",
      "reason": "<why this is urgent>",
      "urgency": "IMMEDIATE" | "URGENT" | "SCHEDULED",
      "team": "<e.g. Fiber Ops, NOC, Field Eng>",
      "estimated_hours": <number>
    }
  ],
  "scheduled_maintenance": [
    {
      "node": "<TID>",
      "task": "<maintenance task>",
      "suggested_window": "<e.g. off-peak weekend>",
      "notes": "<str>"
    }
  ],
  "risk_assessment": "<paragraph on overall risk and exposure>",
  "network_recommendations": "<paragraph with strategic recommendations>",
  "manpower_summary": "<brief summary of total manpower needed and team allocation>"
}
Return ONLY the JSON. No markdown, no code fences, no explanation.
"""

_EXEC_SYSTEM_PROMPT = """\
You are preparing a board-level DWDM network executive briefing
for Telecoms leadership. Audience: C-level executives and board members
who need business clarity, not TL1 technical detail.

Language: concise, professional, non-technical. Translate network issues into \
business impact terms (service availability, customer risk, revenue exposure).

Output: Return ONLY a JSON object with these exact keys:
{
  "report_date": "<today's date as YYYY-MM-DD>",
  "headline": "<one sentence: current network posture>",
  "network_availability_pct": "<e.g. 98.5%>",
  "overall_status_label": "<e.g. Stable | At Risk | Under Stress>",
  "network_health_score": <integer 0-100>,
  "nodes_total": <int>,
  "nodes_healthy": <int>,
  "nodes_degraded": <int>,
  "nodes_critical": <int>,
  "business_risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "service_impact_summary": "<1-2 sentences: what customers/services are affected>",
  "key_findings": ["<finding 1>", "<finding 2>", "<finding 3>"],
  "management_recommendations": ["<rec 1>", "<rec 2>", "<rec 3>"],
  "investment_or_resource_needs": "<any budget/resource ask, or none>",
  "outlook_30_days": "<expected network trajectory over next 30 days>",
  "closing_statement": "<closing reassurance or call to action for leadership>"
}
Return ONLY the JSON. No markdown, no code fences, no explanation.
"""


def _build_network_context(summaries: list[dict]) -> str:
    """
    Aggregate all 19 node briefings into a compact context string for
    network-level AI prompts.
    """
    total = len(summaries)
    status_counts = {'HEALTHY': 0, 'DEGRADED': 0, 'CRITICAL': 0, 'UNKNOWN': 0}
    total_alarms_cr = 0
    total_alarms_mj = 0
    total_dark  = 0
    total_squelched = 0
    total_optical = 0

    node_lines = []
    for s in summaries:
        b = s.get('gemini_briefing') or {}
        status    = b.get('status', 'UNKNOWN')
        score     = b.get('health_score', 0)
        status_counts[status if status in status_counts else 'UNKNOWN'] += 1

        cr = sum(1 for a in s['alarms'] if a['severity'] == 'CR')
        mj = sum(1 for a in s['alarms'] if a['severity'] == 'MJ')
        total_alarms_cr += cr
        total_alarms_mj += mj

        dark = len(b.get('dark_spans', []))
        squelched = len(b.get('squelched_ports', []))
        total_dark += dark
        total_squelched += squelched
        total_optical += len(s.get('optical', []))

        priorities = b.get('top_priorities', [])
        ops_action = b.get('ops_action', '')

        node_lines.append(
            f"  - {s['tid']} | Site: {s['site']} | Status: {status} | Score: {score}/100\n"
            f"    Alarms: CR={cr} MJ={mj} | Dark spans: {dark} | Squelched: {squelched}\n"
            f"    Top priorities: {'; '.join(priorities[:3])}\n"
            f"    Ops action: {ops_action[:200]}"
        )

    per_node_section = '\n\n'.join(node_lines)

    return f"""\
NETWORK SUMMARY — Metro Manila DWDM Ring
Nodes total: {total}
  Healthy:  {status_counts['HEALTHY']}
  Degraded: {status_counts['DEGRADED']}
  Critical: {status_counts['CRITICAL']}
  Unknown:  {status_counts['UNKNOWN']}

Network-wide alarms: CR={total_alarms_cr}  MJ={total_alarms_mj}
Dark spans (total across ring): {total_dark}
Squelched ports (total): {total_squelched}
Total optical channels monitored: {total_optical}

PER-NODE BRIEFINGS (from on-device AI assessment):
{per_node_section}

Synthesise the above into the required JSON as your senior network analyst.
"""


# ---------------------------------------------------------------------------
# Gemini API call (generic)
# ---------------------------------------------------------------------------

def _call_gemini(system_prompt: str, user_content: str, label: str, model: str,
                 max_tokens: int = 4000) -> tuple[dict, dict]:
    """Fire a Gemini API call and return (parsed_dict, token_stats)."""
    from google import genai
    from google.genai import types as genai_types

    gemini_cfg = config.get_gemini_config()
    api_key    = gemini_cfg['api_key']

    full = system_prompt + "\n\n" + user_content
    logger.info(f"Calling Gemini [{label}] ({model}) ...")
    t0 = time.time()
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[genai_types.Content(
            role='user',
            parts=[genai_types.Part(text=full)]
        )],
        config=genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            response_mime_type='application/json',
        ),
    )
    elapsed = time.time() - t0
    usage   = response.usage_metadata
    in_tok  = getattr(usage, 'prompt_token_count', 0) or 0
    out_tok = getattr(usage, 'candidates_token_count', 0) or 0
    logger.info(
        f"[{label}] done in {elapsed:.1f}s — in: {in_tok}  out: {out_tok} tokens"
    )
    parsed = _parse_json_response(response.text, label, label)
    return parsed, {'input': in_tok, 'output': out_tok, 'elapsed': elapsed}


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

# Professional muted palette — (text_color, border_color, bg_tint)
_STATUS_BG = {
    'HEALTHY':   ('#2e8b5e', '#2e8b5e', '#0d1f18'),
    'DEGRADED':  ('#b07d28', '#b07d28', '#1e1608'),
    'CRITICAL':  ('#a83232', '#a83232', '#1e0a0a'),
    'UNKNOWN':   ('#5c6478', '#3a3f52', '#181c27'),
    'PARSE_ERR': ('#5c6478', '#3a3f52', '#181c27'),
}

_URGENCY_COLOR = {
    'IMMEDIATE': '#a83232',
    'URGENT':    '#b07d28',
    'SCHEDULED': '#2e8b5e',
}

_URGENCY_BG = {
    'IMMEDIATE': '#1e0a0a',
    'URGENT':    '#1e1608',
    'SCHEDULED': '#0d1f18',
}

_RISK_COLOR = {
    'LOW':      '#2e8b5e',
    'MEDIUM':   '#b07d28',
    'HIGH':     '#a83232',
    'CRITICAL': '#7a1a1a',
}


def _score_ring(score: int, color: str, size: int = 72) -> str:
    """SVG donut ring for health score — thin, minimal."""
    pct  = max(0, min(100, score))
    r    = (size // 2) - 7
    circ = 2 * 3.14159 * r
    dash = circ * pct / 100
    gap  = circ - dash
    cx = cy = size // 2
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#2a2f42" stroke-width="5"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="5" '
        f'stroke-dasharray="{dash:.1f} {gap:.1f}" stroke-linecap="butt" '
        f'transform="rotate(-90 {cx} {cy})"/>'
        f'<text x="{cx}" y="{cy+5}" text-anchor="middle" font-size="13" '
        f'font-weight="600" fill="{color}" font-family="-apple-system,sans-serif">{pct}</text>'
        f'</svg>'
    )


def _node_mini_card(s: dict) -> str:
    """Compact per-node card for the operations tab."""
    b       = s.get('gemini_briefing') or {}
    status  = b.get('status', 'UNKNOWN')
    score   = b.get('health_score', 0)
    fg, brd, bg = _STATUS_BG.get(status, _STATUS_BG['UNKNOWN'])
    cr = sum(1 for a in s['alarms'] if a['severity'] == 'CR')
    mj = sum(1 for a in s['alarms'] if a['severity'] == 'MJ')
    dark_ct    = len(b.get('dark_spans', []))
    squelch_ct = len(b.get('squelched_ports', []))
    priorities = b.get('top_priorities', ['No assessment available'])
    ops_action = b.get('ops_action', '')

    pri_html = ''.join(
        f'<li style="margin-bottom:4px;color:#9aa0b4;">{p}</li>'
        for p in priorities[:3]
    )

    cr_style  = 'color:#a83232;font-weight:600;' if cr  else 'color:#4a5068;'
    mj_style  = 'color:#b07d28;font-weight:600;' if mj  else 'color:#4a5068;'
    drk_style = 'color:#a83232;' if dark_ct    else 'color:#4a5068;'
    sql_style = 'color:#b07d28;' if squelch_ct else 'color:#4a5068;'

    return f"""
<div style="background:#1e2235;border:1px solid #2a2f42;border-left:3px solid {brd};border-radius:6px;padding:14px;display:flex;gap:14px;align-items:flex-start;">
  <div style="flex-shrink:0;">{_score_ring(score, fg, 60)}</div>
  <div style="flex:1;min-width:0;">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:3px;">
      <span style="font-weight:600;color:#dde0ec;font-size:0.88rem;letter-spacing:0.01em;">{s['tid']}</span>
      <span style="font-size:0.68rem;padding:1px 8px;border-radius:3px;background:{bg};color:{fg};letter-spacing:0.05em;white-space:nowrap;text-transform:uppercase;font-weight:600;">{status}</span>
    </div>
    <div style="color:#4a5068;font-size:0.72rem;margin-bottom:8px;">{s['site']}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:4px;margin-bottom:8px;font-size:0.7rem;">
      <span style="{cr_style}">CR Alarm: {cr}</span>
      <span style="{mj_style}">MJ Alarm: {mj}</span>
      <span style="{drk_style}">Dark: {dark_ct}</span>
      <span style="{sql_style}">Squelch: {squelch_ct}</span>
    </div>
    <ul style="font-size:0.74rem;padding-left:14px;margin:0 0 6px;">{pri_html}</ul>
    {'<p style="color:#5c6478;font-size:0.71rem;margin:4px 0 0;line-height:1.5;">' + ops_action[:180] + ('…' if len(ops_action)>180 else '') + '</p>' if ops_action else ''}
  </div>
</div>"""


def _action_table(actions: list[dict]) -> str:
    if not actions:
        return '<p style="color:#4a5068;font-size:0.88rem;">No immediate actions identified.</p>'
    rows = ''
    for i, a in enumerate(actions):
        urg      = a.get('urgency', 'SCHEDULED')
        ug_color = _URGENCY_COLOR.get(urg, '#5c6478')
        ug_bg    = _URGENCY_BG.get(urg, '#181c27')
        row_bg   = '#1a1f30' if i % 2 == 0 else '#1e2235'
        rows += f"""
<tr style="background:{row_bg};">
  <td style="padding:10px 14px;text-align:center;font-weight:700;color:#9aa0b4;font-size:0.82rem;">#{a.get('priority','?')}</td>
  <td style="padding:10px 14px;">
    <span style="display:inline-block;padding:2px 10px;border-radius:3px;font-size:0.7rem;font-weight:700;
      background:{ug_bg};color:{ug_color};letter-spacing:0.05em;text-transform:uppercase;">{urg}</span>
  </td>
  <td style="padding:10px 14px;font-weight:600;color:#c8cce0;font-size:0.85rem;">{a.get('node','')}</td>
  <td style="padding:10px 14px;color:#9aa0b4;font-size:0.85rem;line-height:1.4;">{a.get('action','')}</td>
  <td style="padding:10px 14px;color:#5c6478;font-size:0.82rem;line-height:1.4;">{a.get('reason','')}</td>
  <td style="padding:10px 14px;color:#7a82a0;font-size:0.82rem;">{a.get('team','')}</td>
  <td style="padding:10px 14px;text-align:center;color:#9aa0b4;font-size:0.85rem;">{a.get('estimated_hours','?')}h</td>
</tr>"""

    return f"""
<div style="overflow-x:auto;border-radius:6px;border:1px solid #2a2f42;overflow:hidden;">
<table style="width:100%;border-collapse:collapse;">
  <thead>
    <tr style="background:#141829;border-bottom:2px solid #2a2f42;">
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:center;letter-spacing:.06em;text-transform:uppercase;">#</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Urgency</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Node</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Action Required</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Reason</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Team</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:center;letter-spacing:.06em;text-transform:uppercase;">Est. Hours</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
</div>"""


def _maintenance_table(items: list[dict]) -> str:
    if not items:
        return '<p style="color:#4a5068;font-size:0.88rem;">No scheduled maintenance items identified.</p>'
    rows = ''
    for i, m in enumerate(items):
        row_bg = '#1a1f30' if i % 2 == 0 else '#1e2235'
        rows += f"""
<tr style="background:{row_bg};">
  <td style="padding:10px 14px;font-weight:600;color:#c8cce0;font-size:0.85rem;">{m.get('node','')}</td>
  <td style="padding:10px 14px;color:#9aa0b4;font-size:0.85rem;line-height:1.4;">{m.get('task','')}</td>
  <td style="padding:10px 14px;color:#7a82a0;font-size:0.82rem;">{m.get('suggested_window','')}</td>
  <td style="padding:10px 14px;color:#5c6478;font-size:0.82rem;line-height:1.4;">{m.get('notes','')}</td>
</tr>"""
    return f"""
<div style="overflow-x:auto;border-radius:6px;border:1px solid #2a2f42;overflow:hidden;">
<table style="width:100%;border-collapse:collapse;">
  <thead>
    <tr style="background:#141829;border-bottom:2px solid #2a2f42;">
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Node</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Task</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Suggested Window</th>
      <th style="padding:10px 14px;color:#4a5068;font-size:0.72rem;font-weight:700;text-align:left;letter-spacing:.06em;text-transform:uppercase;">Notes</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
</div>"""


# ---------------------------------------------------------------------------
# HTML report builder
# ---------------------------------------------------------------------------

def build_html_report(
    summaries: list[dict],
    pm_brief: dict,
    exec_brief: dict,
    model: str,
) -> str:
    now_str  = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(REPORT_DIR, f'network_report_{ts}.html')

    # --- aggregate quick stats ---
    total      = len(summaries)
    n_critical = sum(1 for s in summaries if (s.get('gemini_briefing') or {}).get('status') == 'CRITICAL')
    n_degraded = sum(1 for s in summaries if (s.get('gemini_briefing') or {}).get('status') == 'DEGRADED')
    n_healthy  = sum(1 for s in summaries if (s.get('gemini_briefing') or {}).get('status') == 'HEALTHY')
    total_cr   = sum(sum(1 for a in s['alarms'] if a['severity'] == 'CR') for s in summaries)
    total_mj   = sum(sum(1 for a in s['alarms'] if a['severity'] == 'MJ') for s in summaries)

    net_score  = pm_brief.get('network_health_score', 0)
    net_status = pm_brief.get('overall_status', 'UNKNOWN')
    fg, brd, bg_dim = _STATUS_BG.get(net_status, _STATUS_BG['UNKNOWN'])

    # PM tab components
    sitrep     = pm_brief.get('situation_report', '')
    risk_text  = pm_brief.get('risk_assessment', '')
    reco_text  = pm_brief.get('network_recommendations', '')
    manpower   = pm_brief.get('manpower_summary', '')
    actions    = pm_brief.get('immediate_actions', [])
    maintenance = pm_brief.get('scheduled_maintenance', [])

    crit_nodes = pm_brief.get('critical_nodes', [])
    risk_nodes = pm_brief.get('at_risk_nodes', [])

    crit_badges = ''.join(
        f'<span style="display:inline-block;background:#1e0a0a;border:1px solid #a83232;color:#a83232;'
        f'border-radius:3px;padding:3px 10px;font-size:0.75rem;margin:3px;font-weight:600;">'
        f'{n["tid"]} &mdash; <span style="font-weight:400;color:#7a3232;">{n["issue"]}</span></span>'
        for n in crit_nodes
    ) or '<span style="color:#2e8b5e;font-size:0.82rem;">None identified</span>'

    risk_badges = ''.join(
        f'<span style="display:inline-block;background:#1e1608;border:1px solid #b07d28;color:#b07d28;'
        f'border-radius:3px;padding:3px 10px;font-size:0.75rem;margin:3px;font-weight:600;">'
        f'{n["tid"]} &mdash; <span style="font-weight:400;color:#7a5a1a;">{n["issue"]}</span></span>'
        for n in risk_nodes
    ) or '<span style="color:#2e8b5e;font-size:0.82rem;">None identified</span>'

    action_table_html      = _action_table(actions)
    maintenance_table_html = _maintenance_table(maintenance)

    # Node mini cards (sorted: CRITICAL first, then DEGRADED, healthy last)
    def _sort_key(s):
        st = (s.get('gemini_briefing') or {}).get('status', 'UNKNOWN')
        return {'CRITICAL': 0, 'DEGRADED': 1, 'HEALTHY': 2}.get(st, 3)

    sorted_summaries = sorted(summaries, key=_sort_key)
    node_cards_html = '\n'.join(_node_mini_card(s) for s in sorted_summaries)

    # Executive tab
    ex_headline    = exec_brief.get('headline', '')
    ex_avail       = exec_brief.get('network_availability_pct', 'N/A')
    ex_status_lbl  = exec_brief.get('overall_status_label', net_status)
    ex_score       = exec_brief.get('network_health_score', net_score)
    ex_risk        = exec_brief.get('business_risk_level', 'UNKNOWN')
    ex_risk_color  = _RISK_COLOR.get(ex_risk, '#6b7280')
    ex_impact      = exec_brief.get('service_impact_summary', '')
    ex_findings    = exec_brief.get('key_findings', [])
    ex_recos       = exec_brief.get('management_recommendations', [])
    ex_resource    = exec_brief.get('investment_or_resource_needs', '')
    ex_outlook     = exec_brief.get('outlook_30_days', '')
    ex_closing     = exec_brief.get('closing_statement', '')
    ex_healthy     = exec_brief.get('nodes_healthy', n_healthy)
    ex_degraded    = exec_brief.get('nodes_degraded', n_degraded)
    ex_critical    = exec_brief.get('nodes_critical', n_critical)

    ex_findings_html = ''.join(
        f'<li style="padding:8px 0 8px 12px;border-left:2px solid #e5e7eb;margin-bottom:6px;color:#374151;">'
        f'{f}</li>'
        for f in ex_findings
    )
    ex_recos_html    = ''.join(
        f'<li style="padding:8px 0;color:#374151;">'
        f'<span style="color:#2e8b5e;font-weight:700;margin-right:8px;">&#10003;</span>{r}</li>'
        for r in ex_recos
    )

    doc_title = exec_brief.get('report_title', 'DWDM Network Status Report')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{doc_title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body   {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #141829; color: #c8cce0; }}
  .page  {{ display: none; padding: 28px 32px; max-width: 1680px; margin: 0 auto; }}
  .page.active {{ display: block; }}

  /* Tab bar */
  .tab-bar {{
    display: flex; background: #0e1120;
    border-bottom: 1px solid #1e2338;
    position: sticky; top: 0; z-index: 100;
    padding: 0 32px;
  }}
  .tab-btn {{
    padding: 13px 26px; font-size: 0.82rem; font-weight: 600;
    cursor: pointer; border: none; background: transparent;
    color: #4a5068; border-bottom: 2px solid transparent; margin-bottom: -1px;
    letter-spacing: 0.03em; text-transform: uppercase;
    transition: color .15s, border-color .15s;
  }}
  .tab-btn.active {{ color: #c8cce0; border-bottom-color: #4f6af0; }}
  .tab-btn:hover  {{ color: #9aa0b4; }}

  /* Section card */
  .section {{
    background: #1a1f30; border: 1px solid #22273a; border-radius: 6px;
    padding: 20px 24px; margin-bottom: 20px;
  }}
  .section-title {{
    font-size: 0.7rem; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: #4a5068; margin-bottom: 16px;
    padding-bottom: 10px; border-bottom: 1px solid #1e2338;
  }}

  /* KPI tiles */
  .kpi-grid {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .kpi-tile {{
    background: #1a1f30; border: 1px solid #22273a; border-radius: 6px;
    padding: 16px 20px; flex: 1 1 130px; min-width: 120px;
  }}
  .kpi-label {{ font-size: 0.68rem; color: #4a5068; text-transform: uppercase;
                letter-spacing: .08em; margin-bottom: 8px; }}
  .kpi-value {{ font-size: 1.7rem; font-weight: 700; color: #dde0ec; line-height: 1; }}
  .kpi-sub   {{ font-size: 0.72rem; color: #3a3f52; margin-top: 4px; }}

  /* Node grid */
  .node-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
                gap: 10px; }}

  /* Executive page — intentionally light for print/presentation */
  #tab-exec {{ background: #f4f5f7; color: #1a1e2b; }}
  .exec-body {{ max-width: 900px; margin: 0 auto; }}
  .exec-headline {{
    font-size: 1.2rem; font-weight: 600; color: #1a1e2b;
    line-height: 1.5; margin-bottom: 14px;
  }}
  .exec-kpi-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }}
  .exec-kpi {{
    flex: 1 1 120px; background: #ffffff; border: 1px solid #dde0e8;
    border-radius: 6px; padding: 16px 18px; text-align: center;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }}
  .exec-kpi .val {{ font-size: 1.8rem; font-weight: 700; line-height: 1; }}
  .exec-kpi .lbl {{ font-size: 0.68rem; color: #6b7280; text-transform: uppercase;
                    letter-spacing: .07em; margin-top: 6px; }}
  .exec-section {{
    background: #ffffff; border: 1px solid #dde0e8; border-radius: 6px;
    padding: 20px 24px; margin-bottom: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,.05);
  }}
  .exec-section-title {{
    font-size: 0.68rem; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: #6b7280; margin-bottom: 14px;
    padding-bottom: 8px; border-bottom: 1px solid #e5e7eb;
  }}
  .exec-section p {{ color: #374151; line-height: 1.7; font-size: 0.93rem; }}
  .exec-section ul {{ list-style: none; padding: 0; }}
  .exec-section ul li {{ color: #374151; line-height: 1.6; font-size: 0.92rem;
                         padding: 4px 0; border-bottom: 1px solid #f0f1f3; }}
  .exec-section ul li:last-child {{ border-bottom: none; }}
  .logo-bar {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 32px; border-bottom: 1px solid #1e2338;
    background: #0e1120;
  }}
  @media print {{
    .tab-bar {{ display: none; }}
    .page {{ display: block !important; padding: 20px; }}
    #tab-exec {{ page-break-before: always; }}
  }}
</style>
</head>
<body>

<!-- Top logo / meta bar -->
<div class="logo-bar">
  <div>
    <span style="font-weight:700;font-size:0.88rem;color:#9aa0b4;letter-spacing:0.04em;text-transform:uppercase;">Telecoms</span>
    <span style="color:#22273a;margin:0 10px;">|</span>
    <span style="color:#3a3f52;font-size:0.82rem;">DWDM AI Expert System</span>
  </div>
  <div style="font-size:0.75rem;color:#3a3f52;">{now_str} &nbsp;&nbsp; {model}</div>
</div>

<!-- Tab bar -->
<div class="tab-bar">
  <button class="tab-btn active" onclick="showTab('ops')">Network Operations</button>
  <button class="tab-btn" onclick="showTab('exec')">Executive Report</button>
</div>

<!-- ===================================================================
     TAB 1 — NETWORK OPERATIONS / PM ASSESSMENT
     =================================================================== -->
<div id="tab-ops" class="page active">

  <div style="display:flex;align-items:center;gap:22px;margin-bottom:24px;border-bottom:1px solid #1e2338;padding-bottom:20px;flex-wrap:wrap;">
    {_score_ring(net_score, fg, 80)}
    <div>
      <div style="font-size:0.68rem;color:#4a5068;letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px;">Senior Network Analyst — Program Manager Assessment</div>
      <div style="font-size:1.25rem;font-weight:600;color:#dde0ec;">
        Metro Manila DWDM Ring
      </div>
      <div style="color:#4a5068;font-size:0.8rem;margin-top:4px;">
        {total} nodes assessed &nbsp;&nbsp;{now_str}
      </div>
      <div style="margin-top:10px;">
        <span style="padding:3px 14px;border-radius:3px;font-size:0.72rem;font-weight:700;
          background:{bg_dim};color:{fg};letter-spacing:.07em;text-transform:uppercase;">{net_status}</span>
      </div>
    </div>
  </div>

  <!-- KPI strip -->
  <div class="kpi-grid">
    <div class="kpi-tile">
      <div class="kpi-label">Network Score</div>
      <div class="kpi-value" style="color:{fg};">{net_score}</div>
      <div class="kpi-sub">/ 100</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">Nodes Healthy</div>
      <div class="kpi-value" style="color:#2e8b5e;">{n_healthy}</div>
      <div class="kpi-sub">of {total}</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">Nodes Degraded</div>
      <div class="kpi-value" style="color:#b07d28;">{n_degraded}</div>
      <div class="kpi-sub">of {total}</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">Nodes Critical</div>
      <div class="kpi-value" style="color:#a83232;">{n_critical}</div>
      <div class="kpi-sub">of {total}</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">CR Alarms</div>
      <div class="kpi-value" style="color:#a83232;">{total_cr}</div>
      <div class="kpi-sub">ring-wide</div>
    </div>
    <div class="kpi-tile">
      <div class="kpi-label">MJ Alarms</div>
      <div class="kpi-value" style="color:#b07d28;">{total_mj}</div>
      <div class="kpi-sub">ring-wide</div>
    </div>
  </div>

  <!-- Situation Report -->
  <div class="section">
    <div class="section-title">Situation Report</div>
    <p style="color:#9aa0b4;line-height:1.75;font-size:0.92rem;">{sitrep}</p>
  </div>

  <!-- Critical & At-Risk nodes -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;">
    <div class="section" style="margin-bottom:0;border-left:3px solid #a83232;">
      <div class="section-title">Critical Nodes</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;">{crit_badges}</div>
    </div>
    <div class="section" style="margin-bottom:0;border-left:3px solid #b07d28;">
      <div class="section-title">At-Risk Nodes</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;">{risk_badges}</div>
    </div>
  </div>

  <!-- Immediate Actions Table -->
  <div class="section">
    <div class="section-title">Immediate Action Plan — Prioritised for Maintenance Team Deployment</div>
    {action_table_html}
  </div>

  <!-- Manpower Summary -->
  <div class="section">
    <div class="section-title">Manpower Deployment Summary</div>
    <p style="color:#9aa0b4;line-height:1.75;font-size:0.92rem;">{manpower}</p>
  </div>

  <!-- Scheduled Maintenance -->
  <div class="section">
    <div class="section-title">Scheduled Maintenance Plan</div>
    {maintenance_table_html}
  </div>

  <!-- Risk Assessment -->
  <div class="section">
    <div class="section-title">Risk Assessment</div>
    <p style="color:#9aa0b4;line-height:1.75;font-size:0.92rem;">{risk_text}</p>
  </div>

  <!-- Strategic Recommendations -->
  <div class="section">
    <div class="section-title">Network Recommendations</div>
    <p style="color:#9aa0b4;line-height:1.75;font-size:0.92rem;">{reco_text}</p>
  </div>

  <!-- Per-node cards -->
  <div class="section">
    <div class="section-title">Per-Node Status — All {total} Nodes (sorted by severity)</div>
    <div class="node-grid">{node_cards_html}</div>
  </div>

</div><!-- /tab-ops -->


<!-- ===================================================================
     TAB 2 — EXECUTIVE / MANCOM REPORT
     =================================================================== -->
<div id="tab-exec" class="page">
<div class="exec-body">

  <!-- Header -->
  <div style="padding:32px 0 24px;border-bottom:2px solid #e2e4ea;margin-bottom:28px;">
    <div style="font-size:0.68rem;color:#9ca3af;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px;">
      Management Committee Report &nbsp;&middot;&nbsp; Confidential
    </div>
    <div style="font-size:1.6rem;font-weight:700;color:#111827;margin-bottom:6px;line-height:1.2;">
      {doc_title}
    </div>
    <div style="color:#6b7280;font-size:0.82rem;">
      Telecoms &nbsp;&middot;&nbsp; Metro Manila DWDM Ring &nbsp;&middot;&nbsp; {now_str}
    </div>
    <div style="margin-top:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      <span style="padding:4px 14px;border-radius:3px;font-size:0.72rem;font-weight:700;
        background:{ex_risk_color}18;color:{ex_risk_color};
        border:1px solid {ex_risk_color}44;letter-spacing:.06em;text-transform:uppercase;">
        Business Risk: {ex_risk}
      </span>
      <span style="padding:4px 14px;border-radius:3px;font-size:0.72rem;font-weight:600;
        background:#f3f4f6;color:#374151;border:1px solid #d1d5db;">
        {ex_status_lbl}
      </span>
    </div>
  </div>

  <!-- Headline -->
  <div class="exec-section" style="border-left:3px solid {fg};">
    <p class="exec-headline">{ex_headline}</p>
    <p style="color:#4b5563;line-height:1.7;font-size:0.92rem;">{ex_impact}</p>
  </div>

  <!-- KPI row -->
  <div class="exec-kpi-row">
    <div class="exec-kpi">
      <div class="val" style="color:#111827;">{ex_avail}</div>
      <div class="lbl">Network Availability</div>
    </div>
    <div class="exec-kpi">
      <div class="val" style="color:{fg};">{ex_score}<span style="font-size:1rem;color:#9ca3af;">/100</span></div>
      <div class="lbl">Health Score</div>
    </div>
    <div class="exec-kpi">
      <div class="val" style="color:#2e8b5e;">{ex_healthy}</div>
      <div class="lbl">Nodes Healthy</div>
    </div>
    <div class="exec-kpi">
      <div class="val" style="color:#b07d28;">{ex_degraded}</div>
      <div class="lbl">Nodes Degraded</div>
    </div>
    <div class="exec-kpi">
      <div class="val" style="color:#a83232;">{ex_critical}</div>
      <div class="lbl">Nodes Critical</div>
    </div>
    <div class="exec-kpi">
      <div class="val" style="color:{ex_risk_color};font-size:1.3rem;">{ex_risk}</div>
      <div class="lbl">Business Risk</div>
    </div>
  </div>

  <!-- Key Findings -->
  <div class="exec-section">
    <div class="exec-section-title">Key Findings</div>
    <ul>{ex_findings_html}</ul>
  </div>

  <!-- Management Recommendations -->
  <div class="exec-section">
    <div class="exec-section-title">Management Recommendations</div>
    <ul>{ex_recos_html}</ul>
  </div>

  <!-- Resource / Investment needs -->
  {'<div class="exec-section"><div class="exec-section-title">Resource &amp; Investment Considerations</div><p>' + ex_resource + '</p></div>' if ex_resource and ex_resource.strip().lower() not in ('none','n/a','') else ''}

  <!-- 30-day Outlook -->
  <div class="exec-section">
    <div class="exec-section-title">30-Day Network Outlook</div>
    <p>{ex_outlook}</p>
  </div>

  <!-- Closing -->
  <div class="exec-section" style="border-left:3px solid #d1d5db;background:#f9fafb;">
    <p style="font-style:italic;color:#6b7280;font-size:0.9rem;line-height:1.7;">{ex_closing}</p>
  </div>

  <!-- Footer -->
  <div style="text-align:right;padding:20px 0;color:#9ca3af;font-size:0.72rem;border-top:1px solid #e5e7eb;margin-top:8px;">
    Prepared by Telecoms DWDM AI Expert System &nbsp;&middot;&nbsp;
    {model} &nbsp;&middot;&nbsp; {now_str}
  </div>

</div>
</div><!-- /tab-exec -->

<script>
function showTab(name) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(html)
    return out_path


# ---------------------------------------------------------------------------
# DB persistence for network-level reports
# ---------------------------------------------------------------------------

def _save_report(conn, model: str, pm_brief: dict, exec_brief: dict,
                 node_count: int, html_path: str) -> int:
    """Insert a network report row and return its id."""
    score  = pm_brief.get('network_health_score') or exec_brief.get('network_health_score') or 0
    status = pm_brief.get('overall_status', 'UNKNOWN')
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dwdm_network_reports
                (model, pm_briefing_json, exec_briefing_json,
                 node_count, network_health_score, overall_status, report_html_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                model,
                json.dumps(pm_brief),
                json.dumps(exec_brief),
                node_count,
                score,
                status,
                html_path,
            )
        )
        row_id = cur.fetchone()[0]
    return row_id


def _list_reports(conn):
    """Print a table of all saved network reports."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_at AT TIME ZONE 'Asia/Manila' AS run_ph,
                   model, node_count, network_health_score, overall_status,
                   report_html_path
            FROM dwdm_network_reports
            ORDER BY run_at DESC
            LIMIT 50
            """
        )
        rows = cur.fetchall()
    if not rows:
        print('No saved network reports found.')
        return
    print(f'\n{"ID":>5}  {"Run (PHT)":>22}  {"Model":>24}  {"Nodes":>5}  {"Score":>5}  {"Status":>10}  Report')
    print('-' * 110)
    for r in rows:
        rid, run_ph, mdl, nc, score, status, path = r
        ts_str = run_ph.strftime('%Y-%m-%d %H:%M %Z') if run_ph else '?'
        fname  = os.path.basename(path) if path else '—'
        print(f'{rid:>5}  {ts_str:>22}  {mdl:>24}  {nc:>5}  {score:>5}  {status:>10}  {fname}')
    print()


def _replay_report(conn, report_id: int, summaries: list[dict]) -> str:
    """Re-generate HTML from a saved DB report without calling Gemini."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model, pm_briefing_json, exec_briefing_json FROM dwdm_network_reports WHERE id=%s",
            (report_id,)
        )
        row = cur.fetchone()
    if not row:
        logger.error(f'Report id={report_id} not found in DB.')
        sys.exit(1)
    model, pm_json, exec_json = row
    pm_brief   = pm_json   if isinstance(pm_json,   dict) else json.loads(pm_json   or '{}')
    exec_brief = exec_json if isinstance(exec_json, dict) else json.loads(exec_json or '{}')
    logger.info(f'Replaying report id={report_id} (model={model}) ...')
    return build_html_report(summaries, pm_brief, exec_brief, model)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='DWDM Network-level AI Report')
    parser.add_argument('--model', default=None,
                        help='Gemini model (default: from .env)')
    parser.add_argument('--list', action='store_true',
                        help='List all saved network reports and exit')
    parser.add_argument('--replay', type=int, metavar='ID',
                        help='Regenerate HTML from a saved report by DB id (no AI call)')
    args = parser.parse_args()

    gemini_cfg = config.get_gemini_config()
    model = args.model or gemini_cfg['model']

    # --- Always need a DB connection ---
    conn = db.get_connection()
    conn.autocommit = True

    if args.list:
        _list_reports(conn)
        conn.close()
        return

    # --- Load node summaries (needed for both live and replay) ---
    logger.info('=' * 60)
    logger.info(f'DWDM Network Report — model: {model}')
    logger.info('=' * 60)

    try:
        summaries = load_summaries_from_db(conn, tids=None)
    finally:
        pass  # keep conn open until done

    if not summaries:
        logger.error('No devices found in DB. Run dwdm_agent.py first.')
        conn.close()
        sys.exit(1)

    logger.info(f'Loaded {len(summaries)} node(s) from DB')

    # --- Replay mode: skip AI, rebuild HTML from stored JSON ---
    if args.replay:
        out_path = _replay_report(conn, args.replay, summaries)
        conn.close()
        logger.info(f'Replayed report written: {out_path}')
        print(f'\nReport: file://{out_path}')
        return

    # --- Live mode: call AI ---
    no_briefing = [s['tid'] for s in summaries if not s.get('gemini_briefing')]
    if no_briefing:
        logger.warning(
            f'{len(no_briefing)} node(s) have no stored AI briefing: {no_briefing}'
        )

    logger.info('Building network-level context ...')
    network_ctx = _build_network_context(summaries)

    # Call 1: Senior PM Assessment
    logger.info('-' * 60)
    pm_brief, pm_tokens = _call_gemini(
        _PM_SYSTEM_PROMPT, network_ctx,
        label='PM Assessment', model=model, max_tokens=4000
    )
    logger.info(
        f"PM Assessment tokens — in: {pm_tokens['input']}  "
        f"out: {pm_tokens['output']}  elapsed: {pm_tokens['elapsed']:.1f}s"
    )

    # Call 2: Executive Report
    logger.info('-' * 60)
    exec_brief, exec_tokens = _call_gemini(
        _EXEC_SYSTEM_PROMPT, network_ctx,
        label='Exec Report', model=model, max_tokens=2000
    )
    logger.info(
        f"Exec Report tokens — in: {exec_tokens['input']}  "
        f"out: {exec_tokens['output']}  elapsed: {exec_tokens['elapsed']:.1f}s"
    )

    # Build HTML
    logger.info('-' * 60)
    logger.info('Building HTML report ...')
    out_path = build_html_report(summaries, pm_brief, exec_brief, model)
    logger.info(f'Report written: {out_path}')

    # Save to DB
    try:
        report_id = _save_report(
            conn, model, pm_brief, exec_brief, len(summaries), out_path
        )
        logger.info(f'Report saved to DB — id={report_id}')
    except Exception as e:
        logger.error(f'Failed to save report to DB: {e}')
    finally:
        conn.close()

    print(f'\nReport: file://{out_path}')


def build_report(summaries: list[dict], report_dir: str = None) -> str:
    """
    Phase 3 entry point called by the orchestrator (dwdm_agent.py).

    Accepts in-memory summaries from the orchestrator (which use the 'briefing'
    key) and runs the two network-level Gemini calls before writing the HTML report.

    Returns the absolute path of the written report file.
    """
    # Orchestrator stores per-node AI results under 'briefing'; network_report
    # internally uses 'gemini_briefing' — normalise without mutating the originals.
    adapted = []
    for s in summaries:
        if 'briefing' in s and 'gemini_briefing' not in s:
            s = dict(s, gemini_briefing=s['briefing'])
        adapted.append(s)

    gemini_cfg = config.get_gemini_config()
    model = gemini_cfg['model']

    network_ctx = _build_network_context(adapted)

    pm_brief, _ = _call_gemini(
        _PM_SYSTEM_PROMPT, network_ctx,
        label='PM Assessment', model=model, max_tokens=4000
    )
    exec_brief, _ = _call_gemini(
        _EXEC_SYSTEM_PROMPT, network_ctx,
        label='Exec Report', model=model, max_tokens=2000
    )

    out_path = build_html_report(adapted, pm_brief, exec_brief, model)

    conn = db.get_connection()
    conn.autocommit = True
    try:
        _save_report(conn, model, pm_brief, exec_brief, len(adapted), out_path)
    except Exception as e:
        logger.error(f'Failed to save report to DB: {e}')
    finally:
        conn.close()

    return out_path


if __name__ == '__main__':
    main()
