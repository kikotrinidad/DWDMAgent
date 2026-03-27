#!/usr/bin/env python3
"""
DWDM AI Expert Agent — Central Orchestrator (multi-process)

This orchestrator delegates work to specialized agents:
  Phase 1: node_poller.scrape_node() in separate processes
  Phase 2: ai_agent.run_ai_analysis() in separate processes
  Phase 3: report_builder.build_report() in orchestrator process

Why this model:
  - No threading/GIL concerns for worker execution
  - Each worker is an isolated process with its own TL1 session + DB connection
  - Clear agent boundaries for future horizontal scaling

Run:
  python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py
  python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py --node VM2-DC-NCS2006-DWDM
  python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py --pollers 8 --ai-agents 4
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path setup — allow imports from /opt/AIExperts/
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config
from DWDMAgent.node_poller import scrape_node
from DWDMAgent.ai_agent import run_ai_analysis
from DWDMAgent.network_report import build_report

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, 'dwdm_agent.log')),
    ]
)
logger = logging.getLogger('dwdm_agent')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPORT_DIR = os.path.join(os.path.dirname(__file__), 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)


def _run_phase1_pollers(nodes: list[dict], poller_count: int) -> tuple[list[dict], list[str], float]:
    """Run node pollers in separate processes and collect summaries."""
    t0 = datetime.now(timezone.utc)
    all_summaries: list[dict] = []
    failed_nodes: list[str] = []

    with ProcessPoolExecutor(max_workers=poller_count) as pool:
        futures = {pool.submit(scrape_node, node): node['tid'] for node in nodes}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                summary = future.result()
                all_summaries.append(summary)
                if summary.get('errors'):
                    failed_nodes.append(tid)
            except Exception as exc:
                logger.error(f"[{tid}] Poller process failed: {exc}")
                failed_nodes.append(tid)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    return all_summaries, failed_nodes, elapsed


def _run_phase2_ai(all_summaries: list[dict], ai_count: int) -> tuple[float, list[str]]:
    """Run AI agents in separate processes and update summaries in place."""
    if not all_summaries:
        return 0.0, []

    t1 = datetime.now(timezone.utc)
    failed_nodes: list[str] = []

    # by_tid is seeded with Phase 1 summaries so that if an AI future fails,
    # the node retains its Phase 1 data with no AI enrichment (non-fatal degradation).
    by_tid = {s['tid']: s for s in all_summaries}
    with ProcessPoolExecutor(max_workers=ai_count) as pool:
        futures = {pool.submit(run_ai_analysis, s): s['tid'] for s in all_summaries}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                updated = future.result()
                by_tid[tid] = updated
            except Exception as exc:
                logger.error(f"[{tid}] AI process failed: {exc}")
                failed_nodes.append(tid)

    all_summaries[:] = [by_tid[s['tid']] for s in all_summaries]
    elapsed = (datetime.now(timezone.utc) - t1).total_seconds()
    return elapsed, failed_nodes


def main():
    parser = argparse.ArgumentParser(
        description='DWDM AI Expert Agent — central orchestrator with multi-process agents'
    )
    parser.add_argument('--node', metavar='TID', help='Run only this node TID')
    parser.add_argument('--pollers', type=int, default=8, metavar='N',
                        help='Max parallel node poller processes (default: 8)')
    parser.add_argument('--ai-agents', type=int, default=5, metavar='N',
                        help='Max parallel AI agent processes (default: 5)')
    args = parser.parse_args()

    run_started = datetime.now(timezone.utc)
    logger.info('=' * 60)
    logger.info('DWDM AI Expert Agent — orchestrator starting')
    logger.info(f'Timestamp : {run_started.isoformat()}')

    nodes = config.get_nodes()
    if not nodes:
        logger.error('No nodes found in .env — check DWDM_*_IP entries.')
        sys.exit(1)

    if args.node:
        nodes = [n for n in nodes if n['tid'] == args.node]
        if not nodes:
            logger.error(f"Node TID '{args.node}' not found in .env")
            sys.exit(1)

    total_nodes = len(nodes)
    poller_count = max(1, min(args.pollers, total_nodes))
    ai_count = max(1, min(args.ai_agents, total_nodes))

    logger.info(f'Nodes      : {total_nodes}')
    logger.info(f'Pollers    : {poller_count} processes')
    logger.info(f'AI Agents  : {ai_count} processes')
    logger.info('=' * 60)

    logger.info('Phase 1 — Node Poller Agents')
    all_summaries, failed_nodes, scrape_elapsed = _run_phase1_pollers(nodes, poller_count)
    logger.info(
        f'Phase 1 complete — {len(all_summaries)}/{total_nodes} nodes in {scrape_elapsed:.1f}s'
    )
    if failed_nodes:
        logger.warning(f"Nodes with scrape errors: {', '.join(sorted(failed_nodes))}")

    logger.info('Phase 2 — AI Agents')
    ai_elapsed, ai_failed_nodes = _run_phase2_ai(all_summaries, ai_count)
    logger.info(f'Phase 2 complete — {len(all_summaries)} nodes analysed in {ai_elapsed:.1f}s')
    if ai_failed_nodes:
        logger.warning(f"Nodes with AI errors: {', '.join(sorted(ai_failed_nodes))}")

    logger.info('Phase 3 — HTML Report Build')
    report_path = None
    try:
        report_path = build_report(all_summaries, REPORT_DIR)
        logger.info(f'Report written: {report_path}')
    except Exception as exc:
        logger.error(f'Report generation failed: {exc}')

    total_elapsed = (datetime.now(timezone.utc) - run_started).total_seconds()
    logger.info('=' * 60)
    logger.info(f'Run complete — {len(all_summaries)} nodes processed, {len(failed_nodes)} scrape errors, {len(ai_failed_nodes)} AI errors')
    logger.info(f'Elapsed      — total {total_elapsed:.1f}s | scrape {scrape_elapsed:.1f}s | ai {ai_elapsed:.1f}s')
    if report_path:
        logger.info(f'HTML report  — {report_path}')
    logger.info('=' * 60)


if __name__ == '__main__':
    main()
