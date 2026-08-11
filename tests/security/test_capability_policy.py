from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from protocol import PrivilegeAction
from security.path_policy import PathPolicy
from workflow.capability_broker import inspect_capability_resource, verify_capability_resource
from workflow.capability_policy import eligible_actions, is_eligible_resource


@pytest.mark.parametrize(
    "path",
    [
        "credentials.json",
        "auth.json",
        "service-account.json",
        "secret.json",
        "tokens.json",
        "private.json",
        "settings-security.xml",
        "config/credentials-prod.yaml",
        "config/private-key.pem",
        ".env.production",
        ".config/gh/hosts.yml",
        ".config/gcloud/application_default_credentials.json",
    ],
)
def test_capability_policy_denies_path_policy_secret_corpus(path: str) -> None:
    assert PathPolicy.is_sensitive_relative_path(path)
    assert eligible_actions(path) == frozenset()
    assert not is_eligible_resource(PrivilegeAction.EDIT_PROJECT_CONFIG, path)
    assert not is_eligible_resource(PrivilegeAction.EDIT_DEPENDENCY_MANIFEST, path)


def test_capability_policy_still_allows_a_non_sensitive_project_config() -> None:
    path = "config/settings.json"

    assert not PathPolicy.is_sensitive_relative_path(path)
    assert is_eligible_resource(PrivilegeAction.EDIT_PROJECT_CONFIG, path)


@pytest.mark.parametrize(
    "path",
    [
        "client-secret.json",
        "clientSecret.json",
        "oauth_token.json",
        "db-password.json",
        "prod-secrets/config.json",
        ".opencode/settings.json",
        ".codex/config.json",
        "opencode.json",
    ],
)
def test_capability_policy_denies_control_and_camel_case_credentials(path: str) -> None:
    assert eligible_actions(path) == frozenset()


@pytest.mark.parametrize(
    "path",
    ["auth-service/config.json", "token-parser/config.yaml", "private-api/settings.json"],
)
def test_capability_policy_does_not_treat_project_directory_names_as_credentials(
    path: str,
) -> None:
    assert is_eligible_resource(PrivilegeAction.EDIT_PROJECT_CONFIG, path)


def test_capability_resource_scan_rejects_secret_content_in_generic_config(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"apiKey": "REDACTED_TEST_VALUE"}', encoding="utf-8")

    with pytest.raises(ValueError, match="failed closed"):
        inspect_capability_resource(
            tmp_path,
            PrivilegeAction.EDIT_PROJECT_CONFIG,
            "config/settings.json",
        )


def test_capability_resource_scan_seals_safe_config_identity_and_content(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"name": "demo"}', encoding="utf-8")

    seal = inspect_capability_resource(
        tmp_path,
        PrivilegeAction.EDIT_PROJECT_CONFIG,
        "config/settings.json",
    )
    path.write_text('{"name": "changed"}', encoding="utf-8")

    with pytest.raises(ValueError, match="seal changed"):
        verify_capability_resource(
            tmp_path,
            PrivilegeAction.EDIT_PROJECT_CONFIG,
            "config/settings.json",
            seal.as_dict(),
        )


def test_capability_policy_imports_without_security_package_cycle() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import workflow.capability_policy"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
