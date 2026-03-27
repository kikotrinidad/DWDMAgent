"""
Report Builder — generates the Single-Page Interactive HTML Infographic.

Per PRD Section 6:
  1. Network Health Pulse  — circular gauge (overall ring health 0–100%)
  2. Live Topology Map     — SVG 19-node ring, failed spans blink red
  3. Ops Action Plan       — top 3 priorities across all nodes
  4. Audit Confirmation    — 19/19 node scrape badge
  5. Per-node cards        — embedded from ai_analyst._render_node_card()

Output: /opt/AIExperts/DWDMAgent/reports/dwdm_report_YYYYMMDD_HHMMSS.html
"""

import os
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 19-node ring layout — (x, y) positions on a 900×500 SVG canvas
# Ordered clockwise around the Metro Manila ring.
# ---------------------------------------------------------------------------
RING_NODES = [
    "VM2-DC-NCS2006-DWDM",
    "ZEN-TOWERS-NCS2006-DWDM",
    "MAKATI-CBD-NCS2006",
    "BGC-BONIFACIO-NCS2006",
    "ORTIGAS-CENTER-NCS2006",
    "QUEZON-CITY-NCS2006",
    "CALOOCAN-NCS2006",
    "MALABON-NCS2006",
    "NAVOTAS-NCS2006",
    "VALENZUELA-NCS2006",
    "MARIKINA-NCS2006",
    "PASIG-NCS2006",
    "MANDALUYONG-NCS2006",
    "SAN-JUAN-NCS2006",
    "TAGUIG-NCS2006",
    "PARANAQUE-NCS2006",
    "PASAY-NCS2006",
    "LAS-PINAS-NCS2006",
    "MUNTINLUPA-NCS2006",
]

import math

def _ring_positions(n=19, cx=450, cy=240, rx=380, ry=200):
    """Generate (x,y) positions evenly spaced around an ellipse."""
    positions = []
    for i in range(n):
        angle = (2 * math.pi * i / n) - math.pi / 2
        x = cx + rx * math.cos(angle)
        y = cy + ry * math.sin(angle)
        positions.append((round(x), round(y)))
    return positions


def _build_svg(summaries: list[dict]) -> str:
    """
    Build an SVG ring diagram.
    Nodes with CRITICAL status pulse red; DEGRADED pulse amber; HEALTHY are green.
    """
    positions = _ring_positions()
    status_map = {s['tid']: s.get('briefing', {}).get('status', 'UNKNOWN')
                  for s in summaries}

    color_map = {
        'HEALTHY':  '#22c55e',
        'DEGRADED': '#f59e0b',
        'CRITICAL': '#ef4444',
        'UNKNOWN':  '#6b7280',
    }

    # Build node labels from TIDs (trim to short name)
    def short(tid):
        parts = tid.split('-')
        return parts[0] if parts else tid

    lines = []
    circles = []
    labels = []

    n = len(positions)
    for i, (x, y) in enumerate(positions):
        tid = RING_NODES[i] if i < len(RING_NODES) else f"NODE-{i+1}"
        status = status_map.get(tid, 'UNKNOWN')
        color  = color_map[status]

        # Draw line to next node
        nx, ny = positions[(i + 1) % n]
        # Color the span red if either endpoint is CRITICAL
        next_tid    = RING_NODES[(i + 1) % n] if (i + 1) % n < len(RING_NODES) else ''
        next_status = status_map.get(next_tid, 'UNKNOWN')
        span_color  = '#ef4444' if 'CRITICAL' in (status, next_status) else '#334155'
        span_class  = 'span-critical' if 'CRITICAL' in (status, next_status) else 'span-normal'
        lines.append(
            f'<line x1="{x}" y1="{y}" x2="{nx}" y2="{ny}" '
            f'stroke="{span_color}" stroke-width="2" class="{span_class}" opacity="0.7"/>'
        )

        # Node circle
        pulse_class = f'pulse-{status.lower()}' if status in ('CRITICAL', 'DEGRADED') else ''
        circles.append(
            f'<circle cx="{x}" cy="{y}" r="14" fill="{color}" '
            f'stroke="#1e293b" stroke-width="2" class="{pulse_class}" opacity="0.9"/>'
        )

        # Short label
        lx = x + (16 if x > 450 else -16)
        anchor = 'start' if x > 450 else 'end'
        ly = y - 2
        labels.append(
            f'<text x="{lx}" y="{ly}" fill="#cbd5e1" font-size="9" '
            f'text-anchor="{anchor}" font-family="monospace">{short(tid)}</text>'
        )

    svg = f"""
<svg viewBox="0 0 900 480" xmlns="http://www.w3.org/2000/svg"
     class="w-full h-full" style="background:#0f172a; border-radius:12px;">
  <defs>
    <style>
      @keyframes pulse-red   {{ 0%,100%{{opacity:.9}} 50%{{opacity:.3}} }}
      @keyframes pulse-amber {{ 0%,100%{{opacity:.9}} 50%{{opacity:.5}} }}
      .pulse-critical  {{ animation: pulse-red   1.2s ease-in-out infinite; }}
      .pulse-degraded  {{ animation: pulse-amber 2s   ease-in-out infinite; }}
      .span-critical   {{ animation: pulse-red   1.2s ease-in-out infinite; }}
    </style>
  </defs>
  {''.join(lines)}
  {''.join(circles)}
  {''.join(labels)}
  <text x="450" y="468" fill="#475569" font-size="10" text-anchor="middle"
        font-family="monospace">Metro Manila DWDM Ring — 19 Nodes</text>
</svg>"""
    return svg


