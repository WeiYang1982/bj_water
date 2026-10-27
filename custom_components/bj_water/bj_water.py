import asyncio
import base64
import hashlib
import json
import random
import re
import string
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from .const import LOGGER
from homeassistant.exceptions import ConfigEntryAuthFailed

SERVICE_HOST = "https://www.bjwatergroupkf.com.cn"

# ============ 加密配置 ============
_DECRYPT_KEY = b"cooptecxxxxxxxxx"  # 16字节，用于解密登录响应中的 secretKey / md5Salt
_LOGIN_PREFIX = "cooptec"           # 解密后明文末尾固定追加的盐，需要去掉

# ============ 手机号验证码登录参数（逆向 com.watergroup.marketing v1.12 得到） ============
# EncryptUtils.mobile(phone) = Base64(AES-128-ECB-PKCS5Padding(phone))
# key = MD5("cooptec1a2b3c!@#$").hexdigest().upper()[:16] == "7E40A9A4826FF52E"
_PHONE_SALT = "cooptec1a2b3c!@#$"
_PHONE_KEY = hashlib.md5(_PHONE_SALT.encode("utf-8")).hexdigest().upper()[:16].encode("utf-8")

APP_VERSION = "1.30"

# 发验证码 / 登录这两个接口不签名、不带 token，只需要 App 的 UA 和设备信息
_LOGIN_HEADERS = {
    "Host": "www.bjwatergroupkf.com.cn",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Language": "zh-Hans-CN;q=1",
    "User-Agent": "bei jing zi lai shui/1.30 (iPad; iOS 27.0; Scale/2.00)",
    "token": "",
}
_LOGIN_TIMEOUT = 15


class InvalidData(Exception):
    pass


class LoginError(Exception):
    """手机号登录相关的业务错误（验证码错误、发送过于频繁等）。"""


def normalize_phone(phone: str) -> str:
    """去掉手机号里的空格/横线（App 输入框也会做同样处理）。"""
    return re.sub(r"[\s\-]", "", phone or "")


def is_valid_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"1\d{10}", normalize_phone(phone)))


def encrypt_phone(phone: str) -> str:
    """明文手机号 -> Base64(AES-128-ECB-PKCS5(phone))，与 App 的 EncryptUtils.mobile() 一致。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    cipher = AES.new(_PHONE_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(normalize_phone(phone).encode("utf-8"), AES.block_size))).decode()


def parse_token_expiration(token: str) -> datetime | None:
    """从 JWT 的 exp 声明解析 token 过期时间，失败返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except (IndexError, KeyError, TypeError, ValueError):
        return None


@dataclass
class LoginResult:
    """手机号登录成功后拿到的一组凭据。"""

    token: str
    secret_key_enc: str
    md5_salt_enc: str
    user_info: dict
    expires_at: datetime | None = None


async def async_send_login_code(session, phone: str) -> None:
    """
    请求发送短信验证码。

    App 的真实请求: GET /api/member/bizUsers/getNewLoginPhoneCode?phone=<AES密文>&timestamp=<毫秒>
    """
    url = f"{SERVICE_HOST}/api/member/bizUsers/getNewLoginPhoneCode"
    params = {
        "nonce": uuid.uuid4().hex,
        "phone": encrypt_phone(phone),
        "timestamp": str(int(time.time() * 1000)),
    }
    async with session.get(url, params=params, headers=_LOGIN_HEADERS, timeout=_LOGIN_TIMEOUT) as resp:
        text = await resp.text()
        status = resp.status

    if status != 200:
        raise LoginError(f"发送验证码失败: HTTP {status}")
    try:
        body = json.loads(text)
    except ValueError as err:
        raise LoginError("发送验证码失败: 响应不是合法的 JSON") from err
    if body.get("code") != 0:
        raise LoginError(f"发送验证码失败: {body.get('msg') or body.get('code')}")
    LOGGER.debug("短信验证码已发送: %s", normalize_phone(phone))


