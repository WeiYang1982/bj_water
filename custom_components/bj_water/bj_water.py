import asyncio
import json
from datetime import datetime
from .const import LOGGER


SERVICE_HOST = "https://www.bjwatergroupkf.com.cn"


class InvalidData(Exception):
    pass


class BJWater:
    def __init__(self, session, user_code) -> None:
        self._session = session
        self.user_code = user_code
        self.bill_cycle = set()
        self.info = {"cycle": {}, "user_code": "", "meter_value": []}

    async def get_bill_cycle_range(self):
        """
        获取账单周期
        "data": {
            "months": [
                "2023年08月",
                "2022年02月"],
            "years": [2023,2022]}
        :return:
        """
        LOGGER.info("get_bill_cycle_range user code: " + str(self.user_code))
        bill_month_api = SERVICE_HOST + "/api/member/bizMyWater/getPcMonthsAndYears"
        response = await self._session.get(
            url=bill_month_api, params={"userCode": self.user_code}
        )
        if response.status == 200:
            json_body = json.loads(await response.read())
            LOGGER.info("get_bill_cycle_range response: " + str(json_body))
            if "months" in json_body["data"].keys() and len(json_body["data"]["months"]) > 0:
                bill_list = json_body["data"]["months"]
                year = datetime.now().year
                for bill in bill_list:
                    if str(year) in bill:
                        cycle_date = (
                            datetime.strptime(
                                bill, "%Y年%m月").date().strftime("%Y-%m-%d")
                        )
                        self.bill_cycle.add(cycle_date)
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
        else:
            LOGGER.error(f"get_monthly_bill res state code: {response.status}")
            raise InvalidData(
                f"get_bill_month_range response status_code: {response.status}"
            )
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
        payment_api = SERVICE_HOST + "/api/member/bizMyWater/pcPaymentRecord"
        params = {"userCode": self.user_code}
        response = await self._session.get(url=payment_api, params=params, timeout=10)
        if response.status == 200:
            json_body = json.loads(await response.read())
            LOGGER.info("get_payment_bill: " + str(json_body))
            bill_list = json_body["data"]
            if len(bill_list) == 0:
                raise InvalidData("未查询到缴费记录,请检查水表户号!")
            for bill in json_body["data"]:
                cycle_date = (
                    datetime.strptime(bill["billDate"], "%Y年%m月")
                    .date()
                    .strftime("%Y-%m-%d")
                )
                if cycle_date in self.bill_cycle:
                    amount_detail = {
                        "fee": {
                            "pay": 1,
                            "date": datetime.strptime(bill["date"], "%Y.%m.%d")
                            .date()
                            .strftime("%Y-%m-%d"),
                            "amount": bill["amount"],
                            "szyf": bill["szyf"],
                            "wsf": bill["wsf"],
                            "sf": bill["sf"],
                        }
                    }
                    self.info["cycle"].update({cycle_date: amount_detail})
            LOGGER.info("get_payment_bill end " + str(self.info))
        else:
            LOGGER.error("get_payment_bill res state code: %s" %
                         (response.status))
            raise InvalidData(
                f"get_payment_bill response status_code = {response.status}"
            )

    async def get_monthly_bill(self, bill_cycle):
        """
        获取单个月份的账单详情
        :param bill_cycle: 账单周期 如 2023年6月
        :return:
        """
        monthly_api = SERVICE_HOST + "/api/member/bizMyWater/getPcMonthlyBill"
        params = {"userCode": self.user_code, "billDate": bill_cycle}
        response = await self._session.get(url=monthly_api, params=params, timeout=10)
        if response.status == 200:
            json_body = json.loads(await response.read())
            LOGGER.info("get_monthly_bill: " + str(json_body))
            detail_data = json_body["data"]
            if detail_data["endValue"] == "":
                raise InvalidData("未查询到账单详情,请检查账单周期是否错误!")
            result = {"date": bill_cycle, "usage": detail_data["total"]}

            if self.info["cycle"][bill_cycle]["fee"]["pay"] == 0:
                amount_detail = {
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
            if "total_usage" not in self.info.keys() or self.info["total_usage"] < int(detail_data["grandTotal"]):
                self.info.update(
                    {"total_usage": int(detail_data["grandTotal"])}
                )  # 记录第一阶梯总使用量
            meter_values = detail_data["endValue"].split("/")
            for i in range(len(meter_values)):
                if len(self.info["meter_value"]) <= i:
                    self.info["meter_value"].append({i: int(meter_values[i])})
                elif "meter_value" in self.info and i < len(self.info["meter_value"]):
                    existing_value = self.info["meter_value"][i].get(i, None)
                    if existing_value is None or existing_value < int(meter_values[i]):
                        self.info["meter_value"][i][i] = int(meter_values[i])
                        self.info.update({"first_step_left": int(detail_data["stepLeft"]["fist"])})  # 记录第一阶梯剩余使用量
            self.info.update(
                {"first_step_price": float(detail_data["firstStep"]["price"])}
            )  # 记录第一阶梯水费单价
            self.info.update(
                {
                    "wastwater_treatment_price": float(detail_data["waterborneFee"]["price"])
                }
            )  # 污水处理费
            self.info.update(
                {"water_tax": float(detail_data["taxFee"]["price"])}
            )  # 水资源费
            self.info.update(
                {"second_step_left": int(detail_data["stepLeft"]["second"])}
            )  # 记录第二阶梯剩余使用量
            self.info.update(
                {
                    "total_cost": self.info["water_tax"]
                    + self.info["first_step_price"]
                    + self.info["wastwater_treatment_price"]
                }
            )
            LOGGER.info("周期使用量: %s" % str(result))
            LOGGER.info(self.info)
            return self.info
        else:
            LOGGER.error("get_monthly_bill res state code: %s" %
                         (response.status))
            raise InvalidData(
                f"get_monthly_bill response status_code = {response.status}"
            )

    async def fetch_data(self):
        await self.get_bill_cycle_range()
        await self.get_payment_bill()
        for bill_date in self.bill_cycle:
            await self.get_monthly_bill(bill_date)
        return self.info


# if __name__ == "__main__":
#     user_code = "051351243"
#     # user_code = "01"
#     client = BJWater(user_code)
#     client.get_payment_bill()
#     for c in client.bill_cycle:
#         client.get_monthly_bill(c)
#     # print(client.get_payment_bill())
#     print(client.bill_cycle)
#     print(client.amount_cycle)
#     print(client.usage_cycle)
#     print(client.meter_value)
#     print(client.total_usage)
