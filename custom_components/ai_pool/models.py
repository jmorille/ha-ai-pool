"""Resolve which provider model sits behind a member entity.

Several members pointing at the same model is not a pool. A capacity refusal is
issued by the model, not by the account, so the moment two members share one
they fail together - and the pool, which knows only entity ids, cannot see the
duplication that defeats it.

Nothing in Home Assistant exposes "the model behind this entity", so this reads
the member's own config entry. Providers name that option differently and are
free to change it, which makes this a heuristic and not a lookup: an unknown
model is reported as ``None`` and simply carries no conclusion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

# Ordered by how specific they are. "chat_model" is what the Google and OpenAI
# conversation entities store; "model" is the more common spelling elsewhere.
MODEL_KEYS = ("chat_model", "model", "model_name")


@callback
def member_model(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the model configured for a member, or None if it cannot be read.

    A member's model may live on its config entry or, for providers that
    publish several entities from one account, on the subentry that created
    that particular entity. The subentry is checked first because it is the
    more specific of the two.
    """
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.config_entry_id is None:
        return None

    config_entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if config_entry is None:
        return None

    sources: list[Mapping[str, Any]] = []
    subentry_id = getattr(registry_entry, "config_subentry_id", None)
    if subentry_id and (subentry := config_entry.subentries.get(subentry_id)):
        sources.append(subentry.data)
    sources.append({**config_entry.data, **config_entry.options})

    for source in sources:
        for key in MODEL_KEYS:
            if value := source.get(key):
                return str(value)
    return None


@callback
def shared_models(models: Mapping[str, str | None]) -> dict[str, list[str]]:
    """Group members by model, keeping only the models used more than once.

    Members whose model could not be read are left out rather than lumped
    together: "unknown" is not evidence that two members match.
    """
    by_model: dict[str, list[str]] = {}
    for entity_id, model in models.items():
        if not model:
            continue
        by_model.setdefault(model, []).append(entity_id)
    return {model: members for model, members in by_model.items() if len(members) > 1}
