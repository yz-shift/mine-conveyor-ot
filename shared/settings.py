"""Runtime settings from environment variables (and a local .env file)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    modbus_host: str = "127.0.0.1"
    # 502 is the standard Modbus port but needs elevated rights on most systems.
    modbus_port: int = 5020
    hmi_host: str = "127.0.0.1"
    hmi_port: int = 8000


def _port(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{key} must be between 1 and 65535, got {port}")
    return port


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from `env`, or from .env and the process environment when omitted."""
    if env is None:
        load_dotenv()
        env = os.environ
    defaults = Settings()
    return Settings(
        modbus_host=env.get("MODBUS_HOST", defaults.modbus_host),
        modbus_port=_port(env, "MODBUS_PORT", defaults.modbus_port),
        hmi_host=env.get("HMI_HOST", defaults.hmi_host),
        hmi_port=_port(env, "HMI_PORT", defaults.hmi_port),
    )
