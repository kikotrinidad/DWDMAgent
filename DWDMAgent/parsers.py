"""
TL1 response parsers for all DWDM command outputs.

Each parser receives the full raw TL1 response string (may contain multiple
COMPLD pages and autonomous messages) and returns a list of dicts ready for
database insertion.

Command → Parser mapping:
  RTRV-MAP-NETWORK  → parse_map_network()
  RTRV-INV          → parse_inventory()
  RTRV-ALM-ALL      → parse_alarms()
  RTRV-COND-ALL     → parse_conditions()
  RTRV-OCH          → parse_optical_channels()
"""

import ipaddress
import re
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def extract_data_lines(response):
    """
    Extract quoted data lines from a raw TL1 response.

    TL1 data lines have the form:
        <spaces>"content here"<optional spaces>

    Outer quotes are stripped. Inner escaped quotes (\") are unescaped.
    Autonomous message header lines and COMPLD/DENY control lines are ignored.
    """
    lines = []
    for line in response.splitlines():
        m = re.match(r'^\s+"(.+)"\s*,?\s*$', line)
        if m:
            content = m.group(1).replace('\\"', '"')
            lines.append(content)
    return lines


def parse_kv_pairs(s):
    """
    Parse a KEY=VAL,KEY=VAL,... string into a dict.
    Handles quoted values (KEY="some value") and empty/N/A values (→ None).
    Splits on commas that precede an UPPERCASE= pattern to avoid splitting
    inside values.
    """
    result = {}
    s = s.strip().strip(',')
    if not s:
        return result
    # Split on comma that is immediately followed by an uppercase key= pattern
    tokens = re.split(r',(?=[A-Z][A-Z0-9]*=)', s)
    for token in tokens:
        if '=' not in token:
            continue
        key, _, val = token.partition('=')
        key = key.strip()
        val = val.strip().strip('"')
        result[key] = None if val in ('', 'N/A') else val
    return result


# ---------------------------------------------------------------------------
# CMD3 — RTRV-MAP-NETWORK
# Format: "IP,TID,Model"
# ---------------------------------------------------------------------------

def parse_map_network(response):
    """
    Parse RTRV-MAP-NETWORK response.
    Returns list of dicts: {ip_address, tid, model}
    """
    records = []
    for line in extract_data_lines(response):
        parts = [p.strip() for p in line.split(',', 2)]
        if len(parts) < 2:
            continue
        try:
            ipaddress.ip_address(parts[0])
        except ValueError:
            logger.debug(f"Skipping non-topology line in MAP-NETWORK response: {line}")
            continue
        records.append({
            'ip_address': parts[0],
            'tid':        parts[1],
            'model':      parts[2] if len(parts) > 2 else None,
        })
    return records


# ---------------------------------------------------------------------------
# CMD4 — RTRV-INV
# Format: "AID,CARDTYPE::PN=..,HWREV=..,FWREV=..,SN=..,CLEI=..,PID=..,VID=..,ACTUALCARDNAME=.."
# Note: Response is paged (multiple COMPLD blocks) — extract_data_lines handles all pages.
# ---------------------------------------------------------------------------

def parse_inventory(response):
    """
    Parse RTRV-INV response.
    Returns list of dicts: {aid, card_type, part_number, hw_rev, fw_rev,
                             serial_number, clei, pid, vid, actual_card_name}
    """
    records = []
    for line in extract_data_lines(response):
        if '::' not in line:
            continue
        identity, _, kv_part = line.partition('::')

        # identity = "AID,CARDTYPE" (card type may be quoted)
        id_parts = identity.split(',', 1)
        aid       = id_parts[0].strip().strip('"')
        card_type = id_parts[1].strip().strip('"') if len(id_parts) > 1 else None

        kv = parse_kv_pairs(kv_part)

        records.append({
            'aid':              aid,
            'card_type':        card_type,
            'part_number':      kv.get('PN'),
            'hw_rev':           kv.get('HWREV'),
            'fw_rev':           kv.get('FWREV'),
            'serial_number':    kv.get('SN'),
            'clei':             kv.get('CLEI'),
            'pid':              kv.get('PID'),
            'vid':              kv.get('VID'),
            'actual_card_name': kv.get('ACTUALCARDNAME'),
        })
    return records


# ---------------------------------------------------------------------------
# CMD5 — RTRV-ALM-ALL
# Format: "AID,LAYER:SEVERITY,CONDTYPE,SRVEFF,MM-DD,HH-MM-SS,LOCATION,DIRECTION:"DESC",CARD"
# The colon before "DESC" separates direction from description.
# ---------------------------------------------------------------------------

_ALARM_RE = re.compile(
    r'^(?P<aid>[\w\-\.]+),'
    r'(?P<layer>[A-Z0-9]+):'
    r'(?P<severity>CR|MJ|MN|NA|NR|NE),'
    r'(?P<condtype>[\w\-\^]+),'
    r'(?P<srveff>SA|NSA),'
    r'(?P<date>\d{2}-\d{2}),'
    r'(?P<time>\d{2}-\d{2}-\d{2}),'
    r'(?P<location>NEND|FEND|NEND-FEND)?,'
    r'(?P<direction>\w+)'
    r':"(?P<description>[^"]+)"'
    r'(?:,(?P<card>[\w\-]*))?$'
)


