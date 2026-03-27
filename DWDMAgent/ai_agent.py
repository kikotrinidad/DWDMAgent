#!/usr/bin/env python3
"""
DWDM AI Agent

Loads a node scrape summary from the DB, calls Gemini for analysis,
and stores the AI briefing back to the DB.

Can be run standalone (for re-analysis / testing) or called by the
orchestrator (dwdm_agent.py) via ProcessPoolExecutor.

Standalone usage:
    python3 /opt/AIExperts/DWDMAgent/ai_agent.py --tid VM2-DC-NCS2006-DWDM

When called by the orchestrator the summary dict (already scraped) is
passed directly — no DB read needed.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config
from DWDMAgent import db
from DWDMAgent.ai_analyst import analyse_node   # Gemini call + JSON parse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker — called by orchestrator's ProcessPoolExecutor
# ---------------------------------------------------------------------------

def run_ai_analysis(summary: dict) -> dict:
    """
    Call Gemini for one node summary dict and persist the briefing to DB.

    Creates and owns its own DB connection — safe to run in a separate
    process with no shared state.

    Args:
        summary: dict returned by node_poller.scrape_node()

    Returns:
        The same summary dict, with 'briefing' and 'briefing_html' added.
        On failure, briefing == {} and briefing_html == ''.
    """
    tid       = summary['tid']
    device_id = summary['device_id']

    conn = db.get_connection()
    conn.autocommit = False
    try:
        briefing_json, briefing_html = analyse_node(summary)
        db.insert_ai_briefing(conn, device_id, briefing_json, briefing_html)
        conn.commit()
        summary['briefing']      = briefing_json
        summary['briefing_html'] = briefing_html
        logger.info(f"[{tid}] AI briefing stored  "
                    f"(score={briefing_json.get('health_score','?')}  "
                    f"status={briefing_json.get('status','?')})")
    except Exception as e:
        logger.error(f"[{tid}] AI analysis failed: {e}")
        summary['briefing']      = {}
        summary['briefing_html'] = ''
        conn.rollback()
    finally:
        conn.close()

    return summary


# ---------------------------------------------------------------------------
# Standalone: load most-recent scrape data from DB, re-run AI
# ---------------------------------------------------------------------------

def _load_summary_from_db(conn, tid: str) -> dict | None:
    """
    Reconstruct a summary dict from the latest DB records for a given TID.
    Used for standalone re-analysis without re-scraping.
    """
    cur = conn.cursor()

    # Resolve device_id
    cur.execute("""
        SELECT id, site_name
          FROM devices
         WHERE tid = %s AND device_type = 'DWDM'
        ORDER BY created_at DESC LIMIT 1
    """, (tid,))
    row = cur.fetchone()
    if not row:
        return None
    device_id, site = row

    # Alarms
    cur.execute("""
        SELECT severity, condition_type, service_affecting, location, direction,
               description, aid, occurred_at
          FROM dwdm_alarms WHERE device_id = %s
    """, (device_id,))
    cols = [d[0] for d in cur.description]
    alarms = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Conditions
    cur.execute("""
        SELECT condition_type, service_affecting, location, direction, description, aid
          FROM dwdm_conditions WHERE device_id = %s
    """, (device_id,))
    cols = [d[0] for d in cur.description]
    conditions = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Optical
    cur.execute("""
        SELECT aid, channel_wavelength_nm, opwr_dbm, opwr_oor, power_state, direction
          FROM dwdm_optical_channels WHERE device_id = %s
    """, (device_id,))
    cols = [d[0] for d in cur.description]
    optical = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Inventory
    cur.execute("""
        SELECT aid, card_type, serial_number, clei, product_id, hw_rev
          FROM dwdm_inventory WHERE device_id = %s
    """, (device_id,))
    cols = [d[0] for d in cur.description]
    inventory = [dict(zip(cols, r)) for r in cur.fetchall()]

    cur.close()
    return {
        'tid':        tid,
        'site':       site or '',
        'device_id':  device_id,
        'alarms':     alarms,
        'conditions': conditions,
        'inventory':  inventory,
        'topology':   [],
        'optical':    optical,
        'errors':     [],
    }


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _setup_logging():
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(log_dir, 'ai_agent.log')),
        ]
    )


def main():
    _setup_logging()
    parser = argparse.ArgumentParser(
        description='DWDM AI Agent — (re-)analyse one node from DB using Gemini'
    )
    parser.add_argument('--tid', required=True, metavar='TID',
                        help='Node TID to analyse (must have existing DB scrape data)')
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        summary = _load_summary_from_db(conn, args.tid)
    finally:
        conn.close()

    if not summary:
        logging.error(f"No DB records found for TID '{args.tid}'")
        sys.exit(1)

    logging.info(f"Loaded summary for {args.tid}: "
                 f"{len(summary['alarms'])} alarms, "
                 f"{len(summary['optical'])} optical channels")

    result = run_ai_analysis(summary)
    b = result.get('briefing', {})

    print("\n── AI Briefing ────────────────────────────────")
    print(f"  Node   : {args.tid}")
    print(f"  Score  : {b.get('health_score', 'N/A')}")
    print(f"  Status : {b.get('status', 'N/A')}")
    print(f"  Action : {b.get('ops_action', '')[:120]}")
    if b.get('top_priorities'):
        print("  Priorities:")
        for p in b['top_priorities']:
            print(f"    - {p}")
    print("──────────────────────────────────────────────\n")

    sys.exit(0 if b else 1)


if __name__ == '__main__':
    main()
