"""Config flow for bjwater integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from .bj_water import BJWater, InvalidData

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER
from requests import RequestException


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("userCode"): str
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    session = async_get_clientsession(hass)
    userCode = data["userCode"]
    if userCode.isdigit():
        api = BJWater(session, userCode)
        try:
            await api.get_bill_cycle_range()
        except InvalidData as exc:
            LOGGER.error(exc)
            raise InvalidAuth
        except RequestException:
            raise CannotConnect
    else:
        raise InvalidFormat
    # Return info that you want to store in the config entry.
    return {"title": "水表户号: " + data["userCode"]}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for bjwater."""

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


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidFormat(HomeAssistantError):
    """Error to indicate there is invalid format."""
