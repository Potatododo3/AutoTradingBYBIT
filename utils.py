import hashlib
import hmac
import logging
import logging.handlers
import time
import uuid
from typing import Any


def generate_trade_id() -> str:
    return str(uuid.uuid4())[:8].upper()


def timestamp_ms() -> int:
    return int(time.time() * 1000)


def format_number(value: float, decimals: int = 4) -> str:
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def parse_risk(risk_str: str) -> tuple[float, str]:
    """Parse '1%' -> (1.0, 'percent') or '100$' -> (100.0, 'dollar')."""
    risk_str = risk_str.strip()
    if risk_str.endswith("%"):
        return float(risk_str[:-1]), "percent"
    elif risk_str.endswith("$"):
        return float(risk_str[:-1]), "dollar"
    elif risk_str.startswith("$"):
        return float(risk_str[1:]), "dollar"
    else:
        raise ValueError(f"Cannot parse risk: {risk_str!r}. Use '1%' or '100$'")


def setup_logging() -> None:
    """
    Configure logging:
      - Console: INFO and above, coloured prefix
      - File:    DEBUG and above → bot.log (rotates at 5MB, keeps 3 backups)
    """
    log_format = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # ── Console handler (INFO+) ───────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(log_format, datefmt=date_fmt))
    root.addHandler(console)

    # ── Rotating file handler (DEBUG+) ────────────────────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        "bot.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_fmt))
    root.addHandler(file_handler)

    # Silence noisy libraries (still captured in file at WARNING+)
    for lib in ("httpx", "telegram", "hpack", "httpcore"):
        logging.getLogger(lib).setLevel(logging.WARNING)