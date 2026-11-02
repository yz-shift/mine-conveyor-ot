import pytest

from shared.settings import load_settings


def test_defaults_stay_on_localhost():
    settings = load_settings({})
    assert settings.modbus_host == "127.0.0.1"
    assert settings.modbus_port == 5020
    assert settings.hmi_port == 8000


def test_environment_overrides_defaults():
    settings = load_settings({"MODBUS_PORT": "1502", "HMI_HOST": "0.0.0.0"})
    assert settings.modbus_port == 1502
    assert settings.hmi_host == "0.0.0.0"


@pytest.mark.parametrize("value", ["abc", "0", "70000"])
def test_bad_port_is_rejected(value):
    with pytest.raises(ValueError, match="MODBUS_PORT"):
        load_settings({"MODBUS_PORT": value})
