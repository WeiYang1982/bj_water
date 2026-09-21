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
    # 注意：即使认证失败，也要继续加载 sensor 平台，让 token 有效期 sensor 能显示状态
    try:
        await coordinator.async_refresh()
        # 刷新成功，更新认证状态
        if entry.data.get("auth_status") != "ok":
            new_data = dict(entry.data)
            new_data["auth_status"] = "ok"
            hass.config_entries.async_update_entry(entry, data=new_data)
    except ConfigEntryAuthFailed:
        # 认证失败：更新认证状态并启动重新认证流程
        LOGGER.warning("初始数据认证失败，启动重新认证流程")
        new_data = dict(entry.data)
        new_data["auth_status"] = "failed"
        hass.config_entries.async_update_entry(entry, data=new_data)
        # 启动重新认证，但不 raise，让 sensor 平台继续加载
        entry.async_start_reauth(hass)
    except InvalidData:
        # 数据错误（如户号无效）：记录但继续
        LOGGER.warning(f"初始数据刷新失败: {InvalidData}")

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
        # 使用纯英文名称，确保 entity_id 稳定（中文会被转拼音，可能变化）
        self._attr_unique_id = f"{DOMAIN}.{entry.data['userCode']}_token_validity"
        self._attr_name = "Token Validity"
        self._attr_icon = "mdi:clock-alert"
        self._attr_native_unit_of_measurement = "days"
        self._attr_should_poll = False

    @property
    def state(self):
        """Return the remaining days or auth failure status."""
        # 如果认证失败，显示认证失败状态
        if self._entry.data.get("auth_status") == "failed":
            return "认证失败"
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
        attrs = {}
        # 认证状态
        auth_status = self._entry.data.get("auth_status")
        if auth_status == "failed":
            attrs["认证状态"] = "失败"
        elif auth_status == "ok":
            attrs["认证状态"] = "正常"
        else:
            attrs["认证状态"] = "未知"

        token_added_at = self._entry.data.get("token_added_at")
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
                    attrs["时间状态"] = "已过期"
                elif remaining <= 7:
                    attrs["时间状态"] = "即将过期"
                else:
                    attrs["时间状态"] = "有效"
            except (ValueError, TypeError):
                attrs["时间状态"] = "未知"
        return attrs

    def async_added_to_hass(self) -> None:
        """Run when entity is added to HA."""
        # 监听 config entry 更新，当 auth_status 等数据变化时刷新 sensor 状态
        self.async_on_remove(
            self._entry.add_update_listener(self._on_entry_updated)
        )

    async def _on_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Handle config entry data updates."""
        self._entry = entry
        self.async_schedule_update_ha_state()

    def async_reset_token(self, hass: HomeAssistant) -> None:
        """Reset token validity timestamp."""
        now_iso = datetime.now(timezone.utc).isoformat()
        new_data = dict(self._entry.data)
        new_data["token_added_at"] = now_iso
        hass.config_entries.async_update_entry(self._entry, data=new_data)
        LOGGER.info("Token 有效期已重置: %s", now_iso)
        # 触发传感器更新
        self.async_schedule_update_ha_state()
