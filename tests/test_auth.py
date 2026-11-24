"""Regression tests for account authentication and logging, using synthetic data."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.bj_water import _async_update_data
from custom_components.bj_water import bj_water as client
from custom_components.bj_water.config_flow import ConfigFlow


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock()
        self.api = client.BJWater(
            self.session, "example-meter", "example-token", "example-key", "example-salt"
        )

    async def test_incomplete_credentials_request_reauth_before_network_io(self):
        for field in ("token", "secret_key_enc", "md5_salt_enc"):
            for missing in (None, ""):
                with self.subTest(field=field, missing=missing):
                    with patch.object(self.api, field, missing), self.assertRaises(ConfigEntryAuthFailed):
                        await self.api.fetch_data()
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    async def test_refresh_requests_reauth_for_the_existing_entry(self):
        hass, entry = MagicMock(), MagicMock()
        entry.data = {"userCode": self.api.user_code}
        hass.config_entries.flow.async_progress_by_handler.return_value = []
        with (
            patch.object(self.api, "token", ""),
            patch("custom_components.bj_water.persistent_notification.async_create"),
            self.assertRaises(ConfigEntryAuthFailed),
        ):
            await _async_update_data(hass, entry, self.api)
        entry.async_start_reauth.assert_called_once_with(hass)
        hass.config_entries.async_update_entry.assert_called_once_with(
            entry, data={"userCode": self.api.user_code, "auth_status": "failed"}
        )
        self.session.get.assert_not_called()

    async def test_billing_request_keeps_private_values_out_of_logs(self):
        response = MagicMock(status=200)
        response.read = AsyncMock(return_value=json.dumps(
            {"code": 0, "data": {"months": ["2000年01月"], "address": "example-address"}}
        ).encode())
        self.session.get = AsyncMock(return_value=response)

        with (
            patch.object(self.api, "_decrypt_credentials", return_value=("example-key", "example-salt")),
            self.assertLogs(client.LOGGER, "DEBUG") as logs,
        ):
            cycles = await self.api.get_bill_cycle_range()

        self.assertEqual(cycles, {"2000-01": "2000年01月"})
        request = self.session.get.call_args.kwargs
        self.assertEqual(request["headers"]["token"], self.api.token)
        self.assertEqual(request["params"]["userCode"], self.api.user_code)
        for value in (self.api.token, self.api.user_code, request["params"]["sign"], "example-address", "2000-01"):
            self.assertNotIn(value, "\n".join(logs.output))

    async def test_rejected_session_keeps_the_server_message_out_of_errors(self):
        for status, code in ((401, 0), (403, 0), (200, 1001), (200, 1206), (200, 1003)):
            with self.subTest(status=status, code=code):
                response = MagicMock(status=status)
                response.read = AsyncMock(return_value=json.dumps(
                    {"code": code, "msg": "invalid token example-private-detail"}
                ).encode())
                self.session.get = AsyncMock(return_value=response)
                with (
                    patch.object(self.api, "_decrypt_credentials", return_value=("example-key", "example-salt")),
                    self.assertLogs(client.LOGGER, "DEBUG") as logs,
                    self.assertRaises(ConfigEntryAuthFailed) as error,
                ):
                    await self.api.get_user_info()
                self.assertNotIn("example-private-detail", str(error.exception))
                self.assertNotIn("example-private-detail", "\n".join(logs.output))

    async def test_login_logs_do_not_include_phone_or_account_details(self):
        phone = "examplephone"
        login_data = {"token": "example-token", "secretKey": "example-key", "md5Salt": "example-salt"}
        for method, body in (("get", {"code": 0}), ("post", {"code": 0, "data": login_data})):
            response = getattr(self.session, method).return_value.__aenter__.return_value
            response.status = 200
            response.text = AsyncMock(return_value=json.dumps(body))

        accounts = [{"code": "example-meter", "address": "example-address"}]
        flow = ConfigFlow()
        with (
            patch.object(client, "encrypt_phone", return_value="example-encrypted-phone"),
            patch("custom_components.bj_water.config_flow.async_get_clientsession", return_value=self.session),
            patch.object(client.BJWater, "get_bound_accounts", new_callable=AsyncMock, return_value=accounts),
            self.assertLogs(client.LOGGER, "DEBUG") as logs,
        ):
            await client.async_send_login_code(self.session, phone)
            login = await client.async_login_by_phone_code(self.session, phone, "example-code")
            self.assertEqual(await flow._async_bound_accounts(login), accounts)

        for value in (phone, "example-code", *login_data.values(), "example-meter", "example-address"):
            self.assertNotIn(value, "\n".join(logs.output))
