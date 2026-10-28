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
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, LOGGER

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

    # 复用 __init__.py 里已经刷新好的 coordinator。
    # 之前这里会再 new 一个 coordinator 并 async_refresh() 一次，
    # 导致集成启动时整套用水数据接口被拉两遍（日志里同一段出现两次）。
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id) or {}
    coordinator: DataUpdateCoordinator | None = entry_data.get("coordinator")
    data = coordinator.data if coordinator is not None else None
    if coordinator is None:
        LOGGER.error("未找到已初始化的数据协调器，跳过用水数据实体注册")

    # 1. 添加常规字典中的传感器
    if coordinator is not None and data is not None:
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

    # 历史账期的长期统计（LTS）在实体加入 HA 后再导入：
    # 那时 entity_id 才是最终值（HA 会按设备名 + 实体名生成，可能是拼音），
    # 见 BJWaterSensor / BJWaterLatestUsageSensor 的 async_added_to_hass。


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
        LOGGER.debug("bj_water sensor num: %s", sensor_num)
        LOGGER.debug("bj_water sensor key: %s", sensor_key)
        # 注意：不要设置 self._attr_entity_id —— HA 的实体平台只读取 entity.entity_id，
        # 没有 `_attr_entity_id` 这个约定，写了也不生效。实体 id 由注册表按
        # 「设备名 + 实体名」生成（中文会被 slugify 成拼音），需要固定 id 请在
        # 界面上改，或通过 entity_registry 预注册。
        if sensor_num == 0:
            self._attr_unique_id = f"{DOMAIN}.{user_code}_{sensor_key}"
        else:
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

    async def async_added_to_hass(self) -> None:
        """实体加入后导入当期水费的历史统计（此时 entity_id 才确定）。"""
        await super().async_added_to_hass()
        if self._sensor_key != "latest_bill_amount" or not self.entity_id:
            return
        cycle_data = self.coordinator.data.get("cycle", {}) if self.coordinator.data else {}
        _async_import_measurement_history(
            self.hass,
            self.entity_id,
            "当期历史水费",
            "CNY",
            None,
            _cycle_series(cycle_data, "cost"),
        )


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

    async def async_added_to_hass(self) -> None:
        """实体加入后导入当期用水量的历史统计（此时 entity_id 才确定）。"""
        await super().async_added_to_hass()
        if not self.entity_id:
            return
        _async_import_measurement_history(
            self.hass,
            self.entity_id,
            "当期历史用水量",
            UnitOfVolume.CUBIC_METERS,
            "volume",
            _cycle_series(self._get_cycle_data(), "usage"),
        )


def _cycle_series(cycle_data: dict, field: str) -> list[tuple[datetime, float]]:
    """抽出当年账期的 usage / cost 序列（升序）。"""
    series: list[tuple[datetime, float]] = []
    current_year = str(datetime.now().year)
    for cycle_date, cycle in sorted(cycle_data.items(), key=lambda x: x[0]):
        if not cycle_date.startswith(current_year):
            continue
        if field == "usage":
            raw = cycle.get("meter", {}).get("usage")
        else:
            raw = cycle.get("fee", {}).get("amount")
        if raw is None:
            continue
        try:
            dt = datetime.strptime(cycle_date, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        series.append((dt, float(raw)))
    return series


def _async_import_measurement_history(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    unit_of_measurement: str | None,
    unit_class: str | None,
    series: list[tuple[datetime, float]],
) -> None:
    """
    把历史账期数据作为单期测量值写入某个实体的长期统计（LTS）。

    statistic_id 必须与实体真实的 entity_id 完全一致，否则历史曲线不会挂到该实体上，
    所以这里由调用方传入 self.entity_id，而不是拼 `sensor.bj_water_xxx_yyy`。
    """
    if not series:
        LOGGER.info("无当年账期数据，跳过历史统计导入: %s", statistic_id)
        return
    # 重要：这些实体的 state_class 是 MEASUREMENT，HA 的 sensor 记录器会把它们的
    # 统计元数据钉成 mean_type=ARITHMETIC（见 homeassistant/components/sensor/recorder.py
    # 的 DEFAULT_STATISTICS）。这种元数据下，前端读的是 mean 列而不是 state 列，
    # 只写 state 的话数据虽然进了库，但历史/统计图里一个点都不会画出来。
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_mean=True,
        has_sum=False,  # 测量值不需要 sum 累加
        name=name,
        source="recorder",
        statistic_id=statistic_id,
        unit_of_measurement=unit_of_measurement,
        unit_class=unit_class,
    )
    stats = [
        StatisticData(start=dt, mean=value, state=value) for dt, value in series
    ]
    LOGGER.info(
        "导入历史统计 %s: %d 条 %s ~ %s (%s)",
        statistic_id,
        len(stats),
        series[0][0].strftime("%Y-%m"),
        series[-1][0].strftime("%Y-%m"),
        unit_of_measurement or "无单位",
    )
    async_import_statistics(hass, metadata, stats)
