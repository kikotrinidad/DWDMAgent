"""
Database persistence layer for the DWDM Agent.

Uses the platform-wide AIExperts schema:
  - devices          : shared device registry (device_type='DWDM')
  - raw_scrapes      : verbatim TL1 blobs (any device type)
  - ai_briefings     : Gemini output (any device type)
  - dwdm_topology    : DWDM peer map
  - dwdm_inventory   : DWDM hardware cards
  - dwdm_alarms      : active alarms
  - dwdm_conditions  : standing conditions
  - dwdm_optical_channels : per-wavelength power readings

All functions accept a psycopg2 connection so the caller controls
transaction boundaries (commit/rollback).
"""

import logging
import psycopg2
import psycopg2.extras
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection():
    """Return a new psycopg2 connection using credentials from config."""
    params = config.get_db_config()
    return psycopg2.connect(**params)


# ---------------------------------------------------------------------------
# devices table (platform-wide registry)
# ---------------------------------------------------------------------------

def upsert_device(conn, tid, ip_address, site_name, model=None,
                  vendor='Cisco', protocol='TELNET', port=3082):
    """
    Insert or update a DWDM device in the platform devices registry.
    Returns the device_id (int).
    """
    sql = """
        INSERT INTO devices
            (device_type, hostname, ip_address, site_name, model,
             vendor, protocol, port)
        VALUES ('DWDM', %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (hostname) DO UPDATE
            SET ip_address = EXCLUDED.ip_address,
                site_name  = EXCLUDED.site_name,
                model      = EXCLUDED.model,
                updated_at = now()
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (tid, ip_address, site_name, model, vendor, protocol, port))
        return cur.fetchone()[0]


def get_device_id(conn, tid):
    """Return device_id for a given TID (hostname), or None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM devices WHERE hostname = %s", (tid,))
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Raw scrapes table
# ---------------------------------------------------------------------------

def insert_raw_scrape(conn, device_id, command, ctag, response_text, compld=True):
    """
    Persist a raw TL1 response blob.
    Returns the scrape_id (int).
    This is the prerequisite write for the gatekeeper check.
    """
    sql = """
        INSERT INTO raw_scrapes (device_id, command, ctag, response_text, compld, scraped_at)
        VALUES (%s, %s, %s, %s, %s, now())
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (device_id, command, ctag, response_text, compld))
        return cur.fetchone()[0]


def commit_audit_log(conn, device_id, ctag, response_text):
    """
    GATEKEEPER FUNCTION — Must be called and return True before CLR-AUDIT-LOG
    is permitted. Writes the audit log scrape to raw_scrapes and commits
    immediately so the data is durable before the clear is sent.

    Returns True on success, False on any DB error.
    """
    try:
        insert_raw_scrape(conn, device_id, "RTRV-AUDIT-LOG", ctag, response_text, compld=True)
        conn.commit()
        logger.info(f"[device_id={device_id}] Audit log committed to DB — CLR authorized.")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[device_id={device_id}] Audit log DB commit FAILED — CLR BLOCKED: {e}")
        return False


# ---------------------------------------------------------------------------
# dwdm_topology table (parsed from RTRV-MAP-NETWORK)
# ---------------------------------------------------------------------------

def upsert_topology(conn, device_id, records, scraped_at=None):
    """
    Insert or update topology entries for a device.
    records: list of dicts from parsers.parse_map_network()
    """
    if not records:
        return
    ts = scraped_at or datetime.utcnow()
    sql = """
        INSERT INTO dwdm_topology (device_id, peer_ip, peer_tid, peer_model, scraped_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (device_id, peer_ip) DO UPDATE
            SET peer_tid   = EXCLUDED.peer_tid,
                peer_model = EXCLUDED.peer_model,
                scraped_at = EXCLUDED.scraped_at
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, [
            (device_id, r['ip_address'], r['tid'], r['model'], ts)
            for r in records
        ])
    logger.debug(f"[device_id={device_id}] Upserted {len(records)} topology records")


# ---------------------------------------------------------------------------
# dwdm_inventory table (parsed from RTRV-INV)
# ---------------------------------------------------------------------------

