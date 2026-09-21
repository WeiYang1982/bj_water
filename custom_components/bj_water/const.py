"""Constants for the 北京水费 integration."""
import logging
from datetime import timedelta


DOMAIN = "bj_water"
LOGGER = logging.getLogger(__package__)
UPDATE_INTERVAL = timedelta(days=1)
# UPDATE_INTERVAL = timedelta(minutes=1)

# Token 有效期（天）
TOKEN_VALIDITY_DAYS = 104

