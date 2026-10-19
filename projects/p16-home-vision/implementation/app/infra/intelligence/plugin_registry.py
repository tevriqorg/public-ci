from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable


class PluginRole(StrEnum):
    FRAME_REDUCER = "frame_reducer"
    DETECTOR = "detector"
    MODEL_RUNNER = "model_runner"
    FACE_BACKEND = "face_backend"
    ASR_BACKEND = "asr_backend"


class PluginRegistryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PluginRegistration:
    role: PluginRole
    name: str
    version: str
    factory: Callable[[Any], Any]


class PluginRegistry:
    """Small typed registry for concrete P16 extension roles.

    The registry constructs implementations only. It deliberately has no database,
    deletion, media-identity, or Human-identity authority.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[PluginRole, str], PluginRegistration] = {}

    def register(self, registration: PluginRegistration) -> None:
        if not registration.name.strip() or not registration.version.strip():
            raise PluginRegistryError("PLUGIN_IDENTITY_INVALID")
        key = (registration.role, registration.name)
        if key in self._items:
            raise PluginRegistryError("PLUGIN_ALREADY_REGISTERED")
        self._items[key] = registration

    def create(self, role: PluginRole, name: str, config: Any = None) -> Any:
        registration = self._items.get((role, name))
        if registration is None:
            raise PluginRegistryError("PLUGIN_NOT_FOUND")
        try:
            return registration.factory(config)
        except PluginRegistryError:
            raise
        except Exception as exc:
            raise PluginRegistryError("PLUGIN_CREATE_FAILED") from exc

    def names(self, role: PluginRole) -> tuple[str, ...]:
        return tuple(sorted(name for item_role, name in self._items if item_role == role))

    def provenance(self, role: PluginRole, name: str) -> tuple[str, str]:
        registration = self._items.get((role, name))
        if registration is None:
            raise PluginRegistryError("PLUGIN_NOT_FOUND")
        return registration.name, registration.version