def upsert_inventory(conn, device_id, records, scraped_at=None):
    """
    Insert or update hardware inventory for a device.
    records: list of dicts from parsers.parse_inventory()
    """
    if not records:
        return
    ts = scraped_at or datetime.utcnow()
    sql = """
        INSERT INTO dwdm_inventory
            (device_id, aid, card_type, part_number, hw_rev, fw_rev,
             serial_number, clei, pid, vid, actual_card_name, scraped_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_id, aid) DO UPDATE
            SET card_type       = EXCLUDED.card_type,
                part_number     = EXCLUDED.part_number,
                hw_rev          = EXCLUDED.hw_rev,
                fw_rev          = EXCLUDED.fw_rev,
                serial_number   = EXCLUDED.serial_number,
                clei            = EXCLUDED.clei,
                pid             = EXCLUDED.pid,
                vid             = EXCLUDED.vid,
                actual_card_name= EXCLUDED.actual_card_name,
                scraped_at      = EXCLUDED.scraped_at
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, [
            (device_id, r['aid'], r['card_type'], r['part_number'],
             r['hw_rev'], r['fw_rev'], r['serial_number'], r['clei'],
             r['pid'], r['vid'], r['actual_card_name'], ts)
            for r in records
        ])
    logger.debug(f"[device_id={device_id}] Upserted {len(records)} inventory records")


# ---------------------------------------------------------------------------
# dwdm_alarms table (parsed from RTRV-ALM-ALL)
# ---------------------------------------------------------------------------

def replace_alarms(conn, device_id, records, scraped_at=None):
    """
    Replace all current alarms for a device with the latest scrape.
    Uses DELETE + INSERT within the same transaction for atomicity.
    records: list of dicts from parsers.parse_alarms()
    """
    ts = scraped_at or datetime.utcnow()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dwdm_alarms WHERE device_id = %s", (device_id,))
        if records:
            sql = """
                INSERT INTO dwdm_alarms
                    (device_id, aid, layer, severity, condition_type, service_effect,
                     alarm_date, alarm_time, location, direction, description,
                     card_type, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            psycopg2.extras.execute_batch(cur, sql, [
                (device_id, r['aid'], r['layer'], r['severity'],
                 r['condition_type'], r['service_effect'],
                 r['alarm_date'], r['alarm_time'], r['location'],
                 r['direction'], r['description'], r['card_type'], ts)
                for r in records
            ])
    logger.debug(f"[device_id={device_id}] Replaced alarms ({len(records)} active)")


# ---------------------------------------------------------------------------
# dwdm_conditions table (parsed from RTRV-COND-ALL)
# ---------------------------------------------------------------------------

def replace_conditions(conn, device_id, records, scraped_at=None):
    """
    Replace all current conditions for a device with the latest scrape.
    records: list of dicts from parsers.parse_conditions()
    """
    ts = scraped_at or datetime.utcnow()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dwdm_conditions WHERE device_id = %s", (device_id,))
        if records:
            sql = """
                INSERT INTO dwdm_conditions
                    (device_id, aid, layer, severity, condition_type, service_effect,
                     event_date, event_time, location, direction, description, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            psycopg2.extras.execute_batch(cur, sql, [
                (device_id, r['aid'], r['layer'], r['severity'],
                 r['condition_type'], r['service_effect'],
                 r['event_date'], r['event_time'], r['location'],
                 r['direction'], r['description'], ts)
                for r in records
            ])
    logger.debug(f"[device_id={device_id}] Replaced conditions ({len(records)} active)")


# ---------------------------------------------------------------------------
# dwdm_optical_channels table (parsed from RTRV-OCH)
# ---------------------------------------------------------------------------

def replace_optical_channels(conn, device_id, records, scraped_at=None):
    """
    Replace all optical channel readings for a device with the latest scrape.
    records: list of dicts from parsers.parse_optical_channels()
    """
    ts = scraped_at or datetime.utcnow()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM dwdm_optical_channels WHERE device_id = %s", (device_id,))
        if records:
            sql = """
                INSERT INTO dwdm_optical_channels
                    (device_id, aid, slot, port, direction, wavelength_nm,
                     op_type, opwr_dbm, voa_mode, voa_attn_db, voa_ref_attn,
                     if_index, admin_state, exp_wavelength, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            psycopg2.extras.execute_batch(cur, sql, [
                (device_id, r['aid'], r['slot'], r['port'], r['direction'],
                 r['wavelength_nm'], r['op_type'], r['opwr_dbm'],
                 r['voa_mode'], r['voa_attn_db'], r['voa_ref_attn'],
                 r['if_index'], r['admin_state'], r['exp_wavelength'], ts)
                for r in records
            ])
    logger.debug(f"[device_id={device_id}] Replaced optical channels ({len(records)} records)")


# ---------------------------------------------------------------------------
# ai_briefings table (platform-wide, not DWDM-specific)
# ---------------------------------------------------------------------------

def insert_ai_briefing(conn, device_id, briefing_json, briefing_html):
    """
    Store the Gemini-generated briefing for a device scrape run.
    Returns the briefing_id (int).
    """
    sql = """
        INSERT INTO ai_briefings (device_id, briefing_json, briefing_html, generated_at)
        VALUES (%s, %s, %s, now())
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (device_id, psycopg2.extras.Json(briefing_json), briefing_html))
        return cur.fetchone()[0]
