"""Sensor platform for bj_water integration."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import Platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, LOGGER, UPDATE_INTERVAL
from .bj_water import BJWater


SENSORS: dict[str, dict[str, object]] = {
    "total_usage": {
        "name": "累计用水量",
        "icon": "mdi:water-pump",
        "unit_of_measurement": "m³",
        "attributes": ["last_update"],
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "meter_value": {
        "name": "水表总数",
        "icon": "mdi:scale",
        "unit_of_measurement": "m³",
        "attributes": ["last_update"],
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "first_step_left": {
        "name": "第一阶梯剩余用量",
        "icon": "mdi:water-pump",
        "unit_of_measurement": "m³",
        "device_class": SensorDeviceClass.WATER,
        "attributes": ["last_update"],
    },
    "first_step_price": {
        "name": "第一阶梯水费单价",
        "icon": "mdi:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "wastwater_treatment_price": {
        "name": "污水处理费单价",
        "icon": "mdi:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "water_tax": {
        "name": "水资源费单价",
        "icon": "mdi:currency-cny",
        "unit_of_measurement": "CNY",
    },
    "second_step_left": {
        "name": "第二阶梯剩余用量",
        "icon": "mdi:water-pump",
        "unit_of_measurement": "m³",
        "device_class": SensorDeviceClass.WATER,
    },
    "total_cost": {
        "name": "当前水费总单价",
        "icon": "mdi:cash-100",
        "unit_of_measurement": "CNY/m³",
    },
    "latest_bill_amount": {
        "name": "当期水费",
        "icon": "mdi:cash",
        "unit_of_measurement": "CNY",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.MEASUREMENT,
    },
}


# 历史账单数据的属性标签映射
HISTORY_FEE_LABELS: dict[str, str] = {
    "amount": "总水费",
    "szyf": "水资源费",
    "wsf": "污水处理费",
    "sf": "水费",
    "pay": "缴费状态",
    "date": "缴费日期",
}

HISTORY_USAGE_LABELS: dict[str, str] = {
    "usage": "用水量",
    "value": "水表数",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor platform."""
    sensors_list: list[SensorEntity] = []
    user_code = config_entry.data["userCode"]
    token = config_entry.data.get("token", "")
    secret_key_enc = config_entry.data.get("secretKeyEnc", "")
    md5_salt_enc = config_entry.data.get("md5SaltEnc", "")
    api = BJWater(async_create_clientsession(hass), user_code, token, secret_key_enc, md5_salt_enc)

    coordinator = DataUpdateCoordinator(
        hass,
        logger=LOGGER,
        name=DOMAIN,
        update_interval=UPDATE_INTERVAL,
        update_method=api.fetch_data,
    )
    LOGGER.info("async_setup_entry: %s", coordinator)

    # 尝试刷新数据，如果失败（如认证失败），coordinator.data 为 None
    # 但仍然继续注册 sensor（token 有效期 sensor 不需要 coordinator 数据）
    try:
        await coordinator.async_refresh()
    except Exception as exc:
        LOGGER.warning(f"初始数据刷新失败（可能是认证问题），继续注册 sensor: {exc}")
    data = coordinator.data

    # 添加数据相关的传感器
    if data is not None:
        for key, value in data.items():
            if key in SENSORS:
                if isinstance(value, list):
                    for items in value:
                        for k, v in items.items():
                            sensors_list.append(BJWaterSensor(coordinator, user_code, key, v, k))
                else:
                    sensors_list.append(BJWaterSensor(coordinator, user_code, key, value))
            elif key == "cycle":
                # 按账期创建独立的传感器，便于在历史图表中展示
                for cycle_date, cycle_data in value.items():
                    sensors_list.append(
                        BJWaterHistoryFeeSensor(
                            coordinator, user_code, cycle_date, cycle_data.get("fee", {})
                        )
                    )
                    sensors_list.append(
                        BJWaterHistoryUsageSensor(
                            coordinator, user_code, cycle_date, cycle_data.get("meter", {})
                        )
                    )

    # 始终添加 token 有效期传感器（不依赖 coordinator data）
    from . import TokenValiditySensor
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    expected_unique_id = f"{DOMAIN}.{user_code}_token_validity"

    # 查找所有可能冲突的旧 entity（同名但不同 unique_id 的残留实体）
    # 包括各种旧名称生成的 entity_id（中文转拼音形式）
    old_entity_ids = [
        "sensor.token_you_xiao_qi",               # "Token 有效期"
        "sensor.token_you_xiao_qi_sheng_yu_tian_shu",  # "Token 有效期剩余天数"
        "sensor.token_validity",                   # "Token Validity"
    ]
    for suffix in ["", "_2", "_3", "_4", "_5", "_6", "_7", "_8", "_9", "_10"]:
        for base_name in old_entity_ids:
            existing = entity_registry.async_get(f"{base_name}{suffix}")
            if existing is not None and existing.unique_id != expected_unique_id:
                LOGGER.info(f"更新旧实体的 unique_id: {existing.entity_id} -> {expected_unique_id}")
                entity_registry.async_update_entity(
                    existing.entity_id,
                    new_unique_id=expected_unique_id,
                )

    token_sensor = TokenValiditySensor(config_entry, user_code)
    sensors_list.append(token_sensor)

    async_add_entities(sensors_list, update_before_add=True)