def parse_alarms(response):
    """
    Parse RTRV-ALM-ALL response.
    Returns list of dicts: {aid, layer, severity, condition_type, service_effect,
                             alarm_date, alarm_time, location, direction, description, card_type}
    """
    records = []
    for line in extract_data_lines(response):
        m = _ALARM_RE.match(line)
        if m:
            records.append({
                'aid':            m.group('aid'),
                'layer':          m.group('layer'),
                'severity':       m.group('severity'),
                'condition_type': m.group('condtype'),
                'service_effect': m.group('srveff'),
                'alarm_date':     m.group('date'),
                'alarm_time':     m.group('time'),
                'location':       m.group('location'),
                'direction':      m.group('direction'),
                'description':    m.group('description'),
                'card_type':      m.group('card') or None,
            })
        else:
            logger.debug(f"Alarm line did not match pattern: {line}")
    return records


# ---------------------------------------------------------------------------
# CMD6 — RTRV-COND-ALL
# Format: "AID,LAYER:SEVERITY,CONDTYPE,SRVEFF,MM-DD,HH-MM-SS,LOCATION,DIRECTION,"DESC""
# Description is comma-delimited here (not colon like alarms). Location can be empty.
# ---------------------------------------------------------------------------

_COND_RE = re.compile(
    r'^(?P<aid>[\w\-\.]+),'
    r'(?P<layer>[A-Z0-9]+):'
    r'(?P<severity>CR|MJ|MN|NA|NR|NE),'
    r'(?P<condtype>[\w\-\^]+),'
    r'(?P<srveff>SA|NSA),'
    r'(?P<date>\d{2}-\d{2}),'
    r'(?P<time>\d{2}-\d{2}-\d{2}),'
    r'(?P<location>NEND|FEND|NEND-FEND)?,'
    r'(?P<direction>\w*),'
    r'"(?P<description>[^"]+)"$'
)


def parse_conditions(response):
    """
    Parse RTRV-COND-ALL response.
    Returns list of dicts: {aid, layer, severity, condition_type, service_effect,
                             event_date, event_time, location, direction, description}
    """
    records = []
    for line in extract_data_lines(response):
        m = _COND_RE.match(line)
        if m:
            records.append({
                'aid':            m.group('aid'),
                'layer':          m.group('layer'),
                'severity':       m.group('severity'),
                'condition_type': m.group('condtype'),
                'service_effect': m.group('srveff'),
                'event_date':     m.group('date'),
                'event_time':     m.group('time'),
                'location':       m.group('location'),
                'direction':      m.group('direction') or None,
                'description':    m.group('description'),
            })
        else:
            logger.debug(f"Condition line did not match pattern: {line}")
    return records


# ---------------------------------------------------------------------------
# CMD7 — RTRV-OCH
# Format: "AID:PST_SST_PSTQ:KVPAIRS:ADMIN_STATE,"
# AID encodes: LINEWL-{slot}-{port}-{RX|TX}-{wavelength_nm}
#           or PCHAN-{slot}-{num}-{TX|RX}  (planned channel, no OPWR)
# Response is heavily paged (16+ pages) — extract_data_lines handles all.
# ---------------------------------------------------------------------------

_LINEWL_RE = re.compile(r'^LINEWL-(\d+)-(\d+)-(RX|TX)-([\d.]+)$')
_PCHAN_RE  = re.compile(r'^PCHAN-(\d+)-(\d+)-(TX|RX)$')


def parse_optical_channels(response):
    """
    Parse RTRV-OCH response.
    Returns list of dicts: {aid, slot, port, direction, wavelength_nm, op_type,
                             opwr_dbm, voa_mode, voa_attn_db, voa_ref_attn,
                             if_index, admin_state, exp_wavelength}
    """
    records = []
    for line in extract_data_lines(response):
        # Split AID : PST/SST/PSTQ : kv_pairs : admin_state
        parts = line.split(':', 3)
        if len(parts) < 3:
            continue

        aid        = parts[0].strip()
        kv_part    = parts[2].strip() if len(parts) > 2 else ''
        admin_state = parts[3].strip().rstrip(',') if len(parts) > 3 else None

        kv = parse_kv_pairs(kv_part)

        # Parse AID structure
        slot = port = direction = wavelength = exp_wavelength = None

        m = _LINEWL_RE.match(aid)
        if m:
            slot       = int(m.group(1))
            port       = int(m.group(2))
            direction  = m.group(3)
            wavelength = float(m.group(4))
        else:
            m = _PCHAN_RE.match(aid)
            if m:
                slot          = int(m.group(1))
                port          = int(m.group(2))
                direction     = m.group(3)
                exp_wavelength = kv.get('EXPWLEN')

        # OPWR is absent on passive/unlit channels → NULL
        opwr_raw      = kv.get('OPWR')
        voa_attn_raw  = kv.get('VOAATTN')
        voa_refa_raw  = kv.get('VOAREFATTN')

        records.append({
            'aid':            aid,
            'slot':           slot,
            'port':           port,
            'direction':      direction,
            'wavelength_nm':  wavelength,
            'op_type':        kv.get('OPTYPE'),
            'opwr_dbm':       float(opwr_raw)     if opwr_raw     else None,
            'voa_mode':       kv.get('VOAMODE'),
            'voa_attn_db':    float(voa_attn_raw) if voa_attn_raw else None,
            'voa_ref_attn':   float(voa_refa_raw) if voa_refa_raw else None,
            'if_index':       kv.get('IFINDEX'),
            'admin_state':    admin_state,
            'exp_wavelength': exp_wavelength,
        })
    return records
