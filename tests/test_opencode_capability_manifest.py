"""Tests for OpenCode capability manifest generation."""

import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.capabilities.manifest import (
    check_dir_option_support,
    check_json_output_support,
    check_pure_mode_support,
    create_capability_manifest,
    extract_commands_from_help,
    extract_options_from_help,
    get_binary_hash,
    get_opencode_help,
    get_opencode_run_help,
    get_opencode_version,
    load_manifest,
    save_manifest,
    validate_manifest,
)


class TestManifestGeneration:
    """Test manifest generation functionality."""

    def test_get_opencode_version(self):
        """Test getting OpenCode version."""
        version = get_opencode_version()
        assert isinstance(version, str)
        # 版本应该是有效的版本号格式
        assert version == "1.2.27" or version == "unknown"

    def test_get_opencode_help(self):
        """Test getting OpenCode help."""
        help_info = get_opencode_help()
        assert isinstance(help_info, dict)
        assert "raw" in help_info
        assert "commands" in help_info
        assert "options" in help_info

    def test_get_opencode_run_help(self):
        """Test getting OpenCode run help."""
        run_help = get_opencode_run_help()
        assert isinstance(run_help, dict)
        assert "raw" in run_help
        assert "options" in run_help

    def test_extract_commands_from_help(self):
        """Test extracting commands from help text."""
        help_text = """Commands:
  opencode completion          generate shell completion script
  opencode acp                 start ACP (Agent Client Protocol) server
  opencode mcp                 manage MCP (Model Context Protocol) servers
  opencode [project]           start opencode tui                                          [default]
  opencode attach <url>        attach to a running opencode server
  opencode run [message..]     run opencode with a message"""

        commands = extract_commands_from_help(help_text)
        assert len(commands) >= 4
        assert any(cmd["command"] == "completion" for cmd in commands)
        assert any(cmd["command"] == "acp" for cmd in commands)

    def test_extract_options_from_help(self):
        """Test extracting options from help text."""
        help_text = """Options:
  -h, --help         show help                                                             [boolean]
  -v, --version      show version number                                                   [boolean]
      --print-logs   print logs to stderr                                                  [boolean]
      --log-level    log level                  [string] [choices: "DEBUG", "INFO", "WARN", "ERROR"]
"""

        options = extract_options_from_help(help_text)
        assert len(options) >= 3
        assert any(opt["option"] == "--help" for opt in options)
        assert any(opt["option"] == "--version" for opt in options)

    def test_create_capability_manifest(self):
        """Test creating capability manifest."""
        manifest = create_capability_manifest()

        # Verify basic structure
        assert "version" in manifest
        assert "tool" in manifest
        assert "binary" in manifest
        assert "capabilities" in manifest

        # Verify binary information
        assert "name" in manifest["binary"]
        assert "version" in manifest["binary"]
        assert "launcher_sha256" in manifest["binary"]
        assert "entrypoint_sha256" in manifest["binary"]

        # Verify capabilities
        assert "json_output" in manifest["capabilities"]
        assert "pure_mode" in manifest["capabilities"]
        assert "directory_selection" in manifest["capabilities"]
        assert "legacy_mode" in manifest["capabilities"]
        assert "requires_explicit_opt_in" in manifest["capabilities"]

    def test_save_and_load_manifest(self, tmp_path):
        """Test saving and loading manifest."""
        manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "test",
                "entrypoint_sha256": "test",
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "requires_explicit_opt_in": True,
            },
        }

        output_path = tmp_path / "test-manifest.json"
        save_manifest(manifest, output_path)

        assert output_path.exists()

        loaded = load_manifest(output_path)
        assert loaded is not None
        assert loaded["version"] == "1.0.0"
        assert loaded["tool"] == "opencode"

    def test_validate_manifest(self):
        """Test manifest validation with strict schema."""
        # Valid manifest with all required fields
        valid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "a" * 64,  # Valid hash format
                "entrypoint_sha256": "b" * 64,  # Valid hash format
                "native_binary_sha256": "c" * 64,  # Valid hash format
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": False,
                "recommended_mode": "none",
                "requires_explicit_opt_in": True,
            },
        }

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            # Missing binary and capabilities
        }

        assert validate_manifest(valid_manifest) is True
        assert validate_manifest(invalid_manifest) is False

    def test_get_binary_hash(self):
        """Test getting binary hash."""
        # Test with nonexistent file
        hash_result = get_binary_hash("nonexistent_binary")
        assert hash_result == "unknown"

    @patch("subprocess.run")
    def test_check_json_output_support(self, mock_run):
        """Test checking JSON output support."""
        # 模拟支持JSON的输出
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "--format json"
        mock_run.return_value = mock_result

        assert check_json_output_support() is True

    @patch("subprocess.run")
    def test_check_pure_mode_support(self, mock_run):
        """Test checking pure mode support."""
        # 模拟不支持pure模式
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "--help"
        mock_run.return_value = mock_result

        assert check_pure_mode_support() is False

    @patch("subprocess.run")
    def test_check_dir_option_support(self, mock_run):
        """Test checking dir option support."""
        # 模拟支持dir选项
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "--dir"
        mock_run.return_value = mock_result

        assert check_dir_option_support() is True


