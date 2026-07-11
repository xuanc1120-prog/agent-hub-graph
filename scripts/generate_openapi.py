"""Generate the phase-0 OpenAPI draft from the frozen protocol DTOs.

The FastAPI routes (``web/backend/``) do not exist yet, so this draft has no
``paths``. It freezes the *data* half of the API: ``components.schemas`` are the
JSON schemas of every request/response model in :mod:`protocol.api` plus the
graph/domain models they embed.

Run ``python -m scripts.generate_openapi`` to print the document, or
``--write`` to (re)write ``docs/contracts/openapi.draft.json``. The drift test
``tests/test_openapi_draft.py`` regenerates in memory and asserts byte-equality
with the committed file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, models_json_schema

from protocol import (
    CONTRACT_VERSION,
    ApprovalDecisionRequest,
    ApprovalRenewRequest,
    AssignAgentRequest,
    LayoutSaveRequest,
    LayoutSaveResponse,
    LockNodeRequest,
    MutationVersionResponse,
    PageInfo,
    PlanRequest,
    PlanResponse,
    RecoverWorkspaceRequest,
    RiskFinding,
    RunRequest,
    RunResponse,
    ValidateRequest,
    ValidateResponse,
    ValidationIssue,
    WorkflowSaveRequest,
    WorkflowSaveResponse,
    WsTicketResponse,
)

# Every frozen API DTO. Embedded models (AuthorGraph, CompiledGraph, WorkflowNode,
# ...) are pulled in automatically by the schema generator via $ref.
API_MODELS: tuple[type[BaseModel], ...] = (
    WorkflowSaveRequest,
    WorkflowSaveResponse,
    LayoutSaveRequest,
    LayoutSaveResponse,
    ValidateRequest,
    ValidateResponse,
    ValidationIssue,
    RunRequest,
    RunResponse,
    AssignAgentRequest,
    LockNodeRequest,
    MutationVersionResponse,
    ApprovalDecisionRequest,
    ApprovalRenewRequest,
    PlanRequest,
    PlanResponse,
    RecoverWorkspaceRequest,
    WsTicketResponse,
    RiskFinding,
    PageInfo,
)

OPENAPI_DRAFT_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "contracts" / "openapi.draft.json"
)


class _StableSchema(GenerateJsonSchema):
    """Deterministic ``$ref`` targets so the generated document is stable."""

    def normalize_name(self, name: str) -> str:
        return name


def build_openapi_document() -> dict[str, Any]:
    """Build the paths-free OpenAPI 3.1 draft from the frozen DTOs."""

    _, schema_bundle = models_json_schema(
        [(model, "validation") for model in API_MODELS],
        ref_template="#/components/schemas/{model}",
        schema_generator=_StableSchema,
    )
    schemas: dict[str, Any] = schema_bundle.get("$defs", {})

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Agent Hub API (phase-0 data contract draft)",
            "version": CONTRACT_VERSION,
            "description": (
                "Frozen v1 request/response data contracts (HUB-010). Paths and "
                "security schemes are added by HUB-400 when the FastAPI routes "
                "exist; only component schemas are frozen here."
            ),
        },
        "x-agent-hub-contract-version": CONTRACT_VERSION,
        "paths": {},
        "components": {"schemas": dict(sorted(schemas.items()))},
    }


def dump_openapi(document: dict[str, Any]) -> str:
    """Canonical, stable JSON text (sorted keys, trailing newline)."""

    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"write the draft to {OPENAPI_DRAFT_PATH} instead of printing it",
    )
    args = parser.parse_args()

    text = dump_openapi(build_openapi_document())
    if args.write:
        OPENAPI_DRAFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI_DRAFT_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {OPENAPI_DRAFT_PATH}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
