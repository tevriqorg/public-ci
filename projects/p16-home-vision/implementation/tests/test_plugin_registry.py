from __future__ import annotations

import pytest

from app.infra.intelligence.plugin_registry import (
    PluginRegistration,
    PluginRegistry,
    PluginRegistryError,
    PluginRole,
)


def test_registry_constructs_concrete_role_and_reports_provenance() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginRegistration(
            role=PluginRole.FRAME_REDUCER,
            name="coverage",
            version="v1",
            factory=lambda config: {"config": config},
        )
    )

    assert registry.names(PluginRole.FRAME_REDUCER) == ("coverage",)
    assert registry.provenance(PluginRole.FRAME_REDUCER, "coverage") == ("coverage", "v1")
    assert registry.create(PluginRole.FRAME_REDUCER, "coverage", {"window": 5}) == {
        "config": {"window": 5}
    }


def test_registry_rejects_duplicate_role_name() -> None:
    registry = PluginRegistry()
    registration = PluginRegistration(
        role=PluginRole.MODEL_RUNNER,
        name="openai-compatible",
        version="v1",
        factory=lambda config: config,
    )
    registry.register(registration)

    with pytest.raises(PluginRegistryError) as excinfo:
        registry.register(registration)

    assert excinfo.value.code == "PLUGIN_ALREADY_REGISTERED"
