"""Configuration loading for Pepper Watch.

Layering, lowest precedence first:

    config/default.toml  ->  config/local.toml  ->  PW_* environment variables

Environment names are derived from the table path: ``[camera].preview_fps``
becomes ``PW_CAMERA_PREVIEW_FPS``. Values are coerced to the type of the
default they replace, so ``PW_WEB_PORT=9000`` arrives as an int and a bogus
value fails loudly at startup rather than halfway through a field trial.

Runs on Python 3.10 to 3.13: 3.11 and 3.13 are what Raspberry Pi OS Bookworm
and Trixie ship, and 3.10 is Ubuntu 22.04's, which many Orange Pi and Banana Pi
images are built on.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; requirements.txt pulls in tomli there
    import tomli as tomllib  # type: ignore[no-redef]

from . import board as _board

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "local.toml"
PROFILE_DIR = PROJECT_ROOT / "config" / "profiles"
ENV_PREFIX = "PW"

# Tiers with a settings profile. The "high" tier (Pi 5 class) is what
# default.toml is sized for, so it has none.
TIER_PROFILES = ("mid", "low", "tiny")


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
    def hef_path(self) -> Path:
        return self.path(self.accelerator.hef_path)

    @property
    def sessions_root(self) -> Path:
        return self.path(self.sessions.root)

    @property
    def board_profile(self) -> str | None:
        """The profiles applied for this board, e.g. ``"low"`` or ``"low+pi3"``."""
        applied = self._data.get("board", {}).get("profiles") or []
        return "+".join(applied) or None

    @property
    def board_tier(self) -> str | None:
        return self._data.get("board", {}).get("tier")

    def with_profile(self, name: str) -> "Config":
        """A copy with ``[profiles.<name>]`` merged over it.

        The worker calls this once per session, after it has found out whether
        an AI HAT+ is attached, so capture rate and thread counts suit the
        hardware actually present.
        """
        data = _deep_copy(self._data)
        _merge(data, _deep_copy(data.get("profiles", {}).get(name, {})))
        data["session_profile"] = name
        return Config(data)


def is_raspberry_pi() -> bool:
    """True on Raspberry Pi hardware, judged by the device tree model."""
    return detect_board_info().is_raspberry_pi


def read_device_model() -> str:
    """The device tree model string, or an empty string off-device."""
    return _board.read_model()


def detect_board_info() -> _board.BoardInfo:
    """This board: family, CPU class, RAM and tier. See ``pepperwatch.board``."""
    return _board.detect()


def detect_board(model: str | None = None) -> str:
    """The board key: ``pi5``, ``pi4``, ``pi3``, ``orangepi-zero3``, ... or ``unknown``.

    ``PW_BOARD`` overrides detection, which is how the tests exercise each
    profile without the hardware.
    """
    if model is None:
        return detect_board_info().key
    return _board.detect(model=model, compatible=[], cpuinfo="", mem_mb=None).key


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


def profile_path(name: str) -> Path | None:
    """A tier or board profile, if one ships."""
    candidate = PROFILE_DIR / f"{name}.toml"
    return candidate if candidate.exists() else None


def load(overrides: dict[str, Any] | None = None, *, use_env: bool = True) -> Config:
    """Build the effective configuration.

    ``overrides`` is a nested dict merged above the TOML files and below the
    environment; tests use it to pin values without touching os.environ.
    """
    if not DEFAULT_CONFIG.exists():
        raise ConfigError(f"missing {DEFAULT_CONFIG}")

    data = _load_toml(DEFAULT_CONFIG)

    # Hardware profiles sit between the shipped defaults and the operator's
    # local overrides: they adapt to the board without silently overriding a
    # deliberate choice in local.toml or the environment. The tier's profile
    # (CPU class and RAM) comes first, then any profile for the exact board.
    info = detect_board_info()
    applied = []
    for name in (info.tier, info.key):
        profile = profile_path(name)
        if profile is not None and name not in applied:
            _merge(data, _load_toml(profile))
            applied.append(name)
    data["board"] = {"key": info.key, "tier": info.tier, "family": info.family, "profiles": applied}

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
