"""Deterministic runtime risk classification for captured ChangeSets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from protocol.common import RiskLevel
from workspace.change_set import ChangeSetManifest, FileAction

_RISK_ORDER = {
    RiskLevel.L0: 0,
    RiskLevel.L1: 1,
    RiskLevel.L2: 2,
    RiskLevel.L3: 3,
    RiskLevel.L4: 4,
}
_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".bin",
        ".dll",
        ".dylib",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".pdf",
        ".png",
        ".so",
        ".tar",
        ".wasm",
        ".zip",
    }
)
_L3_COMPONENTS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "payment",
        "payments",
        "permission",
        "permissions",
        "security",
    }
)
_L2_COMPONENTS = frozenset(
    {
        ".github",
        "ci",
        "config",
        "configs",
        "migration",
        "migrations",
        "schema",
        "schemas",
        "script",
        "scripts",
        "test",
        "tests",
    }
)
_L2_BASENAMES = frozenset(
    {
        ".gitignore",
        "dockerfile",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "requirements.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_L4_COMPONENTS = frozenset({".agent-hub", ".git", ".ssh"})


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    runtime_risk: RiskLevel
    effective_risk: RiskLevel
    reasons: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.effective_risk == RiskLevel.L4

    @property
    def requires_approval(self) -> bool:
        return _RISK_ORDER[self.effective_risk] >= _RISK_ORDER[RiskLevel.L2]


class RiskClassifier:
    def classify_paths(self, paths: list[str] | tuple[str, ...]) -> RiskAssessment:
        unique_paths = tuple(dict.fromkeys(paths))
        if not unique_paths:
            return RiskAssessment(
                runtime_risk=RiskLevel.L0,
                effective_risk=RiskLevel.L0,
                reasons=(),
            )
        runtime = RiskLevel.L1
        reasons: list[str] = []
        for path in unique_paths:
            path_risk, reason = self._classify_path(path)
            runtime = _max_risk(runtime, path_risk)
            if reason is not None:
                reasons.append(reason)
        if len(unique_paths) > 20:
            runtime = _max_risk(runtime, RiskLevel.L3)
            reasons.append("more_than_20_changed_paths")
        elif len(unique_paths) >= 6:
            runtime = _max_risk(runtime, RiskLevel.L2)
            reasons.append("six_to_20_changed_paths")
        else:
            reasons.append("one_to_five_changed_paths")
        return RiskAssessment(
            runtime_risk=runtime,
            effective_risk=runtime,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def classify(
        self,
        manifest: ChangeSetManifest,
        *,
        policy_floor: RiskLevel = RiskLevel.L0,
        user_hint: RiskLevel = RiskLevel.L0,
        planner_hint: RiskLevel = RiskLevel.L0,
        agent_hint: RiskLevel = RiskLevel.L0,
        bypass_intent: bool = False,
    ) -> RiskAssessment:
        changed_paths = [
            path
            for change in manifest.changes
            for path in ((change.old_path, change.path) if change.old_path else (change.path,))
        ]
        reasons: list[str] = []
        runtime = RiskLevel.L0 if not changed_paths else RiskLevel.L1

        if bypass_intent:
            runtime = RiskLevel.L4
            reasons.append("runtime_policy_bypass_intent")

        for path in (*changed_paths, *manifest.ignored_files_touched):
            path_risk, reason = self._classify_path(path)
            runtime = _max_risk(runtime, path_risk)
            if reason is not None:
                reasons.append(reason)

        if manifest.ignored_files_touched:
            runtime = RiskLevel.L4
            reasons.append("ignored_workspace_mutation")

        if any(change.binary for change in manifest.changes):
            runtime = _max_risk(runtime, RiskLevel.L3)
            reasons.append("binary_change")
        if any(
            change.action in {FileAction.DELETED, FileAction.RENAMED} for change in manifest.changes
        ):
            runtime = _max_risk(runtime, RiskLevel.L3)
            reasons.append("delete_or_rename")

        count = len(manifest.changes)
        if count > 20:
            runtime = _max_risk(runtime, RiskLevel.L3)
            reasons.append("more_than_20_changed_paths")
        elif count >= 6:
            runtime = _max_risk(runtime, RiskLevel.L2)
            reasons.append("six_to_20_changed_paths")
        elif count:
            reasons.append("one_to_five_changed_paths")

        effective = runtime
        for source, risk in (
            ("policy_floor", policy_floor),
            ("user_hint", user_hint),
            ("planner_hint", planner_hint),
            ("agent_hint", agent_hint),
        ):
            if _RISK_ORDER[risk] > _RISK_ORDER[effective]:
                effective = risk
                reasons.append(f"{source}_raised_risk")

        return RiskAssessment(
            runtime_risk=runtime,
            effective_risk=effective,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _classify_path(path: str) -> tuple[RiskLevel, str | None]:
        pure = PurePosixPath(path)
        components = tuple(part.casefold() for part in pure.parts)
        basename = components[-1] if components else ""
        stem = pure.stem.casefold()
        suffix = pure.suffix.casefold()
        if (
            any(part in _L4_COMPONENTS for part in components)
            or basename == ".env"
            or basename.startswith(".env.")
            or basename in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
            or suffix in {".key", ".pem", ".p12", ".pfx"}
        ):
            return RiskLevel.L4, f"forbidden_or_sensitive_path:{path}"
        if (
            any(part in _L3_COMPONENTS for part in components)
            or stem in _L3_COMPONENTS
            or stem.startswith(("auth_", "payment_", "permission_", "security_"))
        ):
            return RiskLevel.L3, f"security_sensitive_component:{path}"
        if suffix in _BINARY_EXTENSIONS:
            return RiskLevel.L3, f"binary_extension:{path}"
        if (
            basename in _L2_BASENAMES
            or any(part in _L2_COMPONENTS for part in components)
            or basename.startswith(("dockerfile.", "requirements.", "constraints."))
        ):
            return RiskLevel.L2, f"configuration_or_test_path:{path}"
        return RiskLevel.L1, None


def _max_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right


__all__ = ["RiskAssessment", "RiskClassifier"]
