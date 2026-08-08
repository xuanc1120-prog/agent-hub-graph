from __future__ import annotations

import pytest

from security.command_guard import CommandGuard, CommandGuardViolation


@pytest.mark.parametrize(
    ("argv", "template_id"),
    [
        (["pytest"], "pytest"),
        (["pytest", "-q", "tests/test_api.py"], "pytest"),
        (["python", "-m", "pytest", "--maxfail=2"], "python-pytest"),
        (["npm", "test"], "npm-test"),
        (["npm", "test", "--", "login.test.ts"], "npm-test"),
        (["pnpm", "test", "--", "--run", "tests/login.spec.ts"], "pnpm-test"),
        (["go", "test", "./..."], "go-test"),
    ],
)
def test_allows_registered_parameter_level_templates(
    argv: list[str],
    template_id: str,
) -> None:
    approved = CommandGuard().validate(argv)

    assert approved.argv == tuple(argv)
    assert approved.template_id == template_id


@pytest.mark.parametrize(
    "argv",
    [
        "pytest -q",
        ["rm", "-rf", "."],
        ["git", "push"],
        ["cat", ".env"],
        ["python", "-c", "print('unsafe')"],
        ["pytest", "-s"],
        ["pytest", "--pdb"],
        ["pytest", "../../outside.py"],
        ["pytest", "src/app.py"],
        ["npm", "install"],
        ["npm", "test", "--", "--inspect"],
        ["go", "test", "/tmp/..."],
        ["pytest", "tests/test_api.py;git push"],
        ["pytest", "tests/test_api.py:secret"],
        [r"C:\Python\python.exe", "-m", "pytest"],
    ],
)
def test_rejects_shell_unregistered_and_unsafe_arguments(argv: object) -> None:
    with pytest.raises(CommandGuardViolation):
        CommandGuard().validate(argv)  # type: ignore[arg-type]


def test_rejects_duplicate_and_excessive_command_lists() -> None:
    guard = CommandGuard()

    with pytest.raises(CommandGuardViolation, match="duplicate"):
        guard.validate_many([["pytest"], ["pytest"]])
    with pytest.raises(CommandGuardViolation, match="exceeds 1"):
        guard.validate_many([["pytest"], ["npm", "test"]], max_commands=1)
