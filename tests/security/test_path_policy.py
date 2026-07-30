from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from security.path_policy import PathPolicy, PathPolicyViolation


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return root


def test_validates_separate_existing_and_new_exact_scope(repo: Path) -> None:
    scope = PathPolicy(repo).validate_scope(
        ["src/app.py"],
        ["src/new_module.py"],
    )

    assert scope.existing_files == ("src/app.py",)
    assert scope.new_files == ("src/new_module.py",)
    assert scope.all_files == ("src/app.py", "src/new_module.py")


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside.py",
        "/absolute.py",
        "C:/absolute.py",
        r"\\server\share\file.py",
        "src/*.py",
        "src\\app.py",
        "src/CON.txt",
        "src/file.py:secret",
        "src/trailing. ",
        ".git/config",
        ".agent-hub/policy.json",
        ".ssh/config",
        ".gitattributes",
        ".gitmodules",
        ".opencode/config.json",
        ".claude/settings.json",
        "AGENTS.md",
        "CLAUDE.md",
        "opencode.json",
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".yarnrc.yml",
        ".aws/credentials",
        ".azure/accessTokens.json",
        ".docker/config.json",
        ".kube/config",
        ".config/gcloud/application_default_credentials.json",
        "config/.env.production",
        "keys/private.pem",
        "keys/id_rsa",
        "NuGet.Config",
        "pip.conf",
    ],
)
def test_rejects_escape_ambiguous_metadata_and_sensitive_paths(
    repo: Path,
    candidate: str,
) -> None:
    with pytest.raises(PathPolicyViolation):
        PathPolicy(repo).validate_new(candidate)


def test_rejects_noncanonical_unicode(repo: Path) -> None:
    decomposed = unicodedata.normalize("NFD", "src/cafe\u0301.py")
    assert unicodedata.normalize("NFC", decomposed) != decomposed

    with pytest.raises(PathPolicyViolation, match="NFC"):
        PathPolicy(repo).validate_new(decomposed)


def test_rejects_existing_and_new_casefold_overlap(repo: Path) -> None:
    candidate = "SRC/APP.PY" if os.name == "nt" else "src/app.py"

    with pytest.raises(PathPolicyViolation, match="duplicate"):
        PathPolicy(repo).validate_scope(["src/app.py"], [candidate])


def test_rejects_directory_scope(repo: Path) -> None:
    with pytest.raises(PathPolicyViolation, match="regular file"):
        PathPolicy(repo).validate_existing("src")


def test_rejects_hardlinked_existing_file(repo: Path) -> None:
    source = repo / "src" / "app.py"
    alias = repo / "src" / "alias.py"
    os.link(source, alias)

    with pytest.raises(PathPolicyViolation, match="hard-linked"):
        PathPolicy(repo).validate_existing("src/app.py")


def test_scope_limit_is_fail_closed(repo: Path) -> None:
    with pytest.raises(PathPolicyViolation, match="exceeds 1"):
        PathPolicy(repo, max_scope_files=1).validate_scope(
            ["src/app.py"],
            ["src/new.py"],
        )


def test_cleanup_path_can_remove_forbidden_control_file_without_granting_scope(
    repo: Path,
) -> None:
    control = repo / ".gitattributes"
    control.write_text("*.py filter=unsafe\n", encoding="utf-8")
    policy = PathPolicy(repo)

    validated = policy.validate_cleanup_path(".gitattributes")

    assert validated.absolute_path == control
    with pytest.raises(PathPolicyViolation, match="control files"):
        policy.validate_existing(".gitattributes")
