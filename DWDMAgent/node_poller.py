#!/usr/bin/env python3
"""
DWDM Node Poller Agent

Scrapes a single NCS 2006 node via TL1 and persists all data to the DB.
Can be run standalone (for testing / manual re-poll) or called by the
orchestrator (dwdm_agent.py) via ProcessPoolExecutor.

Standalone usage:
    python3 /opt/AIExperts/DWDMAgent/node_poller.py --tid VM2-DC-NCS2006-DWDM
    python3 /opt/AIExperts/DWDMAgent/node_poller.py --tid <TID> --no-ai

Return value (when called by orchestrator):
    A summary dict with keys: tid, site, device_id, alarms, conditions,
    inventory, topology, optical, errors.
    The orchestrator passes this to the AI agent phase.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path — works whether run standalone or imported by orchestrator
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config
from DWDMAgent.tl1_connector import TL1Session, TL1Error
from DWDMAgent import parsers, db
from DWDMAgent.dwdm_commands import BY_STEP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core scrape function — called by orchestrator's ProcessPoolExecutor
# ---------------------------------------------------------------------------

def scrape_node(node: dict) -> dict:
    """
    Run the full 7-step TL1 command stack against one node.

    Each call creates and owns its own TL1 session and DB connection.
    Safe to call from multiple concurrent processes with no shared state.

    Args:
        node: dict with keys: tid, ip, port, site, user, password

    Returns:
        summary dict — always returned even on partial failure; check
        summary['errors'] for a list of per-step failures.
    """
    tid      = node['tid']
    ip       = node['ip']
    port     = node['port']
    site     = node['site']
    user     = node['user']
    password = node['password']

    logger.info(f"[{tid}] Poller started  ({ip}:{port})")

    conn = db.get_connection()
    conn.autocommit = False

    device_id = db.upsert_device(conn, tid, ip, site_name=site,
                                 vendor='Cisco', protocol='TELNET', port=port)
    conn.commit()

    summary: dict = {
        'tid':        tid,
        'site':       site,
        'device_id':  device_id,
        'alarms':     [],
        'conditions': [],
        'inventory':  [],
        'topology':   [],
        'optical':    [],
        'errors':     [],
    }

    now = datetime.now(timezone.utc)

    try:
        with TL1Session(ip, port, tid, user, password) as session:

            # ------------------------------------------------------------------
            # Step 1 — RTRV-AUDIT-LOG
            # ------------------------------------------------------------------
            step = BY_STEP[1]
            logger.info(f"[{tid}] Step 1: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.commit_audit_log(conn, device_id, ctag, resp)
            except TL1Error as e:
                logger.error(f"[{tid}] Step 1 FAILED: {e}")
                summary['errors'].append(f"Step 1: {e}")
                conn.rollback()

            # ------------------------------------------------------------------
            # Step 3 — RTRV-MAP-NETWORK
            # ------------------------------------------------------------------
            step = BY_STEP[3]
            logger.info(f"[{tid}] Step 3: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.insert_raw_scrape(conn, device_id, 'RTRV-MAP-NETWORK', ctag, resp)
                topo = parsers.parse_map_network(resp)
                db.upsert_topology(conn, device_id, topo, scraped_at=now)
                conn.commit()
                summary['topology'] = topo
                logger.info(f"[{tid}] Topology: {len(topo)} peers")
            except TL1Error as e:
                logger.error(f"[{tid}] Step 3 FAILED: {e}")
                summary['errors'].append(f"Step 3: {e}")
                conn.rollback()

            # ------------------------------------------------------------------
            # Step 4 — RTRV-INV
            # ------------------------------------------------------------------
            step = BY_STEP[4]
            logger.info(f"[{tid}] Step 4: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.insert_raw_scrape(conn, device_id, 'RTRV-INV', ctag, resp)
                inv = parsers.parse_inventory(resp)
                db.upsert_inventory(conn, device_id, inv, scraped_at=now)
                conn.commit()
                summary['inventory'] = inv
                logger.info(f"[{tid}] Inventory: {len(inv)} cards")
            except TL1Error as e:
                logger.error(f"[{tid}] Step 4 FAILED: {e}")
                summary['errors'].append(f"Step 4: {e}")
                conn.rollback()

            # ------------------------------------------------------------------
            # Step 5 — RTRV-ALM-ALL
            # ------------------------------------------------------------------
            step = BY_STEP[5]
            logger.info(f"[{tid}] Step 5: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.insert_raw_scrape(conn, device_id, 'RTRV-ALM-ALL', ctag, resp)
                alarms = parsers.parse_alarms(resp)
                db.replace_alarms(conn, device_id, alarms, scraped_at=now)
                conn.commit()
                summary['alarms'] = alarms
                critical = sum(1 for a in alarms if a['severity'] == 'CR')
                major    = sum(1 for a in alarms if a['severity'] == 'MJ')
                logger.info(f"[{tid}] Alarms: {len(alarms)} total  CR:{critical}  MJ:{major}")
            except TL1Error as e:
                logger.error(f"[{tid}] Step 5 FAILED: {e}")
                summary['errors'].append(f"Step 5: {e}")
                conn.rollback()

            # ------------------------------------------------------------------
            # Step 6 — RTRV-COND-ALL
            # ------------------------------------------------------------------
            step = BY_STEP[6]
            logger.info(f"[{tid}] Step 6: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.insert_raw_scrape(conn, device_id, 'RTRV-COND-ALL', ctag, resp)
                conds = parsers.parse_conditions(resp)
                db.replace_conditions(conn, device_id, conds, scraped_at=now)
                conn.commit()
                summary['conditions'] = conds
                logger.info(f"[{tid}] Conditions: {len(conds)}")
            except TL1Error as e:
                logger.error(f"[{tid}] Step 6 FAILED: {e}")
                summary['errors'].append(f"Step 6: {e}")
                conn.rollback()

            # ------------------------------------------------------------------
            # Step 7 — RTRV-OCH
            # ------------------------------------------------------------------
            step = BY_STEP[7]
            logger.info(f"[{tid}] Step 7: {step.description}")
            try:
                ctag, resp = session.send_command(step.command)
                db.insert_raw_scrape(conn, device_id, 'RTRV-OCH', ctag, resp)
                optical = parsers.parse_optical_channels(resp)
                db.replace_optical_channels(conn, device_id, optical, scraped_at=now)
                conn.commit()
                summary['optical'] = optical
                logger.info(f"[{tid}] Optical channels: {len(optical)}")
            except TL1Error as e:
                logger.error(f"[{tid}] Step 7 FAILED: {e}")
                summary['errors'].append(f"Step 7: {e}")
                conn.rollback()

    except Exception as e:
        logger.exception(f"[{tid}] Unhandled error: {e}")
        summary['errors'].append(f"Fatal: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()

    logger.info(f"[{tid}] Poller done  (errors: {len(summary['errors'])})")
    return summary


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
            logging.FileHandler(os.path.join(log_dir, 'node_poller.log')),
        ]
    )


def main():
    _setup_logging()
    parser = argparse.ArgumentParser(
        description='DWDM Node Poller Agent — scrape one NCS 2006 node via TL1'
    )
    parser.add_argument('--tid', required=True, metavar='TID',
                        help='Node TID to scrape (must exist in .env)')
    args = parser.parse_args()

    nodes = config.get_nodes()
    match = [n for n in nodes if n['tid'] == args.tid]
    if not match:
        logging.error(f"TID '{args.tid}' not found in .env")
        sys.exit(1)

    summary = scrape_node(match[0])

    print("\n── Scrape Result ─────────────────────────────")
    print(f"  Node     : {summary['tid']}  ({summary['site']})")
    print(f"  Alarms   : {len(summary['alarms'])}  "
          f"(CR:{sum(1 for a in summary['alarms'] if a['severity']=='CR')}  "
          f"MJ:{sum(1 for a in summary['alarms'] if a['severity']=='MJ')})")
    print(f"  Optical  : {len(summary['optical'])} channels")
    print(f"  Topology : {len(summary['topology'])} peers")
    print(f"  Inventory: {len(summary['inventory'])} cards")
    print(f"  Errors   : {len(summary['errors'])}")
    if summary['errors']:
        for e in summary['errors']:
            print(f"    - {e}")
    print("──────────────────────────────────────────────\n")

    sys.exit(1 if summary['errors'] else 0)


if __name__ == '__main__':
    main()
