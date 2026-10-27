"""Sensor platform for bj_water integration."""
from __future__ import annotations

from datetime import datetime, timezone
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
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
    # 物理水表当前读数
    "meter_value": {
        "name": "水表总数",
        "icon": "mdi:scale",
        "unit_of_measurement": "m³",
        "attributes": ["last_update"],
        "device_class": SensorDeviceClass.WATER,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    # 阶梯剩余用量
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
        "device_class": SensorDeviceClass.WATER,
    },
    "total_cost": {
        "name": "当前水费总单价",
        "icon": "mdi:cash-100",
        "unit_of_measurement": "CNY/m³",
    },
    # 当期水费（最新单期发生额，测量值）
    "latest_bill_amount": {
        "name": "当期水费",
        "icon": "mdi:cash",
        "unit_of_measurement": "CNY",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    # 累计水费（当年总累计，单调递增，需注入长期统计）
    "total_cost_accumulated": {
        "name": "累计水费",
        "icon": "mdi:cash-multiple",
        "unit_of_measurement": "CNY",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    }
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

    try:
        await coordinator.async_refresh()
    except Exception as exc:
        LOGGER.warning(f"初始数据刷新失败（可能是认证问题），继续注册 sensor: {exc}")
    data = coordinator.data

    # 1. 添加常规字典中的传感器
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
                # 2. 注册累计总用水量传感器 (Year-to-date 累计值，需注入 LTS)
                sensors_list.append(
                    BJWaterTotalUsageSensor(coordinator, user_code, value)
                )
                # 3. 注册当期用水量传感器 (最新单期独立值，测量值)
                sensors_list.append(
                    BJWaterLatestUsageSensor(coordinator, user_code, value)
                )

    # 注册 Token 有效期传感器
    from . import TokenValiditySensor
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    expected_unique_id = f"{DOMAIN}.{user_code}_token_validity"

    old_entity_ids = [
        "sensor.token_you_xiao_qi",
        "sensor.token_you_xiao_qi_sheng_yu_tian_shu",
        "sensor.token_validity",
    ]
    for suffix in ["", "_2", "_3", "_4", "_5", "_6", "_7", "_8", "_9", "_10"]:
        for base_name in old_entity_ids:
            existing = entity_registry.async_get(f"{base_name}{suffix}")
            if existing is not None and existing.unique_id != expected_unique_id:
                entity_registry.async_update_entity(
                    existing.entity_id,
                    new_unique_id=expected_unique_id,
                )

    token_sensor = TokenValiditySensor(config_entry, user_code)
    sensors_list.append(token_sensor)

    async_add_entities(sensors_list)

    # 4. 将历史账期数据准确导入累计型传感器的长期统计（LTS）
    if data is not None and isinstance(data.get("cycle"), dict):
        await _async_import_bill_history_to_recorder(hass, user_code, data["cycle"])


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
        LOGGER.info("bj_water sensor num: %s", sensor_num)
        LOGGER.info("bj_water sensor key: %s", sensor_key)
        if sensor_num == 0:
            self._attr_entity_id = f"sensor.{DOMAIN}_{user_code}_{sensor_key}"
            self._attr_unique_id = f"{DOMAIN}.{user_code}_{sensor_key}"
        else:
            self._attr_entity_id = f"sensor.{DOMAIN}_{user_code}_{sensor_key}_{sensor_num}"
            self._attr_unique_id = f"{DOMAIN}.{user_code}_{sensor_key}_{sensor_num}"
        self._sensor_key = sensor_key
        self._sensor_value = sensor_value
        self._sensor_num = sensor_num

        sensor_config = SENSORS.get(sensor_key, {})

        self._attr_name = sensor_config.get("name", sensor_key)
        if sensor_num > 0:
            self._attr_name = f"{self._attr_name}_{sensor_num}"

        self._attr_icon = sensor_config.get("icon")

        # 使用 .get() 避免 KeyError，如果没有单位则为 None
        self._attr_native_unit_of_measurement = sensor_config.get("unit_of_measurement")

        if "device_class" in sensor_config:
            self._attr_device_class = sensor_config["device_class"]
        if "state_class" in sensor_config:
            self._attr_state_class = sensor_config["state_class"]

    @property
    def state(self) -> object:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return self._sensor_value
        value = self.coordinator.data.get(self._sensor_key)
        if isinstance(value, list):
            if self._sensor_num > 0 and self._sensor_num < len(value):
                item = value[self._sensor_num]
                if isinstance(item, dict):
                    return item.get(str(self._sensor_num), item.get(self._sensor_num))
                return item
            elif len(value) == 1 and isinstance(value[0], dict):
                return next(iter(value[0].values()))
            return self._sensor_value
        if value is None:
            return self._sensor_value
        return value


class BJWaterTotalUsageSensor(BJWaterBaseSensor, SensorEntity):
    """【累计型】总用水量传感器（当年滚动累计值，单调递增，对应 LTS）。"""

    def __init__(
            self,
            coordinator: DataUpdateCoordinator,
            user_code: str,
            cycle_data: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, user_code)
        self._attr_unique_id = f"{DOMAIN}.{user_code}_total_usage"
        self._attr_name = "总用水量"
        self._attr_icon = "mdi:water-pump"
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    def _get_cycle_data(self) -> dict:
        return self.coordinator.data.get("cycle", {}) if self.coordinator.data else {}

    @property
    def state(self) -> object:
        """返回当年所有账期用水量的累加总和."""
        cycle_data = self._get_cycle_data()
        if not cycle_data:
            if self.coordinator.data and "total_usage" in self.coordinator.data:
                return self.coordinator.data["total_usage"]
            return None

        total = 0.0
        current_year = str(datetime.now().year)
        for cycle_date, cycle in cycle_data.items():
            if not cycle_date.startswith(current_year):
                continue
            meter = cycle.get("meter", {})
            usage = meter.get("usage")
            if usage is not None:
                total += float(usage)
        return total if total > 0 else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs: dict[str, object] = {}
        cycle_data = self._get_cycle_data()
        if not cycle_data:
            return attrs
        for cycle_date in sorted(cycle_data.keys(), reverse=True):
            cycle = cycle_data[cycle_date]
            meter = cycle.get("meter", {})
            if meter.get("usage") is not None:
                attrs["账期日期"] = cycle_date
                attrs["当期单月用水量"] = meter.get("usage")
                break
        return attrs


class BJWaterLatestUsageSensor(BJWaterBaseSensor, SensorEntity):
    """【测量型】当期用水量传感器（仅反映最新一期单月独立用量）。"""

    def __init__(
            self,
            coordinator: DataUpdateCoordinator,
            user_code: str,
            cycle_data: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, user_code)
        self._attr_unique_id = f"{DOMAIN}.{user_code}_latest_usage"
        self._attr_name = "当期用水量"
        self._attr_icon = "mdi:water"
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.MEASUREMENT

    def _get_cycle_data(self) -> dict:
        return self.coordinator.data.get("cycle", {}) if self.coordinator.data else {}

    @property
    def state(self) -> object:
        """返回最新一期账单的用水量."""
        cycle_data = self._get_cycle_data()
        if not cycle_data:
            return None
        for cycle_date in sorted(cycle_data.keys(), reverse=True):
            cycle = cycle_data[cycle_date]
            meter = cycle.get("meter", {})
            if meter.get("usage") is not None:
                return meter.get("usage")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs: dict[str, object] = {}
        cycle_data = self._get_cycle_data()
        if not cycle_data:
            return attrs
        for cycle_date in sorted(cycle_data.keys(), reverse=True):
            cycle = cycle_data[cycle_date]
            meter = cycle.get("meter", {})
            if meter.get("usage") is not None:
                attrs["账期日期"] = cycle_date
                break
        return attrs


async def _async_import_bill_history_to_recorder(
    hass: HomeAssistant, user_code: str, cycle_data: dict
) -> None:
    """将历史账期数据作为单期测量值，准确导入到【当期传感器】的长期统计（LTS）中。"""
    sorted_cycles = sorted(cycle_data.items(), key=lambda x: x[0])

    usage_records: list[dict] = []
    cost_records: list[dict] = []
    current_year = str(datetime.now().year)

    for cycle_date, cycle in sorted_cycles:
        if not cycle_date.startswith(current_year):
            continue

        meter = cycle.get("meter", {})
        fee = cycle.get("fee", {})
        usage = meter.get("usage")
        amount = fee.get("amount")

        if usage is None and amount is None:
            continue
        try:
            dt = datetime.strptime(cycle_date, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if usage is not None:
            usage_records.append({"date": dt, "usage": float(usage)})
        if amount is not None:
            cost_records.append({"date": dt, "cost": float(amount)})

    # 🎯 核心修正：历史数据注入到【当期传感器】的 statistic_id 中
    statistic_id_latest_usage = f"sensor.{DOMAIN}_{user_code}_latest_usage"
    statistic_id_latest_cost = f"sensor.{DOMAIN}_{user_code}_latest_bill_amount"

    # 1. 导入当期用水量历史（作为独立测量值，has_sum=False）
    if usage_records:
        metadata_usage = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=False,  # 测量值不需要 sum 累加
            name="当期历史用水量",
            source="recorder",
            statistic_id=statistic_id_latest_usage,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            unit_class="volume",
        )
        stats_usage: list[StatisticData] = []
        for item in usage_records:
            stats_usage.append(
                StatisticData(
                    start=item["date"],
                    state=item["usage"],  # 直接写入当月独立值 (18, 22, 23等)
                )
            )
        async_import_statistics(hass, metadata_usage, stats_usage)

    # 2. 导入当期水费历史（作为独立测量值，has_sum=False）
    if cost_records:
        metadata_cost = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=False,  # 测量值不需要 sum 累加
            name="当期历史水费",
            source="recorder",
            statistic_id=statistic_id_latest_cost,
            unit_of_measurement="CNY",
            unit_class=None,
        )
        stats_cost: list[StatisticData] = []
        for item in cost_records:
            stats_cost.append(
                StatisticData(
                    start=item["date"],
                    state=item["cost"],  # 直接写入当月独立金额
                )
            )
        async_import_statistics(hass, metadata_cost, stats_cost)

