"""Configuration loading for Pepper Watch.

Layering, lowest precedence first:

    config/default.toml  ->  config/local.toml  ->  PW_* environment variables

Environment names are derived from the table path: ``[camera].preview_fps``
becomes ``PW_CAMERA_PREVIEW_FPS``. Values are coerced to the type of the
default they replace, so ``PW_WEB_PORT=9000`` arrives as an int and a bogus
value fails loudly at startup rather than halfway through a field trial.

Targets Python 3.11 (the version Raspberry Pi OS Bookworm ships) so the same
source runs unchanged on the development Mac and on the Pi.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "local.toml"
PROFILE_DIR = PROJECT_ROOT / "config" / "profiles"
ENV_PREFIX = "PW"

# Boards this project has a tuned profile for. Anything else runs on
# default.toml, which is sized for the Pi 5.
KNOWN_BOARDS = ("pi5", "pi4", "pi3", "pizero2")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or cannot be coerced."""


class Section:
    """Attribute access over a nested config table.

    Dotted access keeps call sites readable (``cfg.camera.preview_fps``) while
    the underlying data stays a plain dict, so it round-trips to JSON for the
    settings page without a custom encoder.
    """

    __slots__ = ("_data", "_path")

    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        self._data = data
        self._path = path

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError:
            raise AttributeError(
                f"no config key {self._join(name)!r}; "
                f"available: {', '.join(sorted(self._data))}"
            ) from None
        if isinstance(value, dict):
            return Section(value, self._join(name))
        return value

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        """Deep copy, safe to mutate or serialise."""
        return _deep_copy(self._data)

    def _join(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def __repr__(self) -> str:
        return f"Section({self._path or 'root'}: {', '.join(sorted(self._data))})"


class Config(Section):
    """Root config, with the handful of derived values callers actually want."""

    __slots__ = ()

    @property
    def root(self) -> Path:
        return PROJECT_ROOT

    def path(self, value: str) -> Path:
        """Resolve a config-relative path against the project root."""
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    @property
    def db_path(self) -> Path:
        return self.path(self.db.path)

    @property
    def media_root(self) -> Path:
        return self.path(self.media.root)

    @property
    def model_path(self) -> Path:
        return self.path(self.inference.model_path)

    @property
    def platform_kind(self) -> str:
        """Resolved platform: ``"pi"`` or ``"mock"``, never ``"auto"``."""
        kind = self.platform.kind
        return detect_platform() if kind == "auto" else kind

    @property
    def is_pi(self) -> bool:
        return self.platform_kind == "pi"


def detect_platform() -> str:
    """Return ``"pi"`` on Raspberry Pi hardware, else ``"mock"``.

    Reads the device tree model rather than checking ``platform.machine()``,
    which reports ``aarch64`` on plenty of things that are not a Pi.
    """
    model = Path("/proc/device-tree/model")
    try:
        # The device tree string is NUL-terminated.
        text = model.read_bytes().decode("utf-8", "replace").rstrip("\x00")
    except OSError:
        return "mock"
    return "pi" if "raspberry pi" in text.lower() else "mock"


def read_device_model() -> str:
    """The device tree model string, or an empty string off-device."""
    try:
        # The device tree string is NUL-terminated.
        return Path("/proc/device-tree/model").read_bytes().decode("utf-8", "replace").rstrip("\x00")
    except OSError:
        return ""


def detect_board(model: str | None = None) -> str:
    """Identify the board, so its profile can be applied.

    Returns one of :data:`KNOWN_BOARDS`, or ``"unknown"``. The distinction
    matters because default.toml is sized for a Pi 5: a Pi 3 has an eighth of
    the RAM and a CPU several times slower, and running the Pi 5 settings there
    means the memory caps never engage before the kernel's OOM killer does.

    ``PW_BOARD`` overrides detection, which is how the tests exercise each
    profile without the hardware.
    """
    override = os.environ.get(f"{ENV_PREFIX}_BOARD", "").strip().lower()
    if override:
        return override

    text = (model if model is not None else read_device_model()).strip()
    if "raspberry pi" not in text.lower():
        return "unknown"

    lowered = text.lower()
    # Check Zero before the numeric match: "Raspberry Pi Zero 2 W" contains a
    # digit that would otherwise read as a model number.
    if "zero 2" in lowered:
        return "pizero2"
    if "zero" in lowered:
        return "pizero"

    match = re.search(r"raspberry pi\s+(?:compute module\s+)?(\d+)", lowered)
    if match:
        return f"pi{match.group(1)}"
    return "unknown"


def total_memory_mb() -> float | None:
    """Physical RAM in MB, read from /proc/meminfo. ``None`` off Linux."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        pass
    try:
        import psutil

        return psutil.virtual_memory().total / 1e6
    except Exception:  # noqa: BLE001
        return None


def profile_path(board: str) -> Path | None:
    """The tuning profile for a board, if one ships."""
    candidate = PROFILE_DIR / f"{board}.toml"
    return candidate if candidate.exists() else None


def load(overrides: dict[str, Any] | None = None, *, use_env: bool = True) -> Config:
    """Build the effective configuration.

    ``overrides`` is a nested dict merged above the TOML files and below the
    environment; tests use it to pin values without touching os.environ.
    """
    if not DEFAULT_CONFIG.exists():
        raise ConfigError(f"missing {DEFAULT_CONFIG}")

    data = _load_toml(DEFAULT_CONFIG)

    # Board profile sits between the shipped defaults and the operator's local
    # overrides: it adapts to the hardware without silently overriding a
    # deliberate choice in local.toml or the environment.
    board = detect_board()
    profile = profile_path(board)
    if profile is not None:
        _merge(data, _load_toml(profile))
        data.setdefault("platform", {})["profile"] = board

    if LOCAL_CONFIG.exists():
        _merge(data, _load_toml(LOCAL_CONFIG))
    if overrides:
        _merge(data, overrides)
    if use_env:
        _apply_env(data, os.environ)
    return Config(data)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _merge(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Recursive in-place merge; scalars and lists replace wholesale."""
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _apply_env(data: dict[str, Any], environ: "os._Environ[str] | dict[str, str]") -> None:
    for path, current in list(_walk(data)):
        name = "_".join([ENV_PREFIX, *path]).upper()
        if name not in environ:
            continue
        _assign(data, path, _coerce(environ[name], current, name))


def _walk(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            yield from _walk(value, path)
        else:
            yield path, value


def _assign(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _coerce(raw: str, current: Any, name: str) -> Any:
    """Coerce an env string to the type of the value it replaces."""
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name}={raw!r} is not a boolean")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{name}={raw!r} is not a number") from None
    if isinstance(current, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