async def async_login_by_phone_code(session, phone: str, code: str) -> LoginResult:
    """
    手机号 + 短信验证码登录，换取 token / secretKey / md5Salt。

    App 的真实请求: POST /api/member/bizUsers/loginByPhoneCode（phone 为明文，其余为设备信息）
    """
    url = f"{SERVICE_HOST}/api/member/bizUsers/loginByPhoneCode"
    payload = {
        "phone": normalize_phone(phone),
        "osType": "ios",
        "appVersion": APP_VERSION,
        "osTypeVersion": "17.0",
        "model": "iPad8,6",
        "phoneCode": str(code).strip(),
        "brand": "apple",
        "mobileToken": uuid.uuid4().hex,
        "timestamp": str(int(time.time() * 1000)),
        "nonce": uuid.uuid4().hex,
    }
    async with session.post(url, json=payload, headers=_LOGIN_HEADERS, timeout=_LOGIN_TIMEOUT) as resp:
        text = await resp.text()
        status = resp.status

    if status != 200:
        raise LoginError(f"登录失败: HTTP {status}")
    try:
        body = json.loads(text)
    except ValueError as err:
        raise LoginError("登录失败: 响应不是合法的 JSON") from err
    if body.get("code") != 0:
        raise LoginError(f"登录失败: {body.get('msg') or body.get('code')}")

    data = body.get("data") or {}
    token = data.get("token") or ""
    secret_key_enc = data.get("secretKey") or ""
    md5_salt_enc = data.get("md5Salt") or ""
    if not (token and secret_key_enc and md5_salt_enc):
        raise LoginError("登录失败: 响应中缺少 token / secretKey / md5Salt")

    LOGGER.debug("手机号登录成功: %s", normalize_phone(phone))
    return LoginResult(
        token=token,
        secret_key_enc=secret_key_enc,
        md5_salt_enc=md5_salt_enc,
        user_info=data.get("userInfo") or {},
        expires_at=parse_token_expiration(token),
    )


