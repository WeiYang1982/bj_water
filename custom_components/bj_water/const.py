"""Constants for the 北京水费 integration."""
import logging
from datetime import timedelta
from homeassistant.const import Platform


DOMAIN = "bj_water"
PLATFORMS: list[Platform] = [Platform.SENSOR]
LOGGER = logging.getLogger(__package__)
UPDATE_INTERVAL = timedelta(days=1)
# UPDATE_INTERVAL = timedelta(minutes=1)
