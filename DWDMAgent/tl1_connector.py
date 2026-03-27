"""
TL1 Session connector for Cisco NCS 2000/2006 DWDM nodes.

Rules (per CLAUDE.md):
- Always logout cleanly with CANC-USER before closing.
- COMPLD is the command completion signal — send next command immediately after.
- Never use blind time.sleep() between COMPLD and the next command.
- Autonomous messages (A  0 REPT ...) are discarded automatically.
- Large responses (e.g. RTRV-OCH) are multi-page; use idle-based termination (5s).
"""

import telnetlib
import time
import re
import logging

logger = logging.getLogger(__name__)


class TL1Error(Exception):
    """Raised when a TL1 command returns DENY or cannot be completed."""


class TL1Session:
    """
    Manages a single TL1 session over Telnet to a DWDM node.

    Usage:
        with TL1Session(ip, port, tid, user, password) as session:
            ctag, resp = session.send_command("RTRV-MAP-NETWORK:::102;")
    """

    # Idle seconds to wait after last data before considering response complete.
    # Simple commands (login/logout): 2s
    # Paged commands (OCH, INV):      5s — data arrives in bursts separated by silence
    IDLE_SIMPLE = 2
    IDLE_PAGED  = 5

    def __init__(self, ip, port, tid, user, password):
        self.ip       = ip
        self.port     = port
        self.tid      = tid
        self.user     = user
        self.password = password
        self._tn      = None
        self._ctag    = 100  # auto-incrementing per-session ctag

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        """Open TCP connection and flush the initial banner."""
        logger.info(f"[{self.tid}] Connecting to {self.ip}:{self.port}")
        self._tn = telnetlib.Telnet(self.ip, self.port, timeout=30)
        # The node sends a short banner on connect — wait for it then discard
        time.sleep(2)
        self._tn.read_very_eager()

    def login(self):
        """Send ACT-USER and wait for COMPLD."""
        ctag = self._next_ctag()
        cmd  = f"ACT-USER::{self.user}:{ctag}::{self.password};\r\n"
        self._tn.write(cmd.encode('ascii'))
        resp = self._read_paged(ctag, timeout=30, idle_after=self.IDLE_SIMPLE)
        if f"M  {ctag} COMPLD" not in resp:
            raise TL1Error(f"[{self.tid}] Login failed. Response: {resp[:200]}")
        logger.info(f"[{self.tid}] Login OK")
        return resp

    def logout(self):
        """Send CANC-USER. Many NCS 2006 nodes close the TCP connection
        immediately on receipt — before sending COMPLD — which is valid TL1
        behaviour. We do a best-effort 1-second read instead of _read_paged
        so we never emit a spurious WARNING for this normal case."""
        ctag = self._next_ctag()
        cmd  = f"CANC-USER::{self.user}:{ctag};\r\n"
        try:
            self._tn.write(cmd.encode('ascii'))
        except Exception as e:
            logger.debug(f"[{self.tid}] Could not send CANC-USER: {e}")
            return ""
        # Brief wait then consume whatever the node sends back (may be nothing)
        time.sleep(1)
        try:
            data = self._tn.read_very_eager().decode('ascii', errors='replace')
        except EOFError:
            data = ""  # Node already closed — expected
        logger.info(f"[{self.tid}] Logout OK")
        return data

    def close(self):
        """Close the TCP connection."""
        if self._tn:
            self._tn.close()
            self._tn = None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def send_command(self, cmd_template, idle_after=None):
        """
        Send a TL1 command and return (ctag, full_response).

        The cmd_template uses any numeric ctag placeholder (e.g. :::102;)
        which is replaced with the session's auto-incrementing ctag.

        Args:
            cmd_template:  TL1 command string, e.g. "RTRV-OCH::ALL:106;"
            idle_after:    Override idle timeout (default: IDLE_PAGED = 5s)

        Returns:
            (ctag: str, response: str)
        """
        ctag = self._next_ctag()
        cmd  = self._inject_ctag(cmd_template, ctag)
        _idle = idle_after if idle_after is not None else self.IDLE_PAGED

        logger.debug(f"[{self.tid}] → {cmd.strip()}")
        self._tn.write(f"{cmd}\r\n".encode('ascii'))
        resp = self._read_paged(ctag, timeout=120, idle_after=_idle)

        if f"M  {ctag} COMPLD" in resp:
            pages = resp.count(f"M  {ctag} COMPLD")
            logger.debug(f"[{self.tid}] ← COMPLD ({pages} page(s))")
        elif f"M  {ctag} DENY" in resp:
            logger.error(f"[{self.tid}] DENY on command: {cmd.strip()}")
            raise TL1Error(f"[{self.tid}] Command DENIED: {cmd.strip()}")
        else:
            logger.warning(f"[{self.tid}] No COMPLD received for: {cmd.strip()}")

        return ctag, resp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_ctag(self):
        ctag = self._ctag
        self._ctag += 1
        return str(ctag)

    def _inject_ctag(self, cmd, ctag):
        """
        Replace the numeric ctag in a TL1 command with the session ctag.
        The ctag is always the last colon-delimited field before the semicolon.
        e.g. RTRV-INV::ALL:103;  →  RTRV-INV::ALL:200;
        """
        return re.sub(r':\d+;$', f':{ctag};', cmd.strip())

    def _read_paged(self, ctag, timeout=120, idle_after=5):
        """
        Read TL1 response data until idle after last byte received.

        Autonomous messages (A  0 REPT ...) are captured in the buffer
        but the caller's parsers filter them out — only COMPLD blocks matter.

        Terminates when:
          - At least one M  {ctag} COMPLD is seen AND no data has arrived
            for idle_after seconds (handles multi-page responses like RTRV-OCH).
          - Hard timeout reached.
          - Node closes connection (EOFError).
        """
        buffer    = ""
        start     = time.time()
        last_data = time.time()

        while time.time() - start < timeout:
            try:
                chunk = self._tn.read_very_eager().decode('ascii', errors='replace')
            except EOFError:
                logger.warning(f"[{self.tid}] Node closed connection (ctag={ctag})")
                break

            if chunk:
                buffer    += chunk
                last_data  = time.time()
            else:
                # No new data — check idle condition
                if (f"M  {ctag} COMPLD" in buffer and
                        (time.time() - last_data) >= idle_after):
                    break

            time.sleep(0.2)

        return buffer

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.logout()
        except Exception as e:
            logger.warning(f"[{self.tid}] Error during logout in __exit__: {e}")
        finally:
            self.close()
        return False  # Do not suppress exceptions