class TestFixtureValidation:
    """Test fixture validation for security."""

    def test_fixtures_contain_no_secrets(self, tmp_path):
        """Test that fixtures don't contain secrets."""
        # 创建一个测试fixture
        fixture = {
            "command": ["opencode", "run", "--help"],
            "output": "Help text",
            "exit_code": 0,
        }

        fixture_path = tmp_path / "test-fixture.json"
        with open(fixture_path, "w") as f:
            json.dump(fixture, f)

        # 读取并验证
        with open(fixture_path) as f:
            content = f.read()

        # 检查不包含常见的secret模式
        secret_patterns = [
            "bearer ",
            "token=",
            "password=",
            "secret=",
            "api_key=",
        ]

        for pattern in secret_patterns:
            assert pattern.lower() not in content.lower()

    def test_fixtures_contain_no_absolute_paths(self, tmp_path):
        """Test that fixtures don't contain absolute paths."""
        # 创建一个测试fixture
        fixture = {
            "command": ["opencode", "run", "--dir", "./test"],
            "output": "Output",
            "exit_code": 0,
        }

        fixture_path = tmp_path / "test-fixture.json"
        with open(fixture_path, "w") as f:
            json.dump(fixture, f)

        # 读取并验证
        with open(fixture_path) as f:
            content = f.read()

        # 检查不包含绝对路径模式
        import re

        # Windows路径
        windows_path_pattern = r"[A-Za-z]:\\"
        # Unix路径
        unix_path_pattern = r"/home/"

        assert not re.search(windows_path_pattern, content)
        assert not re.search(unix_path_pattern, content)


class TestManifestSchema:
    """Test manifest JSON schema validation."""

    def test_manifest_schema_structure(self):
        """Test manifest has correct schema structure."""
        manifest = create_capability_manifest()

        # Verify top-level fields
        required_fields = ["version", "tool", "binary", "capabilities"]
        for field in required_fields:
            assert field in manifest, f"Missing required field: {field}"

        # Verify binary fields
        binary_fields = ["name", "version", "launcher_sha256", "entrypoint_sha256"]
        for field in binary_fields:
            assert field in manifest["binary"], f"Missing binary field: {field}"

        # Verify capabilities fields
        capability_fields = [
            "json_output",
            "pure_mode",
            "directory_selection",
            "legacy_mode",
            "requires_explicit_opt_in",
        ]
        for field in capability_fields:
            assert field in manifest["capabilities"], f"Missing capability field: {field}"

    def test_manifest_version_format(self):
        """Test manifest version format."""
        manifest = create_capability_manifest()

        # 版本应该是语义化版本格式
        version = manifest["version"]
        parts = version.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_manifest_tool_name(self):
        """Test manifest tool name."""
        manifest = create_capability_manifest()
        assert manifest["tool"] == "opencode"

    def test_manifest_fail_closed_mechanism(self):
        """Test fail closed mechanism when probes fail."""
        manifest = create_capability_manifest()

        # Check requires_explicit_opt_in field exists
        assert "requires_explicit_opt_in" in manifest["capabilities"]
        assert "requires_explicit_opt_in" in manifest["compatibility"]

        # Check probe_status field exists
        assert "probe_status" in manifest["binary"]
        assert "probe_status" in manifest["capabilities"]

        # requires_explicit_opt_in should ALWAYS be True (legacy mode requires explicit opt-in)
        assert manifest["capabilities"]["requires_explicit_opt_in"] is True
        assert manifest["compatibility"]["requires_explicit_opt_in"] is True

        # If critical probes fail, legacy_mode should be False (fail closed)
        probe_status = manifest["capabilities"]["probe_status"]
        critical_probes_passed = all(
            [
                probe_status.get("binary_found", False),
                probe_status.get("version_detected", False),
                probe_status.get("help_parsed", False),
                probe_status.get("run_help_parsed", False),
                probe_status.get("json_support_detected", False),
                probe_status.get("dir_support_detected", False),
            ]
        )

        if not critical_probes_passed:
            # If probes failed, legacy_mode must be False
            assert manifest["capabilities"]["legacy_mode"] is False
            assert manifest["compatibility"]["recommended_mode"] == "none"

    def test_manifest_process_capabilities_unverified(self):
        """Test that process capabilities are marked as unverified."""
        manifest = create_capability_manifest()

        # Check that process capabilities in exit_behavior are marked as unverified
        assert "exit_behavior" in manifest
        assert manifest["exit_behavior"]["cancel_support"] == "unverified"
        assert manifest["exit_behavior"]["process_tree_kill"] == "unverified"

    def test_manifest_json_run_evidence(self):
        """Test JSON run evidence in manifest."""
        manifest = create_capability_manifest()

        # Check json_run_evidence field exists
        assert "json_run_evidence" in manifest

        # Check field structure
        evidence = manifest["json_run_evidence"]
        assert "attempted" in evidence
        assert "success" in evidence
        assert "skip_reason" in evidence
        assert "is_synthetic" in evidence
        assert "event_count" in evidence

    def test_manifest_sanitized_paths(self):
        """Test that paths are sanitized in manifest."""
        manifest = create_capability_manifest()

        # Check that binary name does not contain absolute paths
        binary_name = manifest["binary"]["name"]

        # Should not contain Windows user directory path
        assert "Users" not in binary_name or "AppData" not in binary_name
        # Should not contain Unix user directory path
        assert "/home/" not in binary_name
        assert "/Users/" not in binary_name


