"""Typed event type registry with StrictModel payload validation.

Every ``event_type`` must be registered to exactly one :class:`StrictModel`
payload class.  Unknown event types, **all** duplicate registrations (even
with the same class), wrong payload models, and unknown fields in payloads
are rejected.  Payload canonical JSON must not exceed 65 536 UTF-8 bytes.
"""

from __future__ import annotations

from protocol import StrictModel
from storage.errors import EventPayloadError

# Maximum canonical JSON payload size in bytes (matches SQLite CHECK)
MAX_PAYLOAD_BYTES = 65_536


class EventRegistry:
    """Maps ``event_type`` strings to validated ``StrictModel`` payload classes.

    Usage::

        registry = EventRegistry()
        registry.register("task.started", TaskStartedPayload)
        registry.validate_payload("task.started", payload_dict_or_model)
    """

    def __init__(self) -> None:
        self._types: dict[str, type[StrictModel]] = {}

    def register(self, event_type: str, payload_class: type[StrictModel]) -> None:
        """Register a payload class for an event type.

        Raises :class:`EventPayloadError` if:
        - ``event_type`` is empty or too long.
        - ``payload_class`` is not a subclass of ``StrictModel``.
        - ``event_type`` is already registered (even to the same class).
        """
        if not event_type or len(event_type) > 128:
            raise EventPayloadError(
                f"event_type must contain 1..128 characters, got {len(event_type)}"
            )
        if not (isinstance(payload_class, type) and issubclass(payload_class, StrictModel)):
            raise EventPayloadError(
                f"payload_class must be a StrictModel subclass, got {payload_class!r}"
            )
        if event_type in self._types:
            existing = self._types[event_type]
            raise EventPayloadError(
                f"event_type '{event_type}' is already registered to "
                f"{existing.__name__}; cannot re-register to "
                f"{payload_class.__name__}"
            )
        self._types[event_type] = payload_class

    def is_registered(self, event_type: str) -> bool:
        """Check whether an event type has been registered."""
        return event_type in self._types

    def get_payload_class(self, event_type: str) -> type[StrictModel]:
        """Return the registered payload class.

        Raises :class:`EventPayloadError` if the event type is unknown.
        """
        cls = self._types.get(event_type)
        if cls is None:
            raise EventPayloadError(f"unknown event type: {event_type}")
        return cls

    def validate_payload(
        self,
        event_type: str,
        payload: StrictModel,
    ) -> StrictModel:
        """Validate a typed payload against the registered model.

        Only accepts a ``StrictModel`` instance — not dicts.  The caller
        must construct the correct model before calling this method.

        Raises :class:`EventPayloadError` if validation fails or if the
        canonical JSON exceeds :data:`MAX_PAYLOAD_BYTES`.
        """
        expected_cls = self.get_payload_class(event_type)

        if not isinstance(payload, expected_cls):
            raise EventPayloadError(
                f"payload must be a {expected_cls.__name__} instance, got {type(payload).__name__}"
            )

        # Canonical JSON size check
        from protocol import canonical_json

        canonical = canonical_json(payload)
        if len(canonical) > MAX_PAYLOAD_BYTES:
            raise EventPayloadError(
                f"event payload canonical JSON is {len(canonical)} bytes, "
                f"exceeding the {MAX_PAYLOAD_BYTES} byte limit"
            )

        return payload

    def validate_payload_json(
        self,
        event_type: str,
        payload_json: str,
    ) -> StrictModel:
        """Re-validate a payload JSON string read back from the database.

        Parses the JSON into the registered model and re-checks canonical
        size.  Used by the read path to detect DB drift.
        """
        expected_cls = self.get_payload_class(event_type)
        try:
            validated = expected_cls.model_validate_json(payload_json)
        except Exception as exc:
            raise EventPayloadError(
                f"payload re-validation failed for event type '{event_type}': {exc}"
            ) from exc

        from protocol import canonical_json

        canonical = canonical_json(validated)
        if len(canonical) > MAX_PAYLOAD_BYTES:
            raise EventPayloadError(
                f"re-validated payload canonical JSON is {len(canonical)} "
                f"bytes, exceeding the {MAX_PAYLOAD_BYTES} byte limit"
            )

        return validated

    def registered_types(self) -> dict[str, type[StrictModel]]:
        """Return a snapshot of all registered event types."""
        return dict(self._types)
