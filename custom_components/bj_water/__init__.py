"""The 北京水费 integration."""
from __future__ import annotations

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.components.sensor import SensorEntity

from .bj_water import BJWater, InvalidData
from .const import DOMAIN, LOGGER, UPDATE_INTERVAL, TOKEN_VALIDITY_DAYS
from datetime import datetime, timezone


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 北京水费 from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # 确保 token_added_at 存在（兼容旧配置）
    if "token_added_at" not in entry.data:
        now_iso = datetime.now(timezone.utc).isoformat()
        new_data = dict(entry.data)
        new_data["token_added_at"] = now_iso
        hass.config_entries.async_update_entry(entry, data=new_data)
        LOGGER.info("初始化 token_added_at: %s", now_iso)

    user_code = entry.data["userCode"]
    token = entry.data.get("token", "")
    secret_key_enc = entry.data.get("secretKeyEnc", "")
    md5_salt_enc = entry.data.get("md5SaltEnc", "")
    api = BJWater(async_create_clientsession(hass), user_code, token, secret_key_enc, md5_salt_enc)

    coordinator = DataUpdateCoordinator(
        hass,
        logger=LOGGER,
        name=DOMAIN,
        update_interval=UPDATE_INTERVAL,
        update_method=api.fetch_data,
    )

    # 尝试刷新数据。如果认证失败，DataUpdateCoordinator 会捕获 ConfigEntryAuthFailed
    # 并自动调用 async_start_reauth，无需手动处理。
    try:
        await coordinator.async_refresh()
    except ConfigEntryAuthFailed:
        entry.async_start_reauth(hass)
        raise
    except InvalidData:
        raise

    hass.data[DOMAIN][entry.entry_id] = {
        "config": {
            "userCode": user_code,
        },
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class TokenValiditySensor(SensorEntity):
    """Sensor to track token validity remaining days."""

    def __init__(self, entry):
        """Initialize the sensor."""
        self._entry = entry
        # 使用 unique_id 确保 HA 能通过实体注册表正确复用同一实体
        self._attr_unique_id = f"{DOMAIN}.{entry.data['userCode']}_token_validity"
        self._attr_name = "Token 有效期"
        self._attr_icon = "mdi:clock-alert"
        self._attr_native_unit_of_measurement = "天"
        self._attr_should_poll = False

    @property
    def state(self):
        """Return the remaining days."""
        token_added_at = self._entry.data.get("token_added_at")
        if not token_added_at:
            return None
        try:
            added_time = datetime.fromisoformat(token_added_at)
            # 如果时区信息缺失，假设为 UTC
            if added_time.tzinfo is None:
                added_time = added_time.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            elapsed_days = (now - added_time).days
            remaining = TOKEN_VALIDITY_DAYS - elapsed_days
            return max(remaining, 0)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        token_added_at = self._entry.data.get("token_added_at")
        attrs = {}
        if token_added_at:
            attrs["添加时间"] = token_added_at
            try:
                added_time = datetime.fromisoformat(token_added_at)
                if added_time.tzinfo is None:
                    added_time = added_time.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                elapsed_days = (now - added_time).days
                remaining = TOKEN_VALIDITY_DAYS - elapsed_days
                attrs["已使用天数"] = elapsed_days
                attrs["总有效期"] = TOKEN_VALIDITY_DAYS
                if remaining <= 0:
                    attrs["状态"] = "已过期"
                elif remaining <= 7:
                    attrs["状态"] = "即将过期"
                else:
                    attrs["状态"] = "有效"
            except (ValueError, TypeError):
                attrs["状态"] = "未知"
        return attrs

    def async_reset_token(self, hass: HomeAssistant) -> None:
        """Reset token validity timestamp."""
        now_iso = datetime.now(timezone.utc).isoformat()
        new_data = dict(self._entry.data)
        new_data["token_added_at"] = now_iso
        hass.config_entries.async_update_entry(self._entry, data=new_data)
        LOGGER.info("Token 有效期已重置: %s", now_iso)
        # 触发传感器更新
        self.async_schedule_update_ha_state()