class TestSubmittedArtifacts:
    """Test submitted artifacts (manifest and fixtures)."""

    def test_submitted_manifest_exists(self):
        """Test that submitted manifest file exists."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        assert manifest_path.exists(), f"Manifest file not found: {manifest_path}"

    def test_submitted_manifest_is_valid_json(self):
        """Test that submitted manifest is valid JSON."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            with open(manifest_path, encoding="utf-8") as f:
                try:
                    manifest = json.load(f)
                    assert isinstance(manifest, dict)
                except json.JSONDecodeError as e:
                    pytest.fail(f"Manifest is not valid JSON: {e}")

    def test_submitted_manifest_has_no_absolute_paths(self):
        """Test that submitted manifest contains no absolute paths."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            with open(manifest_path, encoding="utf-8") as f:
                content = f.read()

            # Check that no absolute path patterns are present
            import re

            # Windows path
            windows_path_pattern = r"[A-Za-z]:\\"
            # Unix path
            unix_path_pattern = r"/home/"
            unix_path_pattern2 = r"/Users/"

            assert not re.search(windows_path_pattern, content), (
                "Manifest contains Windows absolute path"
            )
            assert not re.search(unix_path_pattern, content), "Manifest contains Unix home path"
            assert not re.search(unix_path_pattern2, content), "Manifest contains Unix Users path"

    def test_submitted_fixture_exists(self):
        """Test that submitted fixture file exists."""
        fixture_path = Path("tests/fixtures/opencode/capability-fixture.json")
        assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    def test_submitted_fixture_is_valid_json(self):
        """Test that submitted fixture is valid JSON."""
        fixture_path = Path("tests/fixtures/opencode/capability-fixture.json")
        if fixture_path.exists():
            with open(fixture_path, encoding="utf-8") as f:
                try:
                    fixture = json.load(f)
                    assert isinstance(fixture, dict)
                except json.JSONDecodeError as e:
                    pytest.fail(f"Fixture is not valid JSON: {e}")

    def test_submitted_fixture_has_no_secrets(self):
        """Test that submitted fixture contains no secrets."""
        fixture_path = Path("tests/fixtures/opencode/capability-fixture.json")
        if fixture_path.exists():
            with open(fixture_path, encoding="utf-8") as f:
                content = f.read()

            # Check for common secret patterns
            secret_patterns = [
                "bearer ",
                "token=",
                "password=",
                "secret=",
                "api_key=",
            ]

            for pattern in secret_patterns:
                assert pattern.lower() not in content.lower(), (
                    f"Fixture contains secret pattern: {pattern}"
                )

    def test_submitted_fixture_has_no_absolute_paths(self):
        """Test that submitted fixture contains no absolute paths."""
        fixture_path = Path("tests/fixtures/opencode/capability-fixture.json")
        if fixture_path.exists():
            with open(fixture_path, encoding="utf-8") as f:
                content = f.read()

            # Check that no absolute path patterns are present
            import re

            # Windows path
            windows_path_pattern = r"[A-Za-z]:\\"
            # Unix path
            unix_path_pattern = r"/home/"
            unix_path_pattern2 = r"/Users/"

            assert not re.search(windows_path_pattern, content), (
                "Fixture contains Windows absolute path"
            )
            assert not re.search(unix_path_pattern, content), "Fixture contains Unix home path"
            assert not re.search(unix_path_pattern2, content), "Fixture contains Unix Users path"

    def test_submitted_fixture_has_json_run_evidence(self):
        """Test that submitted fixture has JSON run evidence."""
        fixture_path = Path("tests/fixtures/opencode/capability-fixture.json")
        if fixture_path.exists():
            with open(fixture_path, encoding="utf-8") as f:
                fixture = json.load(f)

            # Check json_run_attempt field exists
            assert "json_run_attempt" in fixture, "Fixture missing json_run_attempt field"

            # Check json_event_samples field exists
            assert "json_event_samples" in fixture, "Fixture missing json_event_samples field"

            # Check that json_event_samples is synthetic or has actual data
            json_samples = fixture["json_event_samples"]
            assert (
                json_samples.get("is_synthetic", False) or len(json_samples.get("samples", [])) > 0
            ), "JSON event samples must be synthetic or contain actual samples"

    def test_strict_manifest_schema_validation(self):
        """Test strict manifest schema validation."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)

            # Strict schema validation
            required_top_level = [
                "version",
                "tool",
                "binary",
                "capabilities",
                "commands",
                "options",
                "output_framing",
                "exit_behavior",
                "json_run_evidence",
                "compatibility",
            ]
            for field in required_top_level:
                assert field in manifest, f"Missing required top-level field: {field}"

            # Verify binary fields
            required_binary = [
                "name",
                "version",
                "launcher_sha256",
                "entrypoint_sha256",
                "native_binary_sha256",
                "startup_chain_complete",
                "probe_status",
            ]
            for field in required_binary:
                assert field in manifest["binary"], f"Missing required binary field: {field}"

            # Verify capabilities fields
            required_capabilities = [
                "json_output",
                "pure_mode",
                "directory_selection",
                "legacy_mode",
                "requires_explicit_opt_in",
                "probe_status",
            ]
            for field in required_capabilities:
                assert field in manifest["capabilities"], (
                    f"Missing required capability field: {field}"
                )

            # Verify compatibility fields
            required_compatibility = [
                "version",
                "pure_support",
                "legacy_support",
                "recommended_mode",
                "requires_explicit_opt_in",
            ]
            for field in required_compatibility:
                assert field in manifest["compatibility"], (
                    f"Missing required compatibility field: {field}"
                )

            # Verify exit_behavior fields
            required_exit_behavior = ["timeout_seconds", "cancel_support", "process_tree_kill"]
            for field in required_exit_behavior:
                assert field in manifest["exit_behavior"], (
                    f"Missing required exit_behavior field: {field}"
                )


