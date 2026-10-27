"""Config flow for bj_water integration."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bj_water import (
    BJWater,
    InvalidData,
    LoginError,
    LoginResult,
    async_login_by_phone_code,
    async_send_login_code,
    is_valid_phone,
    normalize_phone,
)
from .const import DOMAIN, LOGGER


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any], check_binding: bool = True
) -> dict[str, Any]:
    """
    确认登录态有效、户号在账号绑定列表里。

    只调用账号级接口（用户信息 / 绑定户号列表），不拉账单、用水数据，
    账单数据留给 DataUpdateCoordinator 在集成加载后再获取。

    check_binding=False 用于重新认证：沿用 entry 里已有的户号，
    不再校验该户号是否还在绑定列表里。
    """
    user_code = data["userCode"]
    if not user_code.isdigit():
        raise InvalidFormat

    session = async_get_clientsession(hass)
    api = BJWater(session, user_code, data["token"], data["secretKeyEnc"], data["md5SaltEnc"])

    auth_ok = False
    try:
        await api.get_user_info()
        auth_ok = True
    except ConfigEntryAuthFailed as exc:
        LOGGER.error("验证登录态失败: %s", exc)
        raise InvalidAuth from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("验证登录态时网络异常: %s", exc)
        raise CannotConnect from exc
    except InvalidData as exc:
        # 用户信息接口返回业务错误时，以绑定户号列表接口的结果为准
        LOGGER.warning("用户信息接口返回业务错误: %s", exc)

    accounts: list[dict[str, Any]] | None = None
    if check_binding or not auth_ok:
        try:
            accounts = await api.get_bound_accounts()
        except ConfigEntryAuthFailed as exc:
            LOGGER.error("验证登录态失败: %s", exc)
            raise InvalidAuth from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.error("验证户号时网络异常: %s", exc)
            raise CannotConnect from exc
        except InvalidData as exc:
            LOGGER.warning("获取绑定户号列表失败，跳过户号校验: %s", exc)
            accounts = []

    if not auth_ok and not accounts:
        # 两个账号级接口都不可用，说明登录态确实有问题
        raise InvalidAuth("用户信息与绑定户号接口均返回业务错误")

    codes = {item["code"] for item in accounts or []}
    if check_binding and codes and user_code not in codes:
        LOGGER.error("户号 %s 不在账号绑定列表 %s 中", user_code, sorted(codes))
        raise InvalidUserCode(f"户号 {user_code} 不在账号绑定列表中")

    return {"title": f"水表户号: {user_code}"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """通过手机号 + 短信验证码登录完成配置（与 App 的登录方式一致）。"""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._phone: str = ""
        self._login: LoginResult | None = None
        self._user_code_hint: str = ""
        self._accounts: list[dict[str, Any]] = []
        self._accounts_loaded = False
        self._meter_options: list[dict[str, Any]] = []

    @property
    def _reauth_entry(self) -> ConfigEntry | None:
        """当前流程是否是重新认证，返回对应的 entry。"""
        if self.source != config_entries.SOURCE_REAUTH:
            return None
        return self._get_reauth_entry()

    # ---------------------------------------------------------------- 第一步：手机号
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """输入手机号并请求短信验证码。"""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if not self._phone and entry is not None:
            # 重新认证时用配置里保存的手机号做默认值
            self._phone = entry.data.get("phone", "")

        if user_input is not None:
            phone = normalize_phone(user_input["phone"])
            errors = await self._async_request_code(phone)
            if not errors:
                return await self.async_step_sms_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("phone", default=self._phone): str}),
            errors=errors,
        )

    # ---------------------------------------------------------------- 第二步：验证码
    async def async_step_sms_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """输入短信验证码（新配置时还可填水表户号）并完成登录。"""
        errors: dict[str, str] = {}
        entry = self._reauth_entry

        if user_input is not None:
            user_code = None if entry is not None else user_input.get("userCode", "")
            typed_user_code = str(user_code or "").strip()
            if typed_user_code:
                self._user_code_hint = typed_user_code

            data, errors, need_meter = await self._async_login_and_validate(
                user_input.get("phoneCode"), user_code
            )
            if need_meter:
                # 账号下绑定了多个水表，下一步让用户挑一个
                return await self.async_step_select_meter()
            if not errors:
                if entry is not None:
                    return self.async_update_reload_and_abort(
                        entry, data_updates=data, reason="reauth_successful"
                    )
                return self.async_create_entry(title=f"水表户号: {data['userCode']}", data=data)
            if errors.get("base") == "already_configured":
                return self.async_abort(reason="already_configured")

        return self.async_show_form(
            step_id="sms_code",
            data_schema=self._sms_code_schema(),
            errors=errors,
        )

    # ---------------------------------------------------------------- 选择水表
    async def async_step_select_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """登录成功后列出账号下所有户号，由用户选择为哪个户号创建集成。"""
        errors: dict[str, str] = {}
        if not self._meter_options:
            return await self.async_step_sms_code()

        if user_input is not None:
            data, errors, _ = await self._async_login_and_validate(None, user_input.get("userCode"))
            if not errors:
                return self.async_create_entry(title=f"水表户号: {data['userCode']}", data=data)
            if errors.get("base") == "already_configured":
                return self.async_abort(reason="already_configured")

        options: dict[str, str] = {}
        for item in self._meter_options:
            code = item["code"]
            options[code] = f"{code} · {item['address']}" if item.get("address") else code
        default = next(
            (item["code"] for item in self._meter_options if item.get("is_default")),
            self._meter_options[0]["code"],
        )
        return self.async_show_form(
            step_id="select_meter",
            data_schema=vol.Schema({vol.Required("userCode", default=default): vol.In(options)}),
            errors=errors,
            description_placeholders={"count": str(len(self._meter_options))},
        )

    # ---------------------------------------------------------------- 重新认证
    async def async_step_reauth(self, entry_data: dict[str, Any] | None = None) -> ConfigFlowResult:
        """登录态失效后重新走一遍手机号登录。"""
        return await self.async_step_user()

    # ---------------------------------------------------------------- 内部方法
    def _sms_code_schema(self) -> vol.Schema:
        """新配置时额外提供户号输入框（留空则自动使用账号绑定的水表）。"""
        fields: dict[Any, Any] = {vol.Required("phoneCode"): str}
        if self._reauth_entry is None:
            fields[vol.Optional("userCode", description={"suggested_value": self._user_code_hint})] = str
        return vol.Schema(fields)

    async def _async_request_code(self, phone: str) -> dict[str, str]:
        """请求短信验证码，返回 HA 表单错误字典。"""
        if not is_valid_phone(phone):
            return {"base": "invalid_phone"}
        try:
            await async_send_login_code(async_get_clientsession(self.hass), phone)
        except LoginError as exc:
            # 常见原因：手机号未注册、发送过于频繁
            LOGGER.warning("发送短信验证码失败: %s", exc)
            return {"base": "code_send_failed"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("发送短信验证码网络异常: %s", exc)
            return {"base": "cannot_connect"}

        self._phone = phone
        self._login = None
        self._accounts = []
        self._accounts_loaded = False
        self._meter_options = []
        return {}

    async def _async_login_and_validate(
        self, code: Any, user_code: str | None
    ) -> tuple[dict[str, Any] | None, dict[str, str], bool]:
        """
        用验证码登录，确定户号并验证。

        返回 (entry data, errors, need_meter)。need_meter 为 True 表示账号绑定了
        多个水表，需要用户在下一步里选择具体户号。

        验证码只会提交一次：后续因为户号错误或选择水表再次提交时复用已拿到的
        token，不需要重新发送短信验证码。
        """
        code = str(code or "").strip()
        if self._login is None:
            if not re.fullmatch(r"\d{4,6}", code):
                return None, {"base": "invalid_code"}, False
            try:
                self._login = await async_login_by_phone_code(
                    async_get_clientsession(self.hass), self._phone, code
                )
            except LoginError as exc:
                LOGGER.warning("手机号登录失败: %s", exc)
                return None, {"base": "invalid_code"}, False
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                LOGGER.warning("手机号登录网络异常: %s", exc)
                return None, {"base": "cannot_connect"}, False

        login = self._login
        entry = self._reauth_entry

        if entry is not None:
            # 重新认证：沿用 entry 里保存的户号，不需要再发现或选择
            user_code = str(entry.data.get("userCode", "")).strip()
        else:
            accounts = await self._async_bound_accounts(login)
            user_code = str(user_code or "").strip() or self._user_code_hint
            if user_code:
                if accounts and user_code not in {item["code"] for item in accounts}:
                    LOGGER.warning("户号 %s 不在账号绑定列表 %s 中", user_code, [i["code"] for i in accounts])
                    return None, {"base": "user_code_not_bound"}, False
            elif accounts:
                # 拿到绑定户号就一律交给下一步 select_meter，由用户确认/选择
                self._meter_options = accounts
                LOGGER.info("登录成功，由用户从 %d 个户号中选择: %s", len(accounts), [i["code"] for i in accounts])
                return None, {}, True
            else:
                user_code = await self._async_discover_user_code(login)
                if user_code:
                    LOGGER.info("绑定列表为空，用账号默认户号让用户确认: %s", user_code)
                    self._meter_options = [{"code": user_code, "address": "", "is_default": True}]
                    return None, {}, True
            if not user_code:
                # 账号下既没有绑定列表、也没有默认户号，只能手填
                return None, {"base": "user_code_required"}, False
            if not user_code.isdigit():
                return None, {"base": "invalid_user_code"}, False

        if entry is None and any(
            item.data.get("userCode") == user_code for item in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return None, {"base": "already_configured"}, False

        data: dict[str, Any] = {
            "userCode": user_code,
            "phone": self._phone,
            "token": login.token,
            "secretKeyEnc": login.secret_key_enc,
            "md5SaltEnc": login.md5_salt_enc,
            "token_added_at": datetime.now(timezone.utc).isoformat(),
        }
        if login.expires_at is not None:
            data["token_expires_at"] = login.expires_at.isoformat()

        try:
            await validate_input(self.hass, data, check_binding=entry is None)
        except InvalidAuth as exc:
            LOGGER.warning("登录态校验失败: %s", exc)
            return None, {"base": "invalid_auth"}, False
        except InvalidUserCode as exc:
            LOGGER.warning("户号校验失败: %s", exc)
            return None, {"base": "invalid_user_code"}, False
        except CannotConnect as exc:
            LOGGER.warning("校验时无法连接: %s", exc)
            return None, {"base": "cannot_connect"}, False

        return data, {}, False

    async def _async_bound_accounts(self, login: LoginResult) -> list[dict[str, Any]]:
        """登录后读取账号绑定的水表列表（一个流程里只查一次），失败返回空列表。"""
        if self._accounts_loaded:
            return self._accounts
        self._accounts_loaded = True

        api = BJWater(
            async_get_clientsession(self.hass),
            "",
            login.token,
            login.secret_key_enc,
            login.md5_salt_enc,
        )
        try:
            self._accounts = await api.get_bound_accounts()
        except (InvalidData, ConfigEntryAuthFailed, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("获取账号绑定水表列表失败，改为手动填写户号: %s", exc)
            self._accounts = []
        else:
            LOGGER.info("账号绑定的水表 %d 个: %s", len(self._accounts), self._accounts)
        return self._accounts

    async def _async_discover_user_code(self, login: LoginResult) -> str:
        """登录后尝试读取账号的默认户号，失败返回空字符串。"""
        api = BJWater(
            async_get_clientsession(self.hass),
            "",
            login.token,
            login.secret_key_enc,
            login.md5_salt_enc,
        )
        try:
            user_code = await api.get_default_user_code()
        except (InvalidData, ConfigEntryAuthFailed, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("读取账号默认户号失败（可手动填写户号）: %s", exc)
            return ""
        return user_code if user_code.isdigit() else ""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidFormat(HomeAssistantError):
    """Error to indicate there is invalid format."""


class InvalidUserCode(HomeAssistantError):
    """Error to indicate the water meter user code is invalid."""