def _gauge_svg(score: int) -> str:
    """Render a semicircular gauge for the overall ring health score."""
    pct   = max(0, min(100, score))
    color = '#22c55e' if pct >= 80 else ('#f59e0b' if pct >= 50 else '#ef4444')
    # Arc: 180° semicircle, r=80, stroke-dasharray trick
    r         = 80
    circ      = math.pi * r     # half circumference for 180° arc
    dash_fill = (pct / 100) * circ
    dash_gap  = circ - dash_fill
    return f"""
<svg viewBox="0 0 200 120" xmlns="http://www.w3.org/2000/svg" class="w-48 h-28 mx-auto">
  <path d="M 20 100 A 80 80 0 0 1 180 100"
        fill="none" stroke="#1e293b" stroke-width="18" stroke-linecap="round"/>
  <path d="M 20 100 A 80 80 0 0 1 180 100"
        fill="none" stroke="{color}" stroke-width="18" stroke-linecap="round"
        stroke-dasharray="{dash_fill:.1f} {dash_gap + 10:.1f}"
        style="transition: stroke-dasharray 1s ease;"/>
  <text x="100" y="95" text-anchor="middle" fill="{color}"
        font-size="32" font-weight="900" font-family="monospace">{pct}</text>
  <text x="100" y="113" text-anchor="middle" fill="#64748b"
        font-size="10" font-family="sans-serif">RING HEALTH %</text>
</svg>"""


def build_report(summaries: list[dict], report_dir: str) -> str:
    """
    Build the full HTML infographic and write it to report_dir.
    Returns the absolute path of the written file.

    Each element in summaries must have been augmented with a 'briefing' key
    by dwdm_agent.py after ai_analyst.analyse_node() runs.
    """
    now       = datetime.now(timezone.utc)
    ts_str    = now.strftime('%Y%m%d_%H%M%S')
    ts_human  = now.strftime('%B %d, %Y  %H:%M UTC')
    filename  = f"dwdm_report_{ts_str}.html"
    filepath  = os.path.join(report_dir, filename)

    # --- Compute overall ring health ---
    scores = [s.get('briefing', {}).get('health_score', 0) for s in summaries]
    ring_score = round(sum(scores) / len(scores)) if scores else 0

    nodes_ok       = sum(1 for s in summaries if not s.get('errors'))
    nodes_critical = sum(1 for s in summaries
                         if s.get('briefing', {}).get('status') == 'CRITICAL')
    nodes_degraded = sum(1 for s in summaries
                         if s.get('briefing', {}).get('status') == 'DEGRADED')

    # --- Top 3 priorities across all nodes ---
    all_prios = []
    for s in summaries:
        b = s.get('briefing', {})
        for p in b.get('top_priorities', []):
            all_prios.append(f"[{s['tid']}] {p}")
    top3 = all_prios[:3]
    top3_html = ''.join(
        f'<li class="flex gap-3 py-2 border-b border-gray-700">'
        f'<span class="text-red-400 font-bold shrink-0">{i+1}.</span>'
        f'<span class="text-gray-200 text-sm">{p}</span></li>'
        for i, p in enumerate(top3)
    ) or '<li class="text-gray-500 text-sm py-2">No critical priorities detected.</li>'

    # --- Audit badge ---
    total = len(summaries)
    audit_color  = '#22c55e' if nodes_ok == total else '#ef4444'
    audit_label  = f"{nodes_ok}/{total} nodes archived &amp; committed"

    # --- SVG topology ---
    svg_ring = _build_svg(summaries)

    # --- Gauge ---
    gauge = _gauge_svg(ring_score)

    # --- Per-node cards ---
    node_cards = ''.join(s.get('briefing_html', '') for s in summaries)

    ring_status_label = ('HEALTHY' if ring_score >= 80
                         else 'DEGRADED' if ring_score >= 50
                         else 'CRITICAL')
    ring_status_color = _status_color(ring_status_label)

    html = f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>DWDM AI Expert — {ts_human}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ background:#0f172a; font-family: 'Inter', sans-serif; }}
  .node-card {{ transition: transform .2s; }}
  .node-card:hover {{ transform: translateY(-2px); }}
  @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0.2}} }}
  .blink {{ animation: blink 1.2s infinite; }}
