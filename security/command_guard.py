"""Parameter-level allowlist for Master-owned test command templates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_SHELL_METACHARACTERS = frozenset(";&|><`\r\n")
_SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9_./:+@=-]{1,512}$")
_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "--quiet",
        "-x",
        "--strict-config",
        "--strict-markers",
        "--disable-warnings",
    }
)
_NPM_FORWARDED_FLAGS = frozenset(
    {
        "--passWithNoTests",
        "--run",
        "--runInBand",
        "--watch=false",
    }
)


class CommandGuardViolation(ValueError):
    """An argv vector is not one of the registered safe templates."""


@dataclass(frozen=True, slots=True)
class ApprovedCommand:
    template_id: str
    argv: tuple[str, ...]


class CommandGuard:
    """Validate argv arrays without invoking a shell or resolving executables."""

    def validate(self, argv: Sequence[str]) -> ApprovedCommand:
        normalized = self._normalize(argv)
        executable = normalized[0].casefold()
        if executable == "pytest":
            self._validate_pytest(normalized[1:])
            return ApprovedCommand("pytest", normalized)
        if executable in {"python", "python3"}:
            if len(normalized) < 3 or normalized[1:3] != ("-m", "pytest"):
                raise CommandGuardViolation("python is only allowed as 'python -m pytest'")
            self._validate_pytest(normalized[3:])
            return ApprovedCommand("python-pytest", normalized)
        if executable in {"npm", "pnpm"}:
            self._validate_package_test(normalized)
            return ApprovedCommand(f"{executable}-test", normalized)
        if executable == "go":
            self._validate_go_test(normalized)
            return ApprovedCommand("go-test", normalized)
        raise CommandGuardViolation(f"unregistered test executable: {normalized[0]}")

    def validate_many(
        self,
        commands: Iterable[Sequence[str]],
        *,
        max_commands: int = 20,
    ) -> tuple[ApprovedCommand, ...]:
        candidates = tuple(commands)
        if len(candidates) > max_commands:
            raise CommandGuardViolation(f"command list exceeds {max_commands} entries")
        approved: list[ApprovedCommand] = []
        seen: set[tuple[str, ...]] = set()
        for candidate in candidates:
            command = self.validate(candidate)
            if command.argv in seen:
                raise CommandGuardViolation("duplicate command templates are not allowed")
            seen.add(command.argv)
            approved.append(command)
        return tuple(approved)

    @staticmethod
    def _normalize(argv: Sequence[str]) -> tuple[str, ...]:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise CommandGuardViolation("command must be an argv sequence, not a shell string")
        if not 1 <= len(argv) <= 64:
            raise CommandGuardViolation("argv must contain 1..64 tokens")
        normalized: list[str] = []
        for token in argv:
            if not isinstance(token, str) or not 1 <= len(token) <= 4_096:
                raise CommandGuardViolation("every argv token must be a bounded string")
            if (
                "\0" in token
                or "$(" in token
                or any(character in _SHELL_METACHARACTERS for character in token)
            ):
                raise CommandGuardViolation("shell syntax is not allowed in argv tokens")
            normalized.append(token)
        executable = normalized[0]
        if (
            executable.startswith(("-", "@"))
            or "/" in executable
            or "\\" in executable
            or ":" in executable
        ):
            raise CommandGuardViolation("executable must be a registered bare name")
        return tuple(normalized)

    def _validate_pytest(self, args: tuple[str, ...]) -> None:
        for token in args:
            if token in _PYTEST_FLAGS:
                continue
            if token.startswith("--maxfail="):
                value = token.removeprefix("--maxfail=")
                if value.isdigit() and 1 <= int(value) <= 20:
                    continue
            if token.startswith("--tb=") and token.removeprefix("--tb=") in {
                "short",
                "line",
                "native",
                "no",
            }:
                continue
            if token.startswith("--color=") and token.removeprefix("--color=") in {
                "yes",
                "no",
                "auto",
            }:
                continue
            if token.startswith("-"):
                raise CommandGuardViolation(f"pytest argument is not allowlisted: {token}")
            self._validate_test_selector(token)

    def _validate_package_test(self, argv: tuple[str, ...]) -> None:
        if len(argv) < 2 or argv[1] != "test":
            raise CommandGuardViolation("package managers are only allowed with the test script")
        if len(argv) == 2:
            return
        if argv[2] != "--":
            raise CommandGuardViolation("test script arguments must follow an explicit '--'")
        for token in argv[3:]:
            if token in _NPM_FORWARDED_FLAGS:
                continue
            if token.startswith("-"):
                raise CommandGuardViolation(f"test runner argument is not allowlisted: {token}")
            self._validate_test_selector(token)

    def _validate_go_test(self, argv: tuple[str, ...]) -> None:
        if len(argv) < 3 or argv[1] != "test":
            raise CommandGuardViolation("go is only allowed with the test subcommand")
        package_seen = False
        for token in argv[2:]:
            if token == "-count=1":
                continue
            if token.startswith("-"):
                raise CommandGuardViolation(f"go test argument is not allowlisted: {token}")
            if package_seen:
                raise CommandGuardViolation("go test accepts one package selector in demo mode")
            if token == "./..." or (
                token.startswith("./")
                and token.endswith("/...")
                and self._safe_relative_selector(token[2:-4])
            ):
                package_seen = True
                continue
            raise CommandGuardViolation("go test package must be './...' or './path/...'")
        if not package_seen:
            raise CommandGuardViolation("go test requires an explicit package selector")

    def _validate_test_selector(self, selector: str) -> None:
        path = selector.split("::", 1)[0]
        if ":" in path or not self._safe_relative_selector(path):
            raise CommandGuardViolation(f"unsafe test selector: {selector}")
        lowered = path.casefold()
        basename = lowered.rsplit("/", 1)[-1]
        if (
            lowered == "."
            or lowered == "tests"
            or lowered.startswith("tests/")
            or basename.startswith("test_")
            or basename.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
        ):
            return
        raise CommandGuardViolation(f"test selector is outside registered test paths: {selector}")

    @staticmethod
    def _safe_relative_selector(selector: str) -> bool:
        if not selector or not _SAFE_SELECTOR.fullmatch(selector):
            return False
        if selector.startswith(("/", "\\", "-")) or "\\" in selector:
            return False
        parts = selector.split("/")
        return not any(part in {"", ".."} for part in parts)


__all__ = ["ApprovedCommand", "CommandGuard", "CommandGuardViolation"]
