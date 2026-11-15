"""Fixtures: a fake ESPHome-style proxy, valve advertisements and the library's simulated valve."""

from __future__ import annotations

from collections.abc import Generator
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from habluetooth import BaseHaRemoteScanner, HaBluetoothConnector
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.mira_ble.const import (
    CONF_OUTLETS,
    CONF_PAIRED_AT,
    CONF_PAIRED_SOURCE,
    CONF_PRESETS,
    CONF_PRODUCT_CODE,
    CONF_SERIAL_NUMBER,
    CONF_VALVE_NAME,
    DOMAIN,
)
from mira_ble import SERVICE_UUID
from mira_ble.testing import OUTLET_2_CONFIG, FakeValve

VALVE_ADDRESS = "AA:BB:CC:DD:12:34"
VALVE_NEW_ADDRESS = "AA:BB:CC:DD:56:78"
SERIAL = "3140090125121900"
NAME_WITH_SERIAL = f"Mira 0051#{SERIAL}"
NAME_WITH_LABEL = "Mira 0051 Bathroom"
PROXY_SOURCE = "00:11:22:33:AA:01"
PROXY_NAME = "esphome-proxy-aa01"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components from this repository."""


@pytest.fixture(autouse=True)
def bluetooth_stack(enable_bluetooth: None) -> None:
    """Start the Bluetooth integration with a mocked local adapter."""


@pytest.fixture(autouse=True)
def close_stale_connections() -> Generator[AsyncMock]:
    """Keep setup away from the host's BlueZ."""
    with patch("custom_components.mira_ble.close_stale_connections_by_address") as close_stale:
        yield close_stale


@pytest.fixture(autouse=True)
def no_confirm_delay() -> Generator[None]:
    """Keep the confirmation loop's shape but not its real-time waits."""
    with (
        patch("mira_ble.valve.CONFIRM_INTERVAL", 0.001),
        patch("mira_ble.valve.CONFIRM_TIMEOUT", 0.05),
    ):
        yield


class FakeProxyScanner(BaseHaRemoteScanner):
    """A connectable remote scanner standing in for an ESPHome Bluetooth proxy."""

    def inject(
        self, address: str = VALVE_ADDRESS, *, name: str = NAME_WITH_SERIAL, rssi: int = -67
    ) -> None:
        """Deliver one advertisement as if the proxy had heard it."""
        self._async_on_advertisement(
            address,
            rssi,
            name,
            [SERVICE_UUID],
            {},
            {},
            None,
            {"source": self.source, "address_type": 1},
            time.monotonic(),
        )


@pytest.fixture
def proxy(hass: HomeAssistant) -> Generator[FakeProxyScanner]:
    """Register a connectable fake proxy that hears the valve."""
    connector = HaBluetoothConnector(
        client=MagicMock(), source=PROXY_SOURCE, can_connect=lambda: True
    )
    scanner = FakeProxyScanner(PROXY_SOURCE, PROXY_NAME, connector, True)
    unregister = bluetooth.async_register_scanner(hass, scanner, connection_slots=3)
    scanner.inject()
    yield scanner
    unregister()


def service_info(
    address: str = VALVE_ADDRESS, name: str | None = NAME_WITH_SERIAL, **overrides: Any
) -> BluetoothServiceInfoBleak:
    """A BluetoothServiceInfoBleak as the config flow receives it from discovery."""
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

    uuids = overrides.pop("service_uuids", [SERVICE_UUID])
    return BluetoothServiceInfoBleak(
        name=name or address,
        address=address,
        rssi=-67,
        manufacturer_data={},
        service_data={},
        service_uuids=uuids,
        source=PROXY_SOURCE,
        device=BLEDevice(address, name, {"source": PROXY_SOURCE}),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=uuids,
            tx_power=None,
            rssi=-67,
            platform_data=(),
        ),
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def outlet(number: int, kind: str, max_temperature: float = 45.0) -> dict[str, Any]:
    """One outlet as pairing stores it."""
    return {
        "number": number,
        "kind": kind,
        "min_temperature": 30.0,
        "max_temperature": max_temperature,
        "default_temperature": 38.0,
        "min_flow": 4.0,
        "max_flow": 16.0,
        "default_flow": 12.0,
        "max_duration": 1800,
    }


ENTRY_DATA: dict[str, Any] = {
    CONF_ADDRESS: VALVE_ADDRESS,
    CONF_SERIAL_NUMBER: SERIAL,
    CONF_VALVE_NAME: "Bathroom",
    CONF_PRODUCT_CODE: "0051",
    CONF_OUTLETS: [outlet(1, "hand_shower"), outlet(2, "overhead", 46.0)],
    CONF_PRESETS: [{"slot": 1, "name": "Default"}, {"slot": 2, "name": "Default Bathfill"}],
    CONF_PAIRED_SOURCE: PROXY_SOURCE,
    CONF_PAIRED_AT: "2026-11-15T12:00:00+00:00",
}


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A paired valve with two named outlets and two presets."""
    return MockConfigEntry(domain=DOMAIN, title="Bathroom", unique_id=SERIAL, data=dict(ENTRY_DATA))


@pytest.fixture
def valve() -> Generator[FakeValve]:
    """The library's simulated valve, used for every connection the integration makes."""
    fake = FakeValve(source=PROXY_SOURCE)
    # The recorded valve reports both outlets as hand showers; make the second an overhead
    # one, as the stored entry says, so the outlets keep distinct names when re-read.
    overhead = bytearray(OUTLET_2_CONFIG)
    overhead[1] = 31
    fake.outlet_configs[2] = bytes(overhead)
    with (
        patch("custom_components.mira_ble.coordinator.BleTransport", return_value=fake),
        patch("custom_components.mira_ble.config_flow.BleTransport", return_value=fake),
    ):
        yield fake


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, proxy: FakeProxyScanner, valve: FakeValve
) -> MockConfigEntry:
    """Set the integration up against the simulated valve."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