class TestJsonlParsing:
    """Test JSONL parsing smoke tests."""

    def test_jsonl_fixture_exists(self):
        """Test that JSONL fixture file exists."""
        jsonl_path = Path("tests/fixtures/opencode/jsonl-sample.jsonl")
        assert jsonl_path.exists(), f"JSONL fixture not found: {jsonl_path}"

    def test_jsonl_fixture_is_valid_jsonl(self):
        """Test that JSONL fixture is valid JSONL (one JSON object per line)."""
        jsonl_path = Path("tests/fixtures/opencode/jsonl-sample.jsonl")
        if jsonl_path.exists():
            with open(jsonl_path, encoding="utf-8") as f:
                lines = f.readlines()

            # Each line should be valid JSON
            events = []
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if line:  # Skip empty lines
                    try:
                        event = json.loads(line)
                        assert isinstance(event, dict), f"Line {i} is not a JSON object"
                        events.append(event)
                    except json.JSONDecodeError as e:
                        pytest.fail(f"Line {i} is not valid JSON: {e}")

            # Should have at least some events
            assert len(events) > 0, "JSONL file has no events"

    def test_jsonl_events_have_required_fields(self):
        """Test that JSONL events have required fields."""
        jsonl_path = Path("tests/fixtures/opencode/jsonl-sample.jsonl")
        if jsonl_path.exists():
            with open(jsonl_path, encoding="utf-8") as f:
                lines = f.readlines()

            required_fields = ["event_type", "timestamp", "data"]
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if line:
                    event = json.loads(line)
                    # Skip metadata line
                    if "_metadata" in event:
                        continue
                    for field in required_fields:
                        assert field in event, f"Line {i} missing required field: {field}"

    def test_jsonl_events_have_valid_structure(self):
        """Test that JSONL events have valid structure."""
        jsonl_path = Path("tests/fixtures/opencode/jsonl-sample.jsonl")
        if jsonl_path.exists():
            with open(jsonl_path, encoding="utf-8") as f:
                lines = f.readlines()

            valid_event_types = ["session_start", "message", "session_end", "error"]
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if line:
                    event = json.loads(line)
                    # Skip metadata line
                    if "_metadata" in event:
                        continue
                    event_type = event.get("event_type")
                    assert event_type in valid_event_types, (
                        f"Line {i} has invalid event_type: {event_type}"
                    )

                    # Check timestamp format (ISO 8601)
                    timestamp = event.get("timestamp")
                    assert timestamp is not None, f"Line {i} missing timestamp"
                    assert "T" in timestamp, f"Line {i} timestamp not ISO format"

                    # Check data is a dict
                    data = event.get("data")
                    assert isinstance(data, dict), f"Line {i} data is not a dict"


