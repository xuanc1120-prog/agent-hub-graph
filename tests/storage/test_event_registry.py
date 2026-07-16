"""Tests for storage.event_registry — typed registration and validation."""

from __future__ import annotations

import pytest
from pydantic import Field

from protocol import StrictModel
from storage.errors import EventPayloadError
from storage.event_registry import MAX_PAYLOAD_BYTES, EventRegistry


class SamplePayload(StrictModel):
    action: str = Field(max_length=100)
    detail: str = Field(default="", max_length=1_000)


class OtherPayload(StrictModel):
    value: int = Field(ge=0)


class BigPayload(StrictModel):
    data: str = Field(max_length=100_000)


# --- Registration ----------------------------------------------------------


class TestRegistration:
    def test_register_and_get(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        assert reg.is_registered("test.event")
        assert reg.get_payload_class("test.event") is SamplePayload

    def test_duplicate_same_class_rejected(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        with pytest.raises(EventPayloadError, match="already registered"):
            reg.register("test.event", SamplePayload)

    def test_duplicate_different_class_rejected(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        with pytest.raises(EventPayloadError, match="already registered"):
            reg.register("test.event", OtherPayload)

    def test_empty_event_type_rejected(self) -> None:
        reg = EventRegistry()
        with pytest.raises(EventPayloadError, match=r"1\.\.128"):
            reg.register("", SamplePayload)

    def test_long_event_type_rejected(self) -> None:
        reg = EventRegistry()
        with pytest.raises(EventPayloadError, match=r"1\.\.128"):
            reg.register("x" * 129, SamplePayload)

    def test_non_strict_model_rejected(self) -> None:
        reg = EventRegistry()
        with pytest.raises(EventPayloadError, match="StrictModel subclass"):
            reg.register("test.event", dict)  # type: ignore[arg-type]

    def test_unknown_event_type(self) -> None:
        reg = EventRegistry()
        with pytest.raises(EventPayloadError, match="unknown event type"):
            reg.get_payload_class("nonexistent")

    def test_registered_types_snapshot(self) -> None:
        reg = EventRegistry()
        reg.register("a.b", SamplePayload)
        reg.register("c.d", OtherPayload)
        snapshot = reg.registered_types()
        assert len(snapshot) == 2
        assert snapshot["a.b"] is SamplePayload


# --- Payload validation (typed only) ---------------------------------------


class TestPayloadValidation:
    def test_validate_typed_payload(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        payload = SamplePayload(action="run")
        result = reg.validate_payload("test.event", payload)
        assert result is payload

    def test_validate_wrong_type_rejected(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        with pytest.raises(EventPayloadError, match="must be a"):
            reg.validate_payload("test.event", OtherPayload(value=1))  # type: ignore[arg-type]

    def test_validate_unknown_event_type(self) -> None:
        reg = EventRegistry()
        with pytest.raises(EventPayloadError, match="unknown event type"):
            reg.validate_payload("unknown", SamplePayload(action="x"))

    def test_payload_size_limit(self) -> None:
        reg = EventRegistry()
        reg.register("test.big", BigPayload)
        big = BigPayload(data="x" * (MAX_PAYLOAD_BYTES + 1))
        with pytest.raises(EventPayloadError, match="exceeding"):
            reg.validate_payload("test.big", big)


# --- Read revalidation -----------------------------------------------------


class TestReadRevalidation:
    def test_validate_payload_json_ok(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        json_str = '{"action":"run","detail":""}'
        result = reg.validate_payload_json("test.event", json_str)
        assert isinstance(result, SamplePayload)
        assert result.action == "run"

    def test_validate_payload_json_unknown_fields(self) -> None:
        reg = EventRegistry()
        reg.register("test.event", SamplePayload)
        json_str = '{"action":"run","unknown":42}'
        with pytest.raises(EventPayloadError, match="re-validation failed"):
            reg.validate_payload_json("test.event", json_str)
