"""Replaceable local-intelligence infrastructure for P16R2."""

from app.infra.intelligence.plugin_registry import (
    PluginRegistration,
    PluginRegistry,
    PluginRegistryError,
    PluginRole,
)

__all__ = [
    "PluginRegistration",
    "PluginRegistry",
    "PluginRegistryError",
    "PluginRole",
]