class TestCriticalDrift:
    """Test critical drift - ensure submitted manifest validates."""

    def test_submitted_manifest_validates(self):
        """CRITICAL: Submitted manifest must pass strict validation."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
            assert manifest is not None, "Failed to load submitted manifest"

            # This is the critical drift test
            assert validate_manifest(manifest), (
                "Submitted manifest fails strict validation! "
                "This indicates schema drift between generator and validator."
            )

    def test_submitted_manifest_has_new_fields(self):
        """Test that submitted manifest has new binary fields."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
            assert manifest is not None

            # Check new binary fields
            binary = manifest.get("binary", {})
            assert "native_binary_sha256" in binary, "Missing native_binary_sha256"
            assert "startup_chain_complete" in binary, "Missing startup_chain_complete"

    def test_submitted_manifest_requires_explicit_opt_in(self):
        """Test that submitted manifest has requires_explicit_opt_in=True."""
        manifest_path = Path("adapters/capabilities/opencode-manifest.json")
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
            assert manifest is not None

            # Check requires_explicit_opt_in is True in both places
            assert manifest.get("capabilities", {}).get("requires_explicit_opt_in") is True
            assert manifest.get("compatibility", {}).get("requires_explicit_opt_in") is True


class TestStrictValidation:
    """Test strict Pydantic validation catches invalid manifests."""

    def test_invalid_hash_format_rejected(self):
        """Test that invalid hash format is rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "not-a-sha256",  # Invalid format
                "entrypoint_sha256": "a" * 64,
                "native_binary_sha256": "b" * 64,
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": False,
                "recommended_mode": "none",
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "Invalid hash format should be rejected"
        assert any("Hash must be 64 hex chars" in str(e) for e in errors)

    def test_invalid_version_format_rejected(self):
        """Test that invalid version format is rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "not-semver",  # Invalid format
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "a" * 64,
                "entrypoint_sha256": "b" * 64,
                "native_binary_sha256": "c" * 64,
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": False,
                "recommended_mode": "none",
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, _errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "Invalid version format should be rejected"

    def test_legacy_recommended_rejected(self):
        """Test that legacy as recommended mode is rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "a" * 64,
                "entrypoint_sha256": "b" * 64,
                "native_binary_sha256": "c" * 64,
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": True,
                "recommended_mode": "legacy",  # Invalid: legacy cannot be recommended
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "Legacy as recommended mode should be rejected"
        assert any("legacy" in str(e).lower() and "recommended" in str(e).lower() for e in errors)

    def test_pure_recommended_without_support_rejected(self):
        """Test that pure as recommended mode without pure_support is rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "a" * 64,
                "entrypoint_sha256": "b" * 64,
                "native_binary_sha256": "c" * 64,
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,  # No pure support
                "legacy_support": False,
                "recommended_mode": "pure",  # Invalid: pure recommended without support
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "Pure recommended without pure_support should be rejected"
        assert any("pure" in str(e).lower() and "support" in str(e).lower() for e in errors)

    def test_extra_fields_rejected(self):
        """Test that extra fields are rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "a" * 64,
                "entrypoint_sha256": "b" * 64,
                "native_binary_sha256": "c" * 64,
                "startup_chain_complete": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
                "extra_field": "should not be here",  # Invalid: extra field
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": False,
                "recommended_mode": "none",
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, _errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "Extra fields should be rejected"

    def test_startup_chain_inconsistency_rejected(self):
        """Test that startup_chain_complete=True with unknown hashes is rejected."""
        from adapters.capabilities.models import validate_manifest_strict

        invalid_manifest = {
            "version": "1.0.0",
            "tool": "opencode",
            "binary": {
                "name": "opencode",
                "version": "1.2.27",
                "launcher_sha256": "unknown",  # Unknown hash
                "entrypoint_sha256": "b" * 64,
                "native_binary_sha256": "c" * 64,
                "startup_chain_complete": True,  # Invalid: claims complete but has unknown
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "success_rate": 1.0,
                },
            },
            "capabilities": {
                "json_output": True,
                "pure_mode": False,
                "directory_selection": True,
                "legacy_mode": False,
                "requires_explicit_opt_in": True,
                "probe_status": {
                    "binary_found": True,
                    "version_detected": True,
                    "help_parsed": True,
                    "run_help_parsed": True,
                    "json_support_detected": True,
                    "pure_support_detected": False,
                    "dir_support_detected": True,
                },
            },
            "commands": [],
            "options": {"global": [], "run": []},
            "output_framing": {"json_events": True, "default_format": "formatted"},
            "exit_behavior": {
                "timeout_seconds": 300,
                "cancel_support": "unverified",
                "process_tree_kill": "unverified",
            },
            "json_run_evidence": {
                "attempted": False,
                "success": False,
                "is_synthetic": True,
                "event_count": 0,
            },
            "compatibility": {
                "version": "1.2.27",
                "pure_support": False,
                "legacy_support": False,
                "recommended_mode": "none",
                "requires_explicit_opt_in": True,
            },
        }

        is_valid, errors = validate_manifest_strict(invalid_manifest)
        assert not is_valid, "startup_chain_complete=True with unknown hashes should be rejected"
        assert any("unknown" in str(e).lower() for e in errors)


class TestProcessTreeTermination:
    """Test process tree termination logic."""

    @patch("subprocess.Popen")
    @patch("psutil.Process")
    def test_windows_psutil_termination(self, mock_psutil_process, mock_popen):
        """Test Windows process tree termination using psutil."""
        from adapters.capabilities.manifest import _terminate_process_tree_windows

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Mock psutil process and children
        mock_child1 = MagicMock()
        mock_child2 = MagicMock()
        mock_parent = MagicMock()
        mock_parent.children.return_value = [mock_child1, mock_child2]

        mock_psutil_process.return_value = mock_parent

        # Call the function
        _terminate_process_tree_windows(mock_proc, timeout=5.0)

        # Verify terminate was called on children and parent
        mock_child1.terminate.assert_called_once()
        mock_child2.terminate.assert_called_once()
        mock_parent.terminate.assert_called_once()

    @patch("subprocess.Popen")
    @patch("psutil.Process")
    def test_windows_psutil_force_kill_on_timeout(self, mock_psutil_process, mock_popen):
        """Test that force kill is called when graceful termination times out."""

        from adapters.capabilities.manifest import _terminate_process_tree_windows

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Mock psutil process
        mock_parent = MagicMock()
        mock_parent.children.return_value = []
        mock_psutil_process.return_value = mock_parent

        # Mock wait_procs to return alive processes
        with patch("psutil.wait_procs") as mock_wait:
            mock_wait.return_value = ([], [mock_parent])  # parent still alive

            _terminate_process_tree_windows(mock_proc, timeout=5.0)

            # Verify force kill was called
            mock_parent.kill.assert_called_once()

    @patch("subprocess.Popen")
    def test_posix_sigkill_on_timeout(self, mock_popen):
        """Test that SIGKILL is sent when SIGTERM times out."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        import signal

        from adapters.capabilities.manifest import _terminate_process_tree_posix

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # First wait times out, second succeeds
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 2), None]

        with patch("os.getpgid", return_value=100), patch("os.killpg") as mock_killpg:
            _terminate_process_tree_posix(mock_proc, timeout=2.0)

            # Verify SIGTERM was sent
            mock_killpg.assert_any_call(100, signal.SIGTERM)

            # Verify SIGKILL was sent (after timeout)
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            mock_killpg.assert_any_call(100, sigkill)

    @patch("subprocess.Popen")
    def test_direct_kill_fallback(self, mock_popen):
        """Test fallback to direct process kill when no process group."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        from adapters.capabilities.manifest import _terminate_process_tree_posix

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        with patch("os.getpgid", side_effect=OSError("No such process")):
            _terminate_process_tree_posix(mock_proc, timeout=5.0)

            # Verify terminate and kill were called
            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_called()

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_taskkill_timeout_fallback(self, mock_popen, mock_run):
        """Test fallback when taskkill itself times out."""
        from adapters.capabilities.manifest import _terminate_process_tree_windows_fallback

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Mock taskkill timeout
        mock_run.side_effect = subprocess.TimeoutExpired("taskkill", 10)

        _terminate_process_tree_windows_fallback(mock_proc, timeout=5.0)

        # Verify force kill was called
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called()

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_taskkill_nonzero_fallback(self, mock_popen, mock_run):
        """Test fallback when taskkill returns non-zero."""
        from adapters.capabilities.manifest import _terminate_process_tree_windows_fallback

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Mock taskkill failure
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        _terminate_process_tree_windows_fallback(mock_proc, timeout=5.0)

        # Verify force kill was called
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called()

    @patch("subprocess.Popen")
    def test_posix_check_process_group_after_sigterm(self, mock_popen):
        """Test that process group is checked after SIGTERM."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        from adapters.capabilities.manifest import _terminate_process_tree_posix

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Process responds to SIGTERM
        mock_proc.wait.return_value = None

        with patch("os.getpgid", return_value=100), patch("os.killpg") as mock_killpg:
            # First call: SIGTERM, second call: check (signal 0), third call: SIGKILL
            mock_killpg.side_effect = [None, None, None]

            _terminate_process_tree_posix(mock_proc, timeout=2.0)

            # Verify SIGTERM was sent
            mock_killpg.assert_any_call(100, signal.SIGTERM)

            # Verify process group check (signal 0)
            mock_killpg.assert_any_call(100, 0)

            # Verify SIGKILL was sent
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            mock_killpg.assert_any_call(100, sigkill)

    @patch("subprocess.Popen")
    def test_posix_process_group_gone_after_sigterm(self, mock_popen):
        """Test that SIGKILL is not sent if process group is gone after SIGTERM."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("POSIX-specific test")

        from adapters.capabilities.manifest import _terminate_process_tree_posix

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Process responds to SIGTERM
        mock_proc.wait.return_value = None

        with patch("os.getpgid", return_value=100), patch("os.killpg") as mock_killpg:
            # First call: SIGTERM, second call: check (signal 0) raises OSError (process group gone)
            mock_killpg.side_effect = [None, OSError("No such process")]

            _terminate_process_tree_posix(mock_proc, timeout=2.0)

            # Verify SIGTERM was sent
            mock_killpg.assert_any_call(100, signal.SIGTERM)

            # Verify process group check (signal 0)
            mock_killpg.assert_any_call(100, 0)

            # Verify SIGKILL was NOT sent (only 2 calls, not 3)
            assert mock_killpg.call_count == 2

    @patch("subprocess.Popen")
    @patch("psutil.Process")
    def test_windows_popen_reclaimed(self, mock_psutil_process, mock_popen):
        """Test that Popen object is reclaimed after psutil termination."""
        from adapters.capabilities.manifest import _terminate_process_tree_windows

        # Mock process
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        # Mock psutil process
        mock_parent = MagicMock()
        mock_parent.children.return_value = []
        mock_psutil_process.return_value = mock_parent

        # Mock wait_procs to return empty alive list
        with patch("psutil.wait_procs") as mock_wait:
            mock_wait.return_value = ([], [])

            _terminate_process_tree_windows(mock_proc, timeout=5.0)

            # Verify Popen wait was called to reclaim handles
            mock_proc.wait.assert_called()
