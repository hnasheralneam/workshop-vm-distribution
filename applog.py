import logging
import logging.handlers
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOG_FILE = os.getenv("LOG_FILE", "server.log")
if not os.path.isabs(LOG_FILE):
    LOG_FILE = str(Path(__file__).parent / LOG_FILE)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    os.chmod(LOG_FILE, 0o600)

log = logging.getLogger("app")
