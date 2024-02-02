from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
)
from .const import DOMAIN, LOGGER, UPDATE_INTERVAL
from .bj_water import BJWater


SENSORS = {
    "total_usage": {
        "name": "第一阶梯总用量",
        "icon": "hass:water-pump",
        "unit_of_measurement": "m³",
        "attributes": ["last_update"],
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL,
    },
    "meter_value": {
        "name": "水表总数",
        "icon": "hass:scale",
        "unit_of_measurement": "m³",
        "attributes": ["last_update"],
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "first_step_left": {
        "name": "第一阶梯剩余用量",
        "icon": "hass:water-pump",
        "unit_of_measurement": "m³",
        "device_class": SensorDeviceClass.WATER,
        "attributes": ["last_update"],
    },
    "first_step_price": {
        "name": "第一阶梯水费单价",
        "icon": "hass:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "wastwater_treatment_price": {
        "name": "污水处理费单价",
        "icon": "hass:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "water_tax": {
        "name": "水资源费单价",
        "icon": "hass:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "second_step_left": {
        "name": "第二阶梯剩余用量",
        "icon": "hass:water-pump",
        "unit_of_measurement": "m³",
        "device_class": SensorDeviceClass.WATER,
    },
    "total_cost": {
        "name": "当前水费总单价",
        "icon": "hass:cash-100",
        "unit_of_measurement": "CNY/m³",
        "device_class": SensorDeviceClass.WATER,
    },
}


HISTORY_FEE_SENSORS = {
    "amount": {"name": "总水费"},
    "szyf": {"name": "水资源费"},
    "wsf": {"name": "污水处理费"},
    "sf": {"name": "水费"},
    "pay": {"name": "缴费状态"},
    "date": {"name": "缴费日期"},
}

HISTORY_USAGE_SENSORS = {
    "usage": {"name": "总用水量"},
    "value": {"name": "水表数"},
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    sensors_list = []
    config = hass.data[DOMAIN][config_entry.entry_id]
    user_code = config["userCode"]
    api = BJWater(async_create_clientsession(hass), user_code)

    coordinator = DataUpdateCoordinator(hass, LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL, update_method=api.fetch_data,)
    LOGGER.info("async_setup_entry: " + str(coordinator))
    await coordinator.async_refresh()
    data = coordinator.data
    for key, value in data.items():
        if key in SENSORS.keys():
            if isinstance(value, list):
                for items in value:
                    for k, v in items.items():
                        sensors_list.append(BJWaterSensor(coordinator, user_code, key, v, k))
            else:
                sensors_list.append(BJWaterSensor(coordinator, user_code, key, value))
        elif key == "cycle":
            dict_data = value
            for k, v in dict_data.items():
                index = v["index"]
                sensors_list.append(BJWaterHistoryFeeSensor(coordinator, user_code, k, v["fee"], index))
                sensors_list.append(BJWaterHistoryUsageSensor(coordinator, user_code, k, v["meter"], index))
    async_add_entities(sensors_list, False)


class BJWaterBaseSensor(CoordinatorEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._unique_id = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def should_poll(self):
        return False


class BJWaterSensor(BJWaterBaseSensor, SensorEntity):
    def __init__(self, coordinator, user_code, sensor_key, sensor_value, sensor_num=0) -> None:
        super().__init__(coordinator)
        self._unique_id = f"{DOMAIN}.{user_code}_{sensor_key}" if sensor_num == 0 else f"{DOMAIN}.{user_code}_{sensor_key}_{sensor_num}"
        self.entity_id = self._unique_id
        self.sensor_key = sensor_key
        self.sensor_value = sensor_value
        self.sensor_num = sensor_num

    def get_value(self, attribute=None):
        try:
            if attribute is None:
                return self.sensor_value
            return SENSORS[self.sensor_key]["attribute"]
        except KeyError as e:
            return STATE_UNKNOWN

    @property
    def name(self):
        name = SENSORS[self.sensor_key]["name"]
        if self.sensor_num > 0:
            name = name + "_" + str(self.sensor_num)
        LOGGER.warn(name + ":" + str(self.sensor_num))
        return name

    @property
    def state(self):
        return self.get_value()

    @property
    def state_class(self):
        if "state_class" in SENSORS[self.sensor_key].keys():
            return SENSORS[self.sensor_key]["state_class"]
        else:
            return None

    @property
    def icon(self):
        return SENSORS[self.sensor_key]["icon"]

    @property
    def device_class(self):
        if "device_class" in SENSORS[self.sensor_key].keys():
            return SENSORS[self.sensor_key]["device_class"]
        else:
            return None

    @property
    def unit_of_measurement(self):
        return SENSORS[self.sensor_key]["unit_of_measurement"]


class BJWaterHistoryFeeSensor(BJWaterBaseSensor):
    def __init__(self, coordinator, user_code, bill_date, sensor_attrs, index) -> None:
        super().__init__(coordinator)
        self._unique_id = f"{DOMAIN}.{user_code}_{index}_Fee"
        self.entity_id = self._unique_id
        self._bill_date = bill_date
        self.sensor_attrs = sensor_attrs

    @property
    def name(self):
        return self._bill_date.replace("-", "") + "_Fee"

    @property
    def state(self):
        return self.sensor_attrs["amount"]

    @property
    def icon(self):
        return "hass:currency-cny"

    @property
    def unit_of_measurement(self):
        return "CNY"

    @property
    def extra_state_attributes(self):
        attrs = {}
        for k, v in self.sensor_attrs.items():
            attrs[HISTORY_FEE_SENSORS[k]["name"]] = v
            if k == "pay":
                attrs[HISTORY_FEE_SENSORS[k]["name"]
                      ] = "未缴费" if v == 0 else "已缴费"
        # if attrs["缴费状态"] == "未缴费":
        #     attrs["缴费日期"] = ""
        LOGGER.info("BJWaterHistoryFeeSensor: " + str(attrs))
        return attrs

    @property
    def device_class(self) -> str | None:
        return SensorDeviceClass.WATER


class BJWaterHistoryUsageSensor(BJWaterBaseSensor):
    def __init__(self, coordinator, user_code, bill_date, sensor_attrs, index) -> None:
        super().__init__(coordinator)
        self._unique_id = f"{DOMAIN}.{user_code}_{index}_Usage"
        self.entity_id = self._unique_id
        self._bill_date = bill_date
        self.sensor_attrs = sensor_attrs

    @property
    def name(self):
        return self._bill_date.replace("-", "") + "_Usage"

    @property
    def state(self):
        return self.sensor_attrs["usage"]

    @property
    def icon(self):
        return "hass:water-circle"

    @property
    def unit_of_measurement(self):
        return "m³"

    @property
    def extra_state_attributes(self):
        attrs = {}
        for k, v in self.sensor_attrs.items():
            attrs[HISTORY_USAGE_SENSORS[k]["name"]] = v
        LOGGER.info("BJWaterHistoryUsageSensor: " + str(attrs))
        return attrs

    @property
    def device_class(self) -> str | None:
        return SensorDeviceClass.WATER
