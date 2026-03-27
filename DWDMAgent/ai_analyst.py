"""
AI Analyst — Gemini integration for the DWDM Agent.

Sends per-node scrape summaries to Gemini and returns:
  - briefing_json : structured dict with findings
  - briefing_html : inline HTML fragment for embedding in the report

Uses the same google-genai SDK pattern as the rest of the AIExperts platform.
"""

import json
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — per PRD Section 5
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are the Telecoms DWDM AI Specialist. You analyze raw TL1 output \
and structured telemetry from Cisco NCS 2006 nodes operating on the 19-node \
Metro Manila DWDM ring.

Knowledge Base:
- Power Thresholds: -8 to -18 dBm (Optimal), -25 dBm (Degraded), -40 dBm (Dark/Cut).
- States: 'SQUELCHED' = Total Customer Blackout. 'FRNGSYNC' = Sync Lost due to fiber cut.
- Protection: Failed SNCP Wrap = backbone Dark (≤ -40 dBm) AND client port SQUELCHED.
- Span Localization: Correlate Side D Rx power of Node A with Side A Rx power of Node B.

Output Requirements:
Return a JSON object with the following exact keys:
{
  "health_score": <integer 0-100>,
  "status": "HEALTHY" | "DEGRADED" | "CRITICAL",
  "top_priorities": [<string>, <string>, <string>],
  "alarm_summary": "<brief text>",
  "optical_summary": "<brief text>",
  "failed_sncp_wraps": [<aid string>, ...],
  "dark_spans": [<aid string>, ...],
  "squelched_ports": [<aid string>, ...],
  "ops_action": "<one paragraph senior ops recommendation>"
}
Return ONLY the JSON object. No markdown, no explanation, no code fences.
"""


def _build_prompt(summary: dict) -> str:
    """
    Build the Gemini prompt from a node scrape summary dict.
    Keeps token usage lean by sending counts + worst items only.
    """
    tid  = summary['tid']
    site = summary['site']

    # Alarms — send all CR/MJ, cap others at 20
    alarms = summary.get('alarms', [])
    critical_alarms = [a for a in alarms if a['severity'] in ('CR', 'MJ')]
    alarm_text = json.dumps(critical_alarms[:50], indent=2)

    # Conditions — flag SQUELCHED ones
    conditions = summary.get('conditions', [])
    squelched   = [c for c in conditions if 'SQUELCH' in (c.get('condition_type') or '').upper()]
    cond_text   = json.dumps(squelched[:30], indent=2)

    # Optical — flag dark (≤ -40) and degraded (≤ -25)
    optical = summary.get('optical', [])
    dark_channels      = [o for o in optical if o.get('opwr_dbm') is not None and o['opwr_dbm'] <= -40.0]
    degraded_channels  = [o for o in optical if o.get('opwr_dbm') is not None and -40.0 < o['opwr_dbm'] <= -25.0]
    optical_text = json.dumps({
        'total_channels': len(optical),
        'dark_count':     len(dark_channels),
        'degraded_count': len(degraded_channels),
        'dark_channels':  dark_channels[:20],
        'degraded_channels': degraded_channels[:20],
    }, indent=2)

    # Inventory summary
    inventory = summary.get('inventory', [])
    inv_text = f"{len(inventory)} cards inventoried"

    errors = summary.get('errors', [])

    return f"""\
Node: {tid}
Site: {site}

=== ALARMS (CR/MJ only, {len(critical_alarms)} of {len(alarms)} total) ===
{alarm_text}

=== SQUELCHED CONDITIONS ({len(squelched)} of {len(conditions)} total) ===
{cond_text}

=== OPTICAL POWER ===
{optical_text}

=== INVENTORY ===
{inv_text}

=== SCRAPE ERRORS ===
{json.dumps(errors)}

