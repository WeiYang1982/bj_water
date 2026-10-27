"""The 北京水费 integration."""
from __future__ import annotations

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.device_registry import async_get as dr_async_get
from homeassistant.components.sensor import SensorEntity

from .bj_water import BJWater, InvalidData
from .const import DOMAIN, LOGGER, UPDATE_INTERVAL, TOKEN_VALIDITY_DAYS
from datetime import datetime, timedelta, timezone


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

    # 在设备注册表中注册一个设备，使其关联到本集成（config_entry），
    # 这样实体才能被"按集成"筛选到。
    device_registry = dr_async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, user_code)},
        name=f"北京水费 {user_code}",
        manufacturer="bj_water",
    )

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

    def __init__(self, entry, user_code):
        """Initialize the sensor."""
        self._entry = entry
        # 使用纯英文名称，确保 entity_id 稳定（中文会被转拼音，可能变化）
        self._attr_unique_id = f"{DOMAIN}.{user_code}_token_validity"
        self._attr_name = "Token Validity"
        self._attr_icon = "mdi:clock-alert"
        self._attr_native_unit_of_measurement = "days"
        self._attr_should_poll = False
        self._attr_device_info = {
            "identifiers": {(DOMAIN, user_code)},
            "name": f"北京水费 {user_code}",
            "manufacturer": "bj_water",
        }

    @property
    def state(self):
        """Return the remaining days or auth failure status."""
        # 如果认证失败，显示认证失败状态
        if self._entry.data.get("auth_status") == "failed":
            return "认证失败"
        expires_at = self._expires_at()
        if expires_at is None:
            return None
        return max((expires_at - datetime.now(timezone.utc)).days, 0)

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

        now = datetime.now(timezone.utc)
        added_at = self._added_at()
        expires_at = self._expires_at()

        if added_at is not None:
            attrs["添加时间"] = added_at.isoformat()
        if expires_at is not None:
            remaining = (expires_at - now).days
            attrs["过期时间"] = expires_at.isoformat()
            attrs["剩余天数"] = max(remaining, 0)
            if added_at is not None:
                attrs["已使用天数"] = (now - added_at).days
                attrs["总有效期"] = (expires_at - added_at).days
            if remaining <= 0:
                attrs["时间状态"] = "已过期"
            elif remaining <= 7:
                attrs["时间状态"] = "即将过期"
            else:
                attrs["时间状态"] = "有效"
        elif added_at is None:
            attrs["时间状态"] = "未知"
        return attrs

    def _added_at(self) -> datetime | None:
        """解析 token 的获取时间。"""
        raw = self._entry.data.get("token_added_at")
        if not raw:
            return None
        try:
            added_at = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        if added_at.tzinfo is None:
            added_at = added_at.replace(tzinfo=timezone.utc)
        return added_at

    def _expires_at(self) -> datetime | None:
        """
        token 过期时间。

        优先使用登录响应里 JWT 的 exp（token_expires_at），
        旧配置没有该字段时退回按 104 天有效期推算。
        """
        raw = self._entry.data.get("token_expires_at")
        if raw:
            try:
                expires_at = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                expires_at = None
            else:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return expires_at

        added_at = self._added_at()
        if added_at is not None:
            return added_at + timedelta(days=TOKEN_VALIDITY_DAYS)
        return None

    async def async_added_to_hass(self) -> None:
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