class BJWater:
    def __init__(self, session, user_code, token=None, secret_key_enc=None, md5_salt_enc=None) -> None:
        self._session = session
        self.user_code = user_code
        self.token = token
        self.secret_key_enc = secret_key_enc
        self.md5_salt_enc = md5_salt_enc
        self.bill_cycle = {}
        self.info = {"cycle": {}, "user_code": "", "meter_value": []}
        self._secret_key = None
        self._md5_salt = None

    # ---- 加密/解密工具方法 ----

    def _aes_ecb_decrypt(self, base64_str: str, key: bytes) -> str:
        """AES-ECB 解密 + PKCS7 padding去除"""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        import base64
        import binascii

        try:
            decoded = base64.b64decode(base64_str)
        except (binascii.Error, ValueError) as e:
            raise InvalidData(f"base64 解码失败: {e}")
        try:
            cipher = AES.new(key, AES.MODE_ECB)
            plaintext = unpad(cipher.decrypt(decoded), AES.block_size)
            return plaintext.decode("utf-8")
        except Exception as e:
            raise InvalidData(f"AES 解密失败: {e}")

    def _decrypt_credentials(self) -> tuple[str, str]:
        """解密登录响应中的 secretKey 和 md5Salt"""
        if self.secret_key_enc is None or self.md5_salt_enc is None:
            raise InvalidData("未配置 SECRET_KEY_ENC 或 MD5_SALT_ENC")
        raw_secret = self._aes_ecb_decrypt(self.secret_key_enc, _DECRYPT_KEY)
        raw_salt = self._aes_ecb_decrypt(self.md5_salt_enc, _DECRYPT_KEY)
        secret_key = raw_secret.replace(_LOGIN_PREFIX, "", 1)
        md5_salt = raw_salt.replace(_LOGIN_PREFIX, "", 1)
        return secret_key, md5_salt

    def _decrypt_response(self, data_enc: str) -> str:
        """解密普通业务接口的响应数据"""
        if self._secret_key is None:
            self._secret_key, self._md5_salt = self._decrypt_credentials()
        key = self._secret_key[:16].encode("utf-8")
        try:
            decrypted = self._aes_ecb_decrypt(data_enc, key)
            decrypted = decrypted.replace(self._secret_key[:16], "", 1)
            return decrypted
        except Exception as e:
            LOGGER.error(f"解密失败: {e}")
            return data_enc

    def _generate_sign(self, params: dict) -> str:
        """计算签名"""
        items = [f"{k}={v}&" for k, v in params.items()]
        items.sort()
        joined = "".join(items)
        truncated = joined[:joined.rfind("&")]
        if self._md5_salt is None:
            self._secret_key, self._md5_salt = self._decrypt_credentials()
        sign_str = truncated + "&secretKey=" + self._secret_key + self._md5_salt
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _random_string(min_len: int = 10, max_len: int = 32) -> str:
        chars = string.digits + string.ascii_lowercase + string.ascii_uppercase
        length = random.randint(min_len, max_len)
        return "".join(random.choice(chars) for _ in range(length))

    # ---- HTTP 请求 ----

    async def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """调用接口并自动解密响应"""
        if self._secret_key is None:
            self._secret_key, self._md5_salt = self._decrypt_credentials()

        # 组装参与签名的参数：业务参数 + timestamp + nonce（不含 sign / noEncrypt）
        all_params = {**(params or {})}
        all_params["timestamp"] = str(int(time.time() * 1000))
        all_params["nonce"] = self._random_string()

        # 签名
        all_params["sign"] = self._generate_sign(all_params)
        # noEncrypt 仅随请求发送，不参与签名
        all_params["noEncrypt"] = ""

        headers = {
            "token": self.token or "",
            "Host": "www.bjwatergroupkf.com.cn",
        }

        url = f"{SERVICE_HOST}/api/{endpoint}"
        LOGGER.info(f"_request {method} {url} params={all_params} headers={headers}")

        if method.upper() == "GET":
            response = await self._session.get(url=url, params=all_params, headers=headers, timeout=10)
        else:
            response = await self._session.post(url=url, json=all_params, headers=headers, timeout=10)

        if response.status in (401, 403):
            LOGGER.error(f"_request res state code: {response.status}")
            raise ConfigEntryAuthFailed(f"接口返回 HTTP {response.status}，登录态已失效")

        if response.status != 200:
            LOGGER.error(f"_request res state code: {response.status}")
            raise InvalidData(f"_request response status_code = {response.status}")

        result = json.loads(await response.read())
        LOGGER.info(f"_request response: {result}")

        # 认证相关错误：抛出 ConfigEntryAuthFailed，触发 HA 自动重新认证流程
        message = str(result.get("msg") or "")
        if result.get("code") in (1001, 1206) or (result.get("code") == 1003 and "token" in message.lower()):
            raise ConfigEntryAuthFailed(f"认证失败: {message or '未知错误'}，请重新通过手机号登录")

        if result.get("code") != 0:
            LOGGER.error(f"API错误: code={result.get('code')}, msg={result.get('msg')}")
            raise InvalidData(f"API错误: {result.get('msg', '未知错误')}")

        # 如果成功且 data 是加密的字符串，解密
        if result.get("code") == 0 and result.get("data") and isinstance(result["data"], str):
            decrypted_data = self._decrypt_response(result["data"])
            try:
                result["data"] = json.loads(decrypted_data)
            except (json.JSONDecodeError, TypeError):
                result["data"] = decrypted_data

        return result

    # ---- 业务方法 ----

    async def get_user_info(self) -> dict:
        """
        获取用户信息（昵称、手机号、实名状态等）。

        和 App 一样不带任何业务参数，因此只用于确认登录态有效（token 可用），
        不依赖水表户号。认证失败会抛出 ConfigEntryAuthFailed，触发 HA 自动重新认证。
        """
        result = await self._request("GET", "member/userCenter/getUserInfo")
        data = result.get("data")
        if isinstance(data, dict):
            return data
        return {}

    async def get_bound_accounts(self) -> list[dict]:
        """
        获取当前账号下绑定的水表列表。

        GET /api/member/userCenter/getBindAccountList，App 在“选择户号”页面调用。
        每项形如 {code, address, isDefault, id, tag, sortNo, userId}，其中 code 就是户号。
        """
        result = await self._request("GET", "member/userCenter/getBindAccountList")
        data = result.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = []
        if not isinstance(data, list):
            return []

        accounts: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            accounts.append(
                {
                    "code": code,
                    "address": str(item.get("address") or "").strip(),
                    "is_default": str(item.get("isDefault") or "0") == "1",
                }
            )
        return accounts

    async def get_default_user_code(self) -> str:
        """
        获取当前登录账号默认绑定的水表户号。

        App 启动后调用 GET /api/member/userCenter/getLoginOrderId，
        把返回的明文 data 作为默认 userCode。账号没设置默认户号时 data 可能为
        空字符串或 "null"，此时返回空字符串，由 get_bound_accounts 兜底。
        """
        result = await self._request("GET", "member/userCenter/getLoginOrderId")
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("userCode", "code", "orderId"):
                if data.get(key):
                    return str(data[key]).strip()
            return ""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("code"):
                    return str(item["code"]).strip()
            return ""
        value = str(data or "").strip()
        return "" if value.lower() in ("null", "none", "undefined") else value

    async def get_bill_cycle_range(self):
        """
        获取账单周期
        :return: dict {cycle_date: bill_month_str}
        """
        LOGGER.info("get_bill_cycle_range user code: " + str(self.user_code))
        LOGGER.info(f"get_bill_cycle_range token: {self.token[:20] if self.token else 'EMPTY'}...")
        result = await self._request("GET", "member/bizMyWater/getMonthsAndYears", {"userCode": self.user_code})

        if "months" in result["data"].keys() and len(result["data"]["months"]) > 0:
            # 重置账期数据，避免多次刷新时累积
            self.bill_cycle = {}
            self.info["cycle"] = {}
            bill_list = sorted(result["data"]["months"], reverse=True)[:6]  # 倒序排列后取最近6个账单周期
            for bill in bill_list:
                cycle_date = datetime.strptime(bill, "%Y年%m月").date().strftime("%Y-%m")
                self.bill_cycle[cycle_date] = bill
                self.info["cycle"].update(
                    {
                        cycle_date: {
                            "fee": {
                                "pay": 0,
                                "date": cycle_date,
                                "amount": 0,
                                "szyf": 0,
                                "wsf": 0,
                                "sf": 0,
                            }
                        }
                    }
                )
            self.info["user_code"] = self.user_code
        else:
            raise InvalidData(f"未查到账单周期,请检查户号: {self.user_code}!")

        LOGGER.info("get_bill_cycle_range end " + str(self.info))
        return self.bill_cycle

    async def get_payment_bill(self):
        """
        获取缴费账单
        amount: 当前周期总费用
        date: 缴费时间
        szyf: 水资源费改税
        wsf: 污水处理费
        sf: 水费
        :return:
        """
        result = await self._request("GET", "member/bizMyWater/paymentRecord", {"userCode": self.user_code})
        bill_list = result["data"]
        if len(bill_list) == 0:
            raise InvalidData("未查询到缴费记录,请检查水表户号!")

        index = 0
        for bill in bill_list:
            cycle_date = datetime.strptime(bill["billDate"], "%Y年%m月").date().strftime("%Y-%m")
            if cycle_date in self.bill_cycle:
                amount_detail = {
                    "index": index,
                    "fee": {
                        "pay": 1,
                        "date": datetime.strptime(bill["date"], "%Y.%m.%d").date().strftime("%Y-%m-%d"),
                        "amount": bill["amount"],
                        "szyf": bill["szyf"],
                        "wsf": bill["wsf"],
                        "sf": bill["sf"],
                    },
                }
                self.info["cycle"][cycle_date].update(amount_detail)
            index += 1
        LOGGER.info("get_payment_bill end " + str(self.info))

    async def get_monthly_bill(self, bill_cycle, index):
        """
        获取单个月份的账单详情
        :param bill_cycle: 账单周期 如 2023-06
        :param index: 账单索引
        :return:
        """
        result = await self._request(
            "GET",
            "member/bizMyWater/getMonthlyBill",
            {"userCode": self.user_code, "billDate": self.bill_cycle[bill_cycle]},
        )

        detail_data = result["data"]
        if detail_data.get("endValue", "") == "":
            raise InvalidData("未查询到账单详情,请检查账单周期是否错误!")

        result_info = {"date": bill_cycle, "usage": detail_data["total"]}

        if self.info["cycle"][bill_cycle]["fee"]["pay"] == 0:
            amount_detail = {
                "index": index,
                "fee": {
                    "pay": 0,
                    "date": bill_cycle,
                    "amount": detail_data["amount"],
                    "szyf": detail_data["taxFee"]["amount"],
                    "wsf": detail_data["waterborneFee"]["amount"],
                    "sf": detail_data["firstStep"]["amount"],
                },
                "meter": {
                    "usage": detail_data["total"],
                    "value": [detail_data["endValue"].split("/")],
                },
            }
            self.info["cycle"][bill_cycle].update(amount_detail)

        self.info["cycle"][bill_cycle].update(
            {
                "meter": {
                    "usage": detail_data["total"],
                    "value": [detail_data["endValue"].split("/")],
                }
            }
        )
        # 累计用水量：使用当期总用量(detail_data["total"]，跨阶梯)，
        # 而非 grandTotal（可能仅为第一阶梯用量，上限180m³）
        if "total_usage" not in self.info.keys():
            self.info["total_usage"] = 0
        self.info["total_usage"] += int(detail_data["total"])
        meter_values = detail_data["endValue"].split("/")
        for i in range(len(meter_values)):
            if len(self.info["meter_value"]) <= i:
                self.info["meter_value"].append({i: int(meter_values[i])})
            elif i < len(self.info["meter_value"]):
                existing_value = self.info["meter_value"][i].get(i, None)
                if existing_value is None or existing_value < int(meter_values[i]):
                    self.info["meter_value"][i][i] = int(meter_values[i])
                    self.info.update({"first_step_left": int(detail_data["stepLeft"]["fist"])})
        self.info.update({"first_step_price": float(detail_data["firstStep"]["price"])})
        self.info.update({"wastwater_treatment_price": float(detail_data["waterborneFee"]["price"])})
        self.info.update({"water_tax": float(detail_data["taxFee"]["price"])})
        self.info.update({"second_step_left": int(detail_data["stepLeft"]["second"])})
        self.info.update(
            {
                "total_cost": self.info["water_tax"] + self.info["first_step_price"] + self.info["wastwater_treatment_price"]
            }
        )
        LOGGER.info("周期使用量: %s" % str(result_info))
        LOGGER.info(self.info)
        return self.info

    async def fetch_data(self):
        # 首先验证用户信息，确认认证有效
        # 如果此步失败会抛出异常，后续逻辑不会执行，避免无意义的 API 调用
        await self.get_user_info()
        await self.get_bill_cycle_range()
        await self.get_payment_bill()
        # 重置累计用水量，避免每次刷新时重复累加
        self.info["total_usage"] = 0
        index = 0
        for bill_date in self.bill_cycle:
            await self.get_monthly_bill(bill_date, index)
            index += 1
        # 当期水费：取最近（第一个）账期的总费用，用于能源面板统计
        if self.info["cycle"]:
            first_cycle_date = next(iter(self.info["cycle"]))
            latest_fee = self.info["cycle"][first_cycle_date].get("fee", {})
            self.info["latest_bill_amount"] = latest_fee.get("amount")
        # 累计水费：仅统计今年（当前自然年）的账期费用之和
        current_year = str(datetime.now().year)
        self.info["total_cost_accumulated"] = sum(
            cycle_data.get("fee", {}).get("amount", 0)
            for cycle_date, cycle_data in self.info["cycle"].items()
            if cycle_date.startswith(current_year)
        )
        return self.info

    async def reset_token_timestamp(self, entry, hass):
        """重置 token 有效期时间戳"""
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        new_data = dict(entry.data)
        new_data["token_added_at"] = now_iso
        hass.config_entries.async_update_entry(entry, data=new_data)
        LOGGER.info("重置 token 有效期时间戳: %s", now_iso)
