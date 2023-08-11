"""The 北京水费 integration."""
from __future__ import annotations
from homeassistant.const import Platform

from homeassistant.helpers.discovery import async_load_platform
from homeassistant.config_entries import ConfigType, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
)
from .bj_water import BJWater

from .const import DOMAIN, LOGGER, UPDATE_INTERVAL

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up platform from a ConfigEntry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


# async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up 北京水费 from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    LOGGER.warning("config: " + str(config))
    LOGGER.warning("hass data: " + str(hass.data[DOMAIN]))

    # user_code = config[DOMAIN].get("userCode")
    user_code = hass.data[DOMAIN].get("userCode")
    config[DOMAIN] = {"userCode": hass.data[DOMAIN].get("userCode")}

    LOGGER.warning("user code:" + str(user_code))
    LOGGER.warning("bj water: %s" % user_code)
    api = BJWater(async_create_clientsession(hass), user_code)
    coordinator = DataUpdateCoordinator(
        hass,
        LOGGER,
        name=DOMAIN,
        update_interval=UPDATE_INTERVAL,
        update_method=api.fetch_data,
    )
    await coordinator.async_refresh()
    hass.data[DOMAIN] = {
        "config": config[DOMAIN],
        "coordinator": coordinator,
    }
    hass.async_create_task(
        async_load_platform(hass, "sensor", DOMAIN, {}, config[DOMAIN])
    )

    return True