</style>
</head>
<body class="text-gray-100 min-h-screen p-6">

  <!-- ── Header ── -->
  <header class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-2xl font-black text-white tracking-tight">
        DWDM AI Expert
        <span class="ml-3 text-sm font-semibold px-3 py-1 rounded-full blink"
              style="background:{ring_status_color}22; color:{ring_status_color}">
          {ring_status_label}
        </span>
      </h1>
      <p class="text-gray-400 text-sm mt-1">
        Telecoms · Metro Manila Ring · {ts_human}
      </p>
    </div>
    <!-- Audit badge -->
    <div class="text-center px-4 py-2 rounded-xl border"
         style="border-color:{audit_color}44; background:{audit_color}11">
      <p class="text-xs text-gray-400 uppercase tracking-wider">Audit</p>
      <p class="font-bold text-sm" style="color:{audit_color}">{audit_label}</p>
    </div>
  </header>

  <!-- ── Top row: Gauge + Action Plan ── -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">

    <!-- Health Gauge -->
    <div class="bg-gray-800 rounded-xl p-5 border border-gray-700 flex flex-col items-center justify-center">
      <p class="text-xs text-gray-400 uppercase tracking-wider mb-2">Network Health Pulse</p>
      {gauge}
      <div class="flex gap-6 mt-3 text-xs text-center">
        <div><span class="text-red-400 font-bold text-lg">{nodes_critical}</span><br/>
             <span class="text-gray-500">Critical</span></div>
        <div><span class="text-amber-400 font-bold text-lg">{nodes_degraded}</span><br/>
             <span class="text-gray-500">Degraded</span></div>
        <div><span class="text-green-400 font-bold text-lg">{nodes_ok}</span><br/>
             <span class="text-gray-500">Healthy</span></div>
      </div>
    </div>

    <!-- Ops Action Plan -->
    <div class="lg:col-span-2 bg-gray-800 rounded-xl p-5 border border-gray-700">
      <p class="text-xs text-gray-400 uppercase tracking-wider mb-3">
        &#9888;&#65039; Ops Action Plan — Top Priorities
      </p>
      <ul class="list-none m-0 p-0">{top3_html}</ul>
    </div>

  </div>

  <!-- ── Topology Map ── -->
  <div class="bg-gray-800 rounded-xl p-5 border border-gray-700 mb-8">
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-3">Live Topology Map</p>
    <div class="w-full" style="height:300px; overflow:hidden;">
      {svg_ring}
    </div>
    <div class="flex gap-6 mt-3 text-xs">
      <span class="flex items-center gap-1">
        <span class="inline-block w-3 h-3 rounded-full bg-green-500"></span> Healthy
      </span>
      <span class="flex items-center gap-1">
        <span class="inline-block w-3 h-3 rounded-full bg-amber-500"></span> Degraded
      </span>
      <span class="flex items-center gap-1">
        <span class="inline-block w-3 h-3 rounded-full bg-red-500 blink"></span> Critical
      </span>
    </div>
  </div>

  <!-- ── Per-Node Cards Grid ── -->
  <div>
    <p class="text-xs text-gray-400 uppercase tracking-wider mb-4">Node Details</p>
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
      {node_cards}
    </div>
  </div>

  <footer class="mt-10 text-center text-xs text-gray-600">
    Generated by DWDM AI Expert · Telecoms · {ts_human}
  </footer>

</body>
</html>"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)

    return filepath


def _status_color(status: str) -> str:
    return {'HEALTHY': '#22c55e', 'DEGRADED': '#f59e0b', 'CRITICAL': '#ef4444'}.get(status, '#6b7280')
