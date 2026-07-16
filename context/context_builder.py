"""ContextBuilder: construct a frozen :class:`ContextPack` from a ``TaskPackage``.

The builder copies ``effective_*`` scope verbatim from the ``TaskPackage``
(which is derived from the compiled graph snapshot - never from AuthorGraph
candidates).  It does **not** widen any permission or capability scope.

The prompt budget (``max_prompt_chars``) is **enforced** by measuring the
canonical JSON byte size of the complete :class:`ContextPack`.  If the size
exceeds the budget, trimmable items are deterministically removed from the
tail and recorded as :class:`TrimmedItem` entries.  Core required fields are
never trimmed; if they alone exceed the budget, a :class:`BudgetExceeded`
error is raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from protocol import (
    ArtifactRef,
    ContextPack,
    NodeSummary,
    TaskKind,
    TaskPackage,
    TitleText,
    canonical_json,
)


class BudgetExceeded(Exception):
    """Core required fields alone exceed the prompt budget."""


@dataclass(frozen=True, slots=True)
class TrimmedItem:
    """Record of an item omitted by budget trimming."""

    item_type: str
    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """Result of :meth:`ContextBuilder.build`."""

    pack: ContextPack
    trimmed: list[TrimmedItem]
    total_bytes: int


class ContextBuilder:
    """Build a :class:`ContextPack` from a ``TaskPackage`` and upstream inputs.

    Budget is measured as canonical JSON byte size of the final pack.
    Trimmable items are removed one-by-one from the tail until the pack
    fits within the budget.
    """

    def build(
        self,
        task: TaskPackage,
        *,
        session_goal: str,
        current_node_title: str,
        current_task: str,
        upstream_summaries: list[NodeSummary] | None = None,
        artifact_refs: list[ArtifactRef] | None = None,
        forbidden_paths: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        max_prompt_chars: int = 12_000,
    ) -> ContextBuildResult:
        """Build a :class:`ContextPack` with canonical byte budget enforcement.

        The budget is enforced on the canonical JSON byte size of the
        complete ContextPack, not on individual field character counts.
        """
        if max_prompt_chars < 1_000 or max_prompt_chars > 24_000:
            raise ValueError(f"max_prompt_chars must be 1000..24000, got {max_prompt_chars}")

        trimmed: list[TrimmedItem] = []

        upstream = list(upstream_summaries or [])
        refs = list(artifact_refs or [])
        forbidden = list(forbidden_paths or [])
        criteria = list(acceptance_criteria or [])

        # First attempt: build with all items
        pack = self._build_pack(
            task,
            session_goal,
            current_node_title,
            current_task,
            upstream,
            refs,
            forbidden,
            criteria,
            max_prompt_chars,
        )
        pack_bytes = len(canonical_json(pack))

        # If over budget, iteratively trim from tail
        # Trim order: artifact_refs, upstream_summaries, forbidden_paths, criteria
        trim_lists: list[tuple[list[Any], str, Any, Any]] = [
            (refs, "artifact_ref", lambda r: r.artifact_id, lambda r: r.artifact_id),
            (upstream, "upstream_summary", lambda s: s.node_run_id, lambda s: s.node_run_id),
            (forbidden, "forbidden_path", lambda s: s, lambda s: s),
            (criteria, "acceptance_criterion", lambda s: s, lambda s: s),
        ]

        for items, item_type, id_fn, _ in trim_lists:
            while pack_bytes > max_prompt_chars and items:
                removed = items.pop()
                trimmed.append(
                    TrimmedItem(
                        item_type=item_type,
                        item_id=str(id_fn(removed)),
                        reason=f"budget exceeded ({pack_bytes} > {max_prompt_chars})",
                    )
                )
                pack = self._build_pack(
                    task,
                    session_goal,
                    current_node_title,
                    current_task,
                    upstream,
                    refs,
                    forbidden,
                    criteria,
                    max_prompt_chars,
                )
                pack_bytes = len(canonical_json(pack))

        # If still over budget with everything trimmed, check required fields
        if pack_bytes > max_prompt_chars:
            # Build minimal pack (no trimmable items)
            minimal = self._build_pack(
                task,
                session_goal,
                current_node_title,
                current_task,
                [],
                [],
                [],
                [],
                max_prompt_chars,
            )
            minimal_bytes = len(canonical_json(minimal))
            if minimal_bytes > max_prompt_chars:
                raise BudgetExceeded(
                    f"required fields alone produce {minimal_bytes} bytes, "
                    f"exceeding budget {max_prompt_chars}"
                )

        return ContextBuildResult(pack=pack, trimmed=trimmed, total_bytes=pack_bytes)

    @staticmethod
    def _build_pack(
        task: TaskPackage,
        session_goal: str,
        current_node_title: str,
        current_task: str,
        upstream: list[NodeSummary],
        refs: list[ArtifactRef],
        forbidden: list[str],
        criteria: list[str],
        max_prompt_chars: int,
    ) -> ContextPack:
        return ContextPack(
            task_id=task.task_id,
            node_id=task.node_id,
            task_kind=TaskKind(task.task_kind.value),
            session_goal=session_goal,
            current_node_title=TitleText(current_node_title),
            current_task=current_task,
            upstream_summaries=upstream,
            artifact_refs=refs,
            effective_allowed_files=list(task.effective_allowed_files),
            effective_new_files=list(task.effective_new_files),
            active_capability_grant_id=task.active_capability_grant_id,
            granted_existing_files=list(task.granted_existing_files),
            effective_allowed_commands=list(task.effective_allowed_commands),
            forbidden_paths=forbidden,
            acceptance_criteria=criteria,
            max_prompt_chars=max_prompt_chars,
        )

    @staticmethod
    def assert_no_scope_widening(
        pack: ContextPack,
        task: TaskPackage,
    ) -> None:
        """Verify the pack did not widen any effective_* scope."""
        pack_files = set(pack.effective_allowed_files)
        task_files = set(task.effective_allowed_files)
        if not pack_files.issubset(task_files):
            raise ValueError("ContextPack effective_allowed_files widens TaskPackage scope")

        pack_new = set(pack.effective_new_files)
        task_new = set(task.effective_new_files)
        if not pack_new.issubset(task_new):
            raise ValueError("ContextPack effective_new_files widens TaskPackage scope")

        pack_cmds = [tuple(c) for c in pack.effective_allowed_commands]
        task_cmds = [tuple(c) for c in task.effective_allowed_commands]
        if not set(pack_cmds).issubset(set(task_cmds)):
            raise ValueError("ContextPack effective_allowed_commands widens TaskPackage scope")

        if pack.active_capability_grant_id != task.active_capability_grant_id:
            raise ValueError("ContextPack capability grant id differs from TaskPackage")

        pack_granted = set(pack.granted_existing_files)
        task_granted = set(task.granted_existing_files)
        if pack_granted != task_granted:
            raise ValueError("ContextPack granted_existing_files differs from TaskPackage")
