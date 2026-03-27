"""
Shared configuration loader for AIExperts agents.
Reads /opt/AIExperts/.env and exposes helpers for all agents.
"""

import os
import re
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"


def _load_env():
    """Parse .env file into os.environ without overriding existing vars."""
    if not _ENV_PATH.exists():
        return
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                if key not in os.environ:
                    os.environ[key] = val


_load_env()


def get(key, default=None):
    """Get a config value by key."""
    return os.getenv(key, default)


def get_db_config():
    """Return database connection parameters from .env — no credential defaults in code."""
    return {
        "host":     get("DWDM_DB_HOST", "localhost"),
        "port":     int(get("DWDM_DB_PORT", "5432")),
        "dbname":   get("DWDM_DB_NAME"),
        "user":     get("DWDM_DB_USER"),
        "password": get("DWDM_DB_PASSWORD"),
    }


def get_nodes():
    """
    Dynamically discover all DWDM nodes from .env.
    Any key matching DWDM_*_IP where the value is non-empty defines a node.
    Returns a list of node dicts ordered by key name.
    """
    nodes = []
    ip_keys = sorted(k for k in os.environ if k.startswith("DWDM_") and k.endswith("_IP"))
    for key in ip_keys:
        ip = os.getenv(key, "").strip()
        if not ip:
            continue
        prefix = key[:-3]  # Strip _IP → e.g. DWDM_VM2_DC_NCS2006
        nodes.append({
            "env_prefix": prefix,
            "ip":         ip,
            "port":       int(os.getenv(f"{prefix}_PORT", "3082")),
            "tid":        os.getenv(f"{prefix}_TID", "").strip(),
            "site":       os.getenv(f"{prefix}_SITE", "").strip(),
            "user":       os.getenv(f"{prefix}_USER", "").strip(),
            "password":   os.getenv(f"{prefix}_PASS", "").strip(),
        })
    return nodes


def get_gemini_config():
    """Return Gemini AI configuration."""
    return {
        "api_key":    get("GEMINI_API_KEY"),
        "model":      get("GEMINI_MODEL_NAME", "gemini-3-flash-preview"),
        "max_output": int(get("AI_MAX_OUTPUT_TOKENS", "1500")),
        "max_input":  int(get("AI_MAX_INPUT_TOKENS", "1000000")),
    }



