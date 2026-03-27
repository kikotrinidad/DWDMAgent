"""
DWDM TL1 Command Stack — shared across all agents.

This file defines the ordered 7-step command sequence per the PRD.
CTAGs are placeholders only — the TL1Session connector replaces them
with auto-incrementing session CTAGs at runtime.

To skip a command from an agent, simply exclude it from the list you
pass to the orchestrator — do not remove entries here.

CRITICAL — The "Head of Ops" Gatekeeper Rule (Steps 1 & 2):
  CLR-AUDIT-LOG (Step 2) is FORBIDDEN unless:
    1. RTRV-AUDIT-LOG (Step 1) returned COMPLD, AND
    2. The raw response was successfully committed to the DB.
  This logic is enforced in agent.py — never bypass it.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class TL1Command:
    step:        int
    command:     str
    description: str
    ctag_hint:   str          # placeholder ctag in the command string
    gated:       bool = False  # True = has a prerequisite gate check
    gate_for:    Optional[int] = None  # step number this command is gated by


# ---------------------------------------------------------------------------
# The 7-Step Command Stack
# ---------------------------------------------------------------------------

COMMANDS = [
    TL1Command(
        step=1,
        command="RTRV-AUDIT-LOG:::100;",
        description="Archive: Stream and save current audit log to DB",
        ctag_hint="100",
    ),
    # Step 2 (CLR-AUDIT-LOG) removed — nodes return DENY for this user account.
    # Audit log is still archived to DB via Step 1.
    TL1Command(
        step=3,
        command="RTRV-MAP-NETWORK:::102;",
        description="Map: Identify 19-node topology and neighbor relationships",
        ctag_hint="102",
    ),
    TL1Command(
        step=4,
        command="RTRV-INV::ALL:103;",
        description="Inventory: Audit hardware (SMR2 backbone vs 10G-LC client cards)",
        ctag_hint="103",
    ),
    TL1Command(
        step=5,
        command="RTRV-ALM-ALL::ALL:104;",
        description="Alarms: Identify CR/MJ alarms (e.g. LOS-P on Side D)",
        ctag_hint="104",
    ),
    TL1Command(
        step=6,
        command="RTRV-COND-ALL:::105;",
        description="Impact: Detect SQUELCHED ports indicating customer outage",
        ctag_hint="105",
    ),
    TL1Command(
        step=7,
        command="RTRV-OCH::ALL:106;",
        description="Physics: Scrape raw OPWR (optical power) levels for all channels",
        ctag_hint="106",
    ),
]

# ---------------------------------------------------------------------------
# Convenience lookups
# ---------------------------------------------------------------------------

# All commands in order
ALL = COMMANDS

# Commands safe to run without gating (excludes CLR-AUDIT-LOG)
UNGATED = [c for c in COMMANDS if not c.gated]

# Commands by step number
BY_STEP = {c.step: c for c in COMMANDS}
