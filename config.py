import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
AUTHORIZED_USER_ID: int = int(os.environ["AUTHORIZED_USER_ID"])

# Bybit
BYBIT_API_KEY: str  = os.environ["BYBIT_API_KEY"]
BYBIT_SECRET: str   = os.environ["BYBIT_SECRET"]
BYBIT_BASE_URL: str = "https://api.bybit.com"

# Discord
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# Database
DB_PATH: str = os.getenv("DB_PATH", "trades.db")

# Trading settings
CONFIRMATION_REQUIRED: bool = True
MAX_LEVERAGE: int = 125
DEFAULT_LEVERAGE: int = 10
API_RETRY_ATTEMPTS: int = 3
API_TIMEOUT: float = 10.0

# TP split percentages (must sum to 1.0)
TP1_PCT: float = 0.40
TP2_PCT: float = 0.30
TP3_PCT: float = 0.30

# CSV journal file
CSV_JOURNAL_PATH: str = "journal.csv"

# Debug / dry-run mode — when True, no real orders are placed
DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"