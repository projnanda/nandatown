"""The twelve replaceable protocol layers.

Every interaction between agents follows rules set by one of these
layers. Each has a working default plugin, and a scenario can swap any
of them for another registered plugin. A contributor who finds a problem
that fits no existing layer can propose a new one.
"""

from __future__ import annotations

from typing import Any

LAYER_NAMES = [
    "transport",
    "communication",
    "identity",
    "registry",
    "auth",
    "trust",
    "payments",
    "coordination",
    "negotiation",
    "memory",
    "privacy",
    "data_facts",
]

_REGISTRY: dict[str, dict[str, type]] = {name: {} for name in LAYER_NAMES}


class UnknownPlugin(Exception):
    pass


def register(layer: str, plugin_id: str):
    """Class decorator: register a plugin for a layer."""
    if layer not in LAYER_NAMES:
        raise UnknownPlugin(f"unknown layer {layer!r}")

    def wrap(cls: type) -> type:
        cls.layer = layer
        cls.plugin_id = plugin_id
        _REGISTRY[layer][plugin_id] = cls
        return cls

    return wrap


def resolve(layer: str, plugin_id: str) -> type:
    try:
        return _REGISTRY[layer][plugin_id]
    except KeyError:
        raise UnknownPlugin(
            f"no plugin {plugin_id!r} for layer {layer!r};"
            f" registered: {sorted(_REGISTRY.get(layer, {}))}")


def plugins() -> dict[str, list[dict[str, Any]]]:
    return {
        layer: [
            {"plugin_id": pid, "summary": (cls.__doc__ or "").strip()
             .splitlines()[0] if cls.__doc__ else ""}
            for pid, cls in sorted(_REGISTRY[layer].items())
        ]
        for layer in LAYER_NAMES
    }


DEFAULT_PLUGINS = {
    "transport": "memory.v1",
    "communication": "envelope.v1",
    "identity": "keys.v1",
    "registry": "index.v1",
    "auth": "hmac.v1",
    "trust": "reputation.v1",
    "payments": "ledger.v1",
    "coordination": "contractnet.v1",
    "negotiation": "haggle.v1",
    "memory": "kv.v1",
    "privacy": "redact.v1",
    "data_facts": "evidence.v1",
}

# Import the default plugin modules so they self-register.
from . import (  # noqa: E402,F401
    auth,
    communication,
    coordination,
    data_facts,
    identity,
    memory,
    negotiation,
    payments,
    privacy,
    registry_layer,
    transport,
    trust,
    trust_capped,
)