class BJWaterBaseSensor(CoordinatorEntity):
    """Base class for bj_water sensors."""

    _attr_should_poll = False

    def __init__(self, coordinator: DataUpdateCoordinator, user_code: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._user_code = user_code
        self._attr_unique_id = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, user_code)},
        )


class BJWaterSensor(BJWaterBaseSensor, SensorEntity):
    """Representation of a bj_water sensor."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        user_code: str,
        sensor_key: str,
        sensor_value: object,
        sensor_num: int = 0,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, user_code)
        if sensor_num == 0:
            self._attr_unique_id = f"{DOMAIN}.{user_code}_{sensor_key}"
        else:
            self._attr_unique_id = f"{DOMAIN}.{user_code}_{sensor_key}_{sensor_num}"
        self._sensor_key = sensor_key
        self._sensor_value = sensor_value
        self._sensor_num = sensor_num
        self._attr_name = SENSORS[sensor_key]["name"]
        if sensor_num > 0:
            self._attr_name = f"{self._attr_name}_{sensor_num}"
        self._attr_icon = SENSORS[sensor_key]["icon"]
        self._attr_native_unit_of_measurement = SENSORS[sensor_key]["unit_of_measurement"]
        if "device_class" in SENSORS[sensor_key]:
            self._attr_device_class = SENSORS[sensor_key]["device_class"]
        if "state_class" in SENSORS[sensor_key]:
            self._attr_state_class = SENSORS[sensor_key]["state_class"]

    @property
    def state(self) -> object:
        """Return the state of the sensor."""
        return self._sensor_value


class BJWaterHistoryFeeSensor(BJWaterBaseSensor, SensorEntity):
    """按账期创建的水费传感器，每个账期一个独立传感器。

    便于在 HA 历史图表中展示历史账单数据。
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        user_code: str,
        bill_date: str,
        sensor_attrs: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, user_code)
        # 使用账期作为 unique_id 的一部分，确保每个账期有独立的传感器
        self._attr_unique_id = f"{DOMAIN}.{user_code}_{bill_date}_fee"
        self._attr_name = f"水费 {bill_date}"
        self._attr_icon = "mdi:currency-cny"
        self._attr_native_unit_of_measurement = "CNY"
        self._sensor_attrs = sensor_attrs
        self._attr_device_class = SensorDeviceClass.MONETARY

    @property
    def state(self) -> object:
        """Return the state of the sensor (总水费)."""
        return self._sensor_attrs.get("amount")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the state attributes."""
        attrs: dict[str, object] = {}
        for k, v in self._sensor_attrs.items():
            if k not in HISTORY_FEE_LABELS:
                continue
            label = HISTORY_FEE_LABELS[k]
            if k == "pay":
                attrs[label] = "已缴费" if v == 1 else "未缴费"
            else:
                attrs[label] = v
        return attrs


class BJWaterHistoryUsageSensor(BJWaterBaseSensor, SensorEntity):
    """按账期创建的用水量传感器，每个账期一个独立传感器。

    便于在 HA 历史图表中展示历史用水量数据。
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        user_code: str,
        bill_date: str,
        sensor_attrs: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, user_code)
        # 使用账期作为 unique_id 的一部分，确保每个账期有独立的传感器
        self._attr_unique_id = f"{DOMAIN}.{user_code}_{bill_date}_usage"
        self._attr_name = f"用水量 {bill_date}"
        self._attr_icon = "mdi:water"
        self._attr_native_unit_of_measurement = "m³"
        self._sensor_attrs = sensor_attrs
        self._attr_device_class = SensorDeviceClass.WATER

    @property
    def state(self) -> object:
        """Return the state of the sensor (用水量)."""
        return self._sensor_attrs.get("usage")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the state attributes."""
        attrs: dict[str, object] = {}
        for k, v in self._sensor_attrs.items():
            if k == "usage":
                attrs[HISTORY_USAGE_LABELS[k]] = v
            elif k == "value":
                if isinstance(v, list) and len(v) > 0:
                    value_list = v[0]
                    if isinstance(value_list, list) and len(value_list) > 0:
                        attrs[HISTORY_USAGE_LABELS[k]] = value_list[0]
        return attrs
