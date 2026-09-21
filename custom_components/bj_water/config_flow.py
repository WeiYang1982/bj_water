"""Config flow for bj_water integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .bj_water import BJWater, InvalidData
from .const import DOMAIN, LOGGER
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from requests import RequestException


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("userCode"): str,
        vol.Required("token"): str,
        vol.Required("secretKeyEnc"): str,
        vol.Required("md5SaltEnc"): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    user_code = data["userCode"]
    token = data["token"]
    secret_key_enc = data["secretKeyEnc"]
    md5_salt_enc = data["md5SaltEnc"]
    if user_code.isdigit():
        api = BJWater(async_get_clientsession(hass), user_code, token, secret_key_enc, md5_salt_enc)
        try:
            await api.get_bill_cycle_range()
        except InvalidData as exc:
            LOGGER.error(str(exc))
            raise InvalidAuth from exc
        except RequestException:
            raise CannotConnect from exc
    else:
        raise InvalidFormat
    # Return info that you want to store in the config entry.
    return {"title": f"水表户号: {user_code}"}


async def async_added_to_hass(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """When entity is added to hass."""
    # 如果 entry 中没有 token_added_at，初始化它
    if "token_added_at" not in entry.data:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        # 创建新 entry 数据副本，添加 token_added_at
        new_data = dict(entry.data)
        new_data["token_added_at"] = now_iso
        hass.config_entries.async_update_entry(entry, data=new_data)
        LOGGER.info("初始化 token_added_at: %s", now_iso)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for bj_water."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if len(entries) > 0:
                for entity in entries:
                    user_code = entity.data["userCode"]
                    if user_input["userCode"] == user_code:
                        return self.async_abort(reason="already_configured")
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except InvalidFormat:
                errors["base"] = "invalid_format"
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def async_step_reauth(self, user_input=None):
        """Handle reauthentication request."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Handle reauthentication form."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            # 保持原有的 userCode，更新 token和加密参数
            new_data = {
                "userCode": entry.data["userCode"],
                "token": user_input["token"],
                "secretKeyEnc": user_input["secretKeyEnc"],
                "md5SaltEnc": user_input["md5SaltEnc"],
            }
            try:
                await validate_input(self.hass, new_data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # 更新 entry 数据
                from datetime import datetime, timezone
                new_data = {
                    "userCode": entry.data["userCode"],
                    "token": user_input["token"],
                    "secretKeyEnc": user_input["secretKeyEnc"],
                    "md5SaltEnc": user_input["md5SaltEnc"],
                    "token_added_at": datetime.now(timezone.utc).isoformat(),
                }
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                LOGGER.info("Token 重新认证成功，重置有效期")
                # 触发重新加载
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required("token", default=entry.data.get("token", "")): str,
                vol.Required("secretKeyEnc", default=entry.data.get("secretKeyEnc", "")): str,
                vol.Required("md5SaltEnc", default=entry.data.get("md5SaltEnc", "")): str,
            }),
            errors=errors,
            description_text="Token 或加密参数已失效，请重新输入登录 Token 和加密密钥",
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidFormat(HomeAssistantError):
    """Error to indicate there is invalid format."""
