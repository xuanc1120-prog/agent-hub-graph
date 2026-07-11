"""Phase-0 OpenAPI draft drift guard.

The committed ``docs/contracts/openapi.draft.json`` is generated from the frozen
``protocol/api.py`` DTOs. This test regenerates the document in memory and
asserts byte-equality with the committed file, so the draft cannot silently
drift from the contract. Regenerate with ``python -m scripts.generate_openapi
--write`` when the DTOs intentionally change (which, post-freeze, requires an
ADR).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_openapi import OPENAPI_DRAFT_PATH, build_openapi_document, dump_openapi


def test_committed_openapi_draft_matches_generated() -> None:
    generated = dump_openapi(build_openapi_document())
    committed = OPENAPI_DRAFT_PATH.read_text(encoding="utf-8")
    assert committed == generated, (
        "docs/contracts/openapi.draft.json is stale; "
        "run `python -m scripts.generate_openapi --write`"
    )


def test_openapi_draft_is_schema_only_and_versioned() -> None:
    document = build_openapi_document()
    assert document["openapi"].startswith("3.1")
    assert document["x-agent-hub-contract-version"] == "1"
    # Phase-0: data-contract half only. Paths are added by the route owner
    # (HUB-400) when the FastAPI routes exist.
    assert document["paths"] == {}
    assert document["components"]["schemas"]


def test_openapi_draft_covers_core_api_dtos() -> None:
    schemas = build_openapi_document()["components"]["schemas"]
    for name in (
        "WorkflowSaveRequest",
        "ValidateResponse",
        "RunRequest",
        "ApprovalDecisionRequest",
        "AuthorGraph",
        "CompiledGraph",
    ):
        assert name in schemas


def test_committed_draft_is_valid_json() -> None:
    parsed = json.loads(OPENAPI_DRAFT_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)


def test_openapi_draft_path_is_under_docs_contracts() -> None:
    expected_parent = Path(__file__).resolve().parent.parent / "docs" / "contracts"
    assert OPENAPI_DRAFT_PATH.name == "openapi.draft.json"
    assert OPENAPI_DRAFT_PATH.parent == expected_parent