Analyze the above data and return the JSON briefing as instructed.
"""


def analyse_node(summary: dict) -> tuple[dict, str]:
    """
    Call Gemini to analyse one node scrape summary.

    Returns:
        (briefing_json: dict, briefing_html: str)

    Raises:
        RuntimeError if Gemini call fails.
    """
    gemini_cfg = config.get_gemini_config()
    api_key    = gemini_cfg['api_key']
    model_name = gemini_cfg['model']
    max_tokens = gemini_cfg['max_output']
    tid        = summary['tid']

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        raise RuntimeError(f"google-genai SDK not installed: {e}")

    prompt = _build_prompt(summary)

    logger.info(f"[{tid}] Calling Gemini ({model_name}) ...")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=[
            genai_types.Content(
                role='user',
                parts=[genai_types.Part(text=_SYSTEM_PROMPT + "\n\n" + prompt)]
            )
        ],
        config=genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
        ),
    )

    raw = response.text.strip()
    usage = response.usage_metadata
    in_tok  = getattr(usage, 'prompt_token_count', '?')
    out_tok = getattr(usage, 'candidates_token_count', '?')
    tot_tok = getattr(usage, 'total_token_count', '?')
    logger.info(
        f"[{tid}] Gemini tokens — input: {in_tok}, output: {out_tok}, total: {tot_tok}"
    )

    # Parse JSON response
    try:
        briefing_json = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from a response that slipped in extra text
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            briefing_json = json.loads(m.group(0))
        else:
            logger.error(f"[{tid}] Gemini returned non-JSON: {raw[:300]}")
            briefing_json = {
                "health_score": 0,
                "status": "UNKNOWN",
                "top_priorities": ["AI parse error — review raw response"],
                "alarm_summary": "Parse error",
                "optical_summary": "Parse error",
                "failed_sncp_wraps": [],
                "dark_spans": [],
                "squelched_ports": [],
                "ops_action": raw[:500],
            }

    briefing_html = _render_node_card(summary['tid'], summary['site'], briefing_json)
    return briefing_json, briefing_html


# ---------------------------------------------------------------------------
# Minimal per-node HTML card (embedded in the full report by report_builder)
# ---------------------------------------------------------------------------

def _status_color(status: str) -> str:
    return {'HEALTHY': '#22c55e', 'DEGRADED': '#f59e0b', 'CRITICAL': '#ef4444'}.get(status, '#6b7280')


def _render_node_card(tid: str, site: str, b: dict) -> str:
    status  = b.get('status', 'UNKNOWN')
    score   = b.get('health_score', 0)
    color   = _status_color(status)
    prios   = b.get('top_priorities', [])
    action  = b.get('ops_action', '')
    alm_sum = b.get('alarm_summary', '')
    opt_sum = b.get('optical_summary', '')

    prio_items = ''.join(
        f'<li class="text-sm text-gray-300 py-1 border-b border-gray-700">{p}</li>'
        for p in prios
    )

    dark_aids = ', '.join(b.get('dark_spans', [])) or 'None'
    squelched = ', '.join(b.get('squelched_ports', [])) or 'None'
    wraps     = ', '.join(b.get('failed_sncp_wraps', [])) or 'None'

    return f"""
<div class="node-card bg-gray-800 rounded-xl p-4 border border-gray-700 shadow-lg">
  <div class="flex items-center justify-between mb-3">
    <div>
      <h3 class="text-white font-bold text-lg">{tid}</h3>
      <p class="text-gray-400 text-xs">{site}</p>
    </div>
    <div class="flex flex-col items-center">
      <span class="text-3xl font-black" style="color:{color}">{score}</span>
      <span class="text-xs font-semibold px-2 py-0.5 rounded-full mt-1"
            style="background:{color}22; color:{color}">{status}</span>
    </div>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Alarms</p>
    <p class="text-sm text-gray-200">{alm_sum}</p>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Optical</p>
    <p class="text-sm text-gray-200">{opt_sum}</p>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Top Priorities</p>
    <ul class="list-none m-0 p-0">{prio_items}</ul>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Dark Spans</p>
    <p class="text-xs text-red-400 font-mono">{dark_aids}</p>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Squelched Ports</p>
    <p class="text-xs text-orange-400 font-mono">{squelched}</p>
  </div>
  <div class="mb-3">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Failed SNCP Wraps</p>
    <p class="text-xs text-yellow-400 font-mono">{wraps}</p>
  </div>
  <div class="mt-3 pt-3 border-t border-gray-700">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Ops Action</p>
    <p class="text-sm text-cyan-300 italic">{action}</p>
  </div>
</div>
"""
