"""OpenCode CLI capability detection and manifest generation."""

import contextlib
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def get_binary_hash(binary_path: str) -> str:
    """Calculate SHA256 hash of a binary file."""
    try:
        with open(binary_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "unknown"


def get_opencode_startup_chain() -> dict[str, Any]:
    """
    Get the complete OpenCode startup chain with hashes.

    Returns:
        Dictionary with:
        - launcher_path: Path to launcher script (.cmd/.sh)
        - launcher_sha256: Hash of launcher
        - entrypoint_path: Path to JS entry point
        - entrypoint_sha256: Hash of JS entry point
        - native_binary_path: Path to native binary (.exe)
        - native_binary_sha256: Hash of native binary
        - chain_complete: Whether all components were found
    """
    import os
    import platform

    result = {
        "launcher_path": "unknown",
        "launcher_sha256": "unknown",
        "entrypoint_path": "unknown",
        "entrypoint_sha256": "unknown",
        "native_binary_path": "unknown",
        "native_binary_sha256": "unknown",
        "chain_complete": False,
    }

    try:
        # Find opencode launcher
        if platform.system() == "Windows":
            proc = subprocess.run(
                ["where", "opencode"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            proc = subprocess.run(
                ["which", "opencode"],
                capture_output=True,
                text=True,
                timeout=10,
            )

        if proc.returncode != 0:
            return result

        # Get all launcher paths from where/which
        all_paths = [p.strip() for p in proc.stdout.strip().split("\n") if p.strip()]

        # On Windows, prefer .cmd over extensionless shim
        launcher_path = None
        if platform.system() == "Windows":
            for path in all_paths:
                if path.endswith(".cmd"):
                    launcher_path = path
                    break
        # Fallback to first path if no .cmd found
        if launcher_path is None:
            launcher_path = all_paths[0] if all_paths else "unknown"

        result["launcher_path"] = launcher_path
        result["launcher_sha256"] = get_binary_hash(launcher_path)

        # Read launcher to find JS entry point
        if launcher_path.endswith(".cmd"):
            # Windows .cmd launcher - read to find node invocation
            with open(launcher_path, encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Look for pattern: "%_prog%" "%dp0%\node_modules\opencode-ai\bin\opencode"
            import re

            match = re.search(r'"%_prog%"\s+"(%dp0%\\[^"]+)"', content)
            if match:
                # Expand environment variables
                dp0 = os.path.dirname(launcher_path)
                entrypoint_relative = match.group(1).replace("%dp0%", dp0)
                entrypoint_path = os.path.normpath(entrypoint_relative)

                if os.path.exists(entrypoint_path):
                    result["entrypoint_path"] = entrypoint_path
                    result["entrypoint_sha256"] = get_binary_hash(entrypoint_path)

                    # Read JS entry to find native binary
                    # Note: We don't need to store the content, just verify file exists

                    # Look for pattern: findBinary(scriptDir)
                    # The native binary is in node_modules/opencode-{platform}-{arch}/bin/opencode
                    platform_map = {"windows": "windows", "linux": "linux", "darwin": "darwin"}
                    # Map Windows AMD64 to x64, and other variants
                    arch_map = {
                        "x64": "x64",
                        "amd64": "x64",  # Windows returns AMD64
                        "x86_64": "x64",  # Linux/macOS returns x86_64
                        "arm64": "arm64",
                        "aarch64": "arm64",  # Linux returns aarch64
                    }

                    current_platform = platform_map.get(
                        platform.system().lower(), platform.system().lower()
                    )
                    current_arch = arch_map.get(
                        platform.machine().lower(), platform.machine().lower()
                    )

                    # Look for native binary in node_modules
                    base_name = f"opencode-{current_platform}-{current_arch}"
                    binary_name = "opencode.exe" if platform.system() == "Windows" else "opencode"

                    # Search up from entrypoint directory
                    current_dir = os.path.dirname(entrypoint_path)
                    for _ in range(10):  # Limit search depth
                        modules_dir = os.path.join(current_dir, "node_modules")
                        if os.path.isdir(modules_dir):
                            # Check for native binary
                            candidate = os.path.join(modules_dir, base_name, "bin", binary_name)
                            if os.path.exists(candidate):
                                result["native_binary_path"] = candidate
                                result["native_binary_sha256"] = get_binary_hash(candidate)
                                break

                            # Check baseline variant
                            baseline_candidate = os.path.join(
                                modules_dir, f"{base_name}-baseline", "bin", binary_name
                            )
                            if os.path.exists(baseline_candidate):
                                result["native_binary_path"] = baseline_candidate
                                result["native_binary_sha256"] = get_binary_hash(baseline_candidate)
                                break

                        parent_dir = os.path.dirname(current_dir)
                        if parent_dir == current_dir:
                            break
                        current_dir = parent_dir

        # Check if chain is complete
        result["chain_complete"] = all(
            [
                result["launcher_sha256"] != "unknown",
                result["entrypoint_sha256"] != "unknown",
                result["native_binary_sha256"] != "unknown",
            ]
        )

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return result


def get_opencode_version() -> str:
    """Get OpenCode CLI version."""
    try:
        # On Windows, may need to use cmd to run .cmd files
        import platform

        if platform.system() == "Windows":
            # Use cmd /c to run opencode command
            result = subprocess.run(
                ["cmd", "/c", "opencode", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            result = subprocess.run(
                ["opencode", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def get_opencode_help() -> dict[str, Any]:
    """Get OpenCode CLI help information."""
    try:
        import platform

        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", "opencode", "--help"],
                capture_output=True,
                timeout=10,
            )
            # 手动处理编码
            stdout = result.stdout.decode("utf-8", errors="ignore") if result.stdout else ""
        else:
            result = subprocess.run(
                ["opencode", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout = result.stdout or ""

        if result.returncode == 0:
            return {
                "raw": stdout,
                "commands": extract_commands_from_help(stdout),
                "options": extract_options_from_help(stdout),
            }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return {"raw": "", "commands": [], "options": []}


def get_opencode_run_help() -> dict[str, Any]:
    """Get OpenCode run command help information."""
    try:
        import platform

        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", "opencode", "run", "--help"],
                capture_output=True,
                timeout=10,
            )
            # 手动处理编码
            stdout = result.stdout.decode("utf-8", errors="ignore") if result.stdout else ""
        else:
            result = subprocess.run(
                ["opencode", "run", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout = result.stdout or ""

        if result.returncode == 0:
            return {
                "raw": stdout,
                "options": extract_options_from_help(stdout),
            }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return {"raw": "", "options": []}


def extract_commands_from_help(help_text: str) -> list[dict[str, str]]:
    """Extract commands from help text."""
    commands = []
    lines = help_text.split("\n")
    in_commands_section = False

    for line in lines:
        if "Commands:" in line:
            in_commands_section = True
            continue
        if in_commands_section and line.strip():
            if line.startswith("  opencode "):
                # Parse command line, format: opencode command [args]  description
                parts = line.strip().split(maxsplit=2)
                if len(parts) >= 2:
                    # First part is "opencode", second part is command
                    cmd = parts[1] if len(parts) > 1 else ""
                    desc = parts[2] if len(parts) > 2 else ""
                    # Check for aliases
                    if "[aliases:" in desc:
                        desc = desc.split("[aliases:")[0].strip()
                    # Remove default marker
                    if "[default]" in desc:
                        desc = desc.replace("[default]", "").strip()
                    commands.append({"command": cmd, "description": desc})
            elif not line.startswith(" "):
                break

    return commands


def extract_options_from_help(help_text: str) -> list[dict[str, str]]:
    """Extract options from help text."""
    options = []
    lines = help_text.split("\n")
    in_options_section = False

    for line in lines:
        if "Options:" in line:
            in_options_section = True
            continue
        if in_options_section and line.strip():
            if line.startswith("  ") and ("--" in line or "-h" in line or "-v" in line):
                # Parse option line
                # Format: -h, --help  description  [type] [default: value]
                # Or: --print-logs  description  [type] [default: value]

                # Extract option part (first column)
                line_parts = line.split()
                if len(line_parts) >= 1:
                    # Find option end position (usually two spaces)
                    option_part = ""
                    desc_start = 0

                    # Find option part
                    for i, part in enumerate(line_parts):
                        if part.startswith("-") or part.endswith(","):
                            option_part += part + " "
                        else:
                            desc_start = i
                            break

                    option_part = option_part.strip()
                    if not option_part and line_parts[0].startswith("-"):
                        option_part = line_parts[0]
                        desc_start = 1

                    # Normalize option format: extract long option
                    normalized_option = option_part
                    if ", " in option_part:
                        # Format: -h, --help -> --help
                        option_parts = option_part.split(", ")
                        if len(option_parts) > 1:
                            normalized_option = option_parts[1]
                    elif option_part.startswith("-") and not option_part.startswith("--"):
                        # Short option: -h -> --help (need to infer from description)
                        # Keep as is, cannot infer long option from short option
                        normalized_option = option_part

                    # Get description part
                    desc = " ".join(line_parts[desc_start:]) if desc_start < len(line_parts) else ""

                    # Check type and default value
                    type_info = ""
                    default_info = ""

                    if "[string]" in desc:
                        type_info = "string"
                    elif "[number]" in desc:
                        type_info = "number"
                    elif "[boolean]" in desc:
                        type_info = "boolean"
                    elif "[array]" in desc:
                        type_info = "array"

                    # Extract default value
                    if "[default:" in desc:
                        default_start = desc.find("[default:") + 9
                        default_end = desc.find("]", default_start)
                        if default_end > default_start:
                            default_info = desc[default_start:default_end]

                    # Clean description
                    clean_desc = desc
                    for remove in ["[string]", "[number]", "[boolean]", "[array]"]:
                        clean_desc = clean_desc.replace(remove, "")
                    if "[default:" in clean_desc:
                        default_start = clean_desc.find("[default:")
                        default_end = clean_desc.find("]", default_start) + 1
                        clean_desc = clean_desc[:default_start] + clean_desc[default_end:]
                    clean_desc = clean_desc.strip()

                    options.append(
                        {
                            "option": normalized_option,
                            "description": clean_desc,
                            "type": type_info,
                            "default": default_info,
                        }
                    )
            elif not line.startswith(" "):
                break

    return options


def check_json_output_support() -> bool:
    """Check if opencode supports JSON output format."""
    try:
        import platform

        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", "opencode", "run", "--help"],
                capture_output=True,
                timeout=10,
            )
            # 处理字节或字符串输出
            if isinstance(result.stdout, bytes):
                stdout = result.stdout.decode("utf-8", errors="ignore")
            else:
                stdout = result.stdout or ""
        else:
            result = subprocess.run(
                ["opencode", "run", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout = result.stdout or ""

        if result.returncode == 0:
            return "--format" in stdout and "json" in stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def check_pure_mode_support() -> bool:
    """Check if opencode supports --pure mode."""
    try:
        import platform

        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", "opencode", "run", "--help"],
                capture_output=True,
                timeout=10,
            )
            # 处理字节或字符串输出
            if isinstance(result.stdout, bytes):
                stdout = result.stdout.decode("utf-8", errors="ignore")
            else:
                stdout = result.stdout or ""
        else:
            result = subprocess.run(
                ["opencode", "run", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout = result.stdout or ""

        if result.returncode == 0:
            return "--pure" in stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def check_dir_option_support() -> bool:
    """Check if opencode supports --dir option."""
    try:
        import platform

        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", "opencode", "run", "--help"],
                capture_output=True,
                timeout=10,
            )
            # 处理字节或字符串输出
            if isinstance(result.stdout, bytes):
                stdout = result.stdout.decode("utf-8", errors="ignore")
            else:
                stdout = result.stdout or ""
        else:
            result = subprocess.run(
                ["opencode", "run", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout = result.stdout or ""

        if result.returncode == 0:
            return "--dir" in stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def create_capability_manifest(attempt_json_run: bool = False) -> dict[str, Any]:
    """Create a comprehensive capability manifest for OpenCode CLI.

    Args:
        attempt_json_run: If True, attempt a real JSON run in a temp repo.
                         If False, use synthetic data (safe for unit tests).
                         Should only be True for explicit integration/manual commands.
    """
    # Probe status tracking
    probe_results = {
        "binary_found": False,
        "version_detected": False,
        "help_parsed": False,
        "run_help_parsed": False,
        "json_support_detected": False,
        "pure_support_detected": False,
        "dir_support_detected": False,
    }

    # Get complete startup chain with hashes
    startup_chain = get_opencode_startup_chain()
    binary_path = startup_chain.get("launcher_path", "opencode")
    launcher_hash = startup_chain.get("launcher_sha256", "unknown")
    entrypoint_hash = startup_chain.get("entrypoint_sha256", "unknown")
    native_binary_hash = startup_chain.get("native_binary_sha256", "unknown")

    if startup_chain.get("launcher_sha256", "unknown") != "unknown":
        probe_results["binary_found"] = True

    # Get version information
    version = get_opencode_version()
    if version != "unknown":
        probe_results["version_detected"] = True

    # Get help information
    help_info = get_opencode_help()
    if help_info.get("commands") or help_info.get("options"):
        probe_results["help_parsed"] = True

    run_help_info = get_opencode_run_help()
    if run_help_info.get("options"):
        probe_results["run_help_parsed"] = True

    # Check capabilities
    supports_json = check_json_output_support()
    if supports_json:
        probe_results["json_support_detected"] = True

    supports_pure = check_pure_mode_support()
    if supports_pure:
        probe_results["pure_support_detected"] = True

    supports_dir = check_dir_option_support()
    if supports_dir:
        probe_results["dir_support_detected"] = True

    # Calculate probe success rate
    probe_success_count = sum(probe_results.values())
    probe_total = len(probe_results)
    probe_success_rate = probe_success_count / probe_total

    # Fail closed mechanism: do not enable legacy mode if critical probes fail
    # Critical probes: binary_found, version_detected, help_parsed, run_help_parsed
    # Plus JSON and dir support detection
    critical_probes_passed = all(
        [
            probe_results["binary_found"],
            probe_results["version_detected"],
            probe_results["help_parsed"],
            probe_results["run_help_parsed"],
            probe_results["json_support_detected"],
            probe_results["dir_support_detected"],
        ]
    )

    # Legacy mode ALWAYS requires explicit opt-in, regardless of probe success
    # This is a security requirement: legacy mode must be explicitly chosen
    requires_explicit_opt_in = True

    # Legacy mode: only available when critical probes succeed and pure mode is not supported
    # If probes fail, legacy_mode should be False (fail closed)
    legacy_mode = critical_probes_passed and not supports_pure

    # Legacy support: only declare support when probes succeed
    legacy_support = critical_probes_passed

    # Recommended mode: no recommendation if probes fail
    # Legacy mode is never recommended - it must be explicitly chosen
    if not critical_probes_passed:
        recommended_mode = "none"
    elif supports_pure:
        recommended_mode = "pure"
    else:
        # Legacy mode is available but not recommended
        recommended_mode = "none"

    # Sanitize path: remove user sensitive information
    def sanitize_path(path: str) -> str:
        """Sanitize path, remove user sensitive information."""
        # Remove Windows user directory path
        if "Users" in path and "AppData" in path:
            # Replace with generic path
            return "opencode (npm global)"
        # Remove Unix user directory path
        if "/home/" in path or "/Users/" in path:
            return "opencode (global)"
        return path

    sanitized_binary_path = sanitize_path(binary_path)

    # Attempt JSON run - only if explicitly requested (not for unit tests)
    if attempt_json_run:
        json_run_result = attempt_json_run_in_temp_repo()
    else:
        # Use synthetic data for unit tests - safe, no real agent calls
        json_run_result = {
            "attempted": False,
            "success": False,
            "skip_reason": "JSON run not attempted (use attempt_json_run=True for integration)",
            "json_events": [],
            "is_synthetic": True,
        }

    # Create manifest
    manifest = {
        "version": "1.0.0",
        "tool": "opencode",
        "binary": {
            "name": sanitized_binary_path,
            "version": version,
            "launcher_sha256": launcher_hash,
            "entrypoint_sha256": entrypoint_hash,
            "native_binary_sha256": native_binary_hash,
            "startup_chain_complete": startup_chain.get("chain_complete", False),
            "probe_status": {
                "binary_found": probe_results["binary_found"],
                "version_detected": probe_results["version_detected"],
                "success_rate": probe_success_rate,
            },
        },
        "capabilities": {
            "json_output": supports_json,
            "pure_mode": supports_pure,
            "directory_selection": supports_dir,
            "legacy_mode": legacy_mode,
            "requires_explicit_opt_in": requires_explicit_opt_in,
            "probe_status": probe_results,
        },
        "commands": help_info.get("commands", []),
        "options": {
            "global": help_info.get("options", []),
            "run": run_help_info.get("options", []),
        },
        "output_framing": {
            "json_events": supports_json,
            "default_format": "formatted",
        },
        "exit_behavior": {
            "timeout_seconds": 300,
            "cancel_support": "unverified",
            "process_tree_kill": "unverified",
        },
        "json_run_evidence": {
            "attempted": json_run_result.get("attempted", False),
            "success": json_run_result.get("success", False),
            "skip_reason": json_run_result.get("skip_reason"),
            "is_synthetic": json_run_result.get("is_synthetic", False),
            "event_count": len(json_run_result.get("json_events", [])),
        },
        "compatibility": {
            "version": version,
            "pure_support": supports_pure,
            "legacy_support": legacy_support,
            "recommended_mode": recommended_mode,
            "requires_explicit_opt_in": requires_explicit_opt_in,
        },
    }

    return manifest


def save_manifest(manifest: dict[str, Any], output_path: Path) -> None:
    """Save manifest to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def load_manifest(manifest_path: Path) -> dict[str, Any] | None:
    """Load manifest from a JSON file."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def validate_manifest(manifest: dict[str, Any]) -> bool:
    """Validate manifest structure with strict Pydantic model."""
    from adapters.capabilities.models import validate_manifest_strict

    is_valid, _errors = validate_manifest_strict(manifest)
    return is_valid


def _terminate_process_tree(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """
    Terminate an entire process tree, escalating from SIGTERM to SIGKILL.

    Uses psutil on Windows for recursive child termination.
    Uses process groups on POSIX for group-wide signals.

    Args:
        proc: The subprocess.Popen object to terminate
        timeout: Seconds to wait after SIGTERM before escalating to SIGKILL
    """
    import platform

    try:
        if platform.system() == "Windows":
            _terminate_process_tree_windows(proc, timeout)
        else:
            _terminate_process_tree_posix(proc, timeout)
    except Exception:
        # Last resort: force kill direct process
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def _terminate_process_tree_windows(proc: subprocess.Popen, timeout: float) -> None:
    """Terminate process tree on Windows using psutil."""
    try:
        import psutil

        # Get the process object
        try:
            parent = psutil.Process(proc.pid)
        except psutil.NoSuchProcess:
            return

        # Get all children recursively
        children = parent.children(recursive=True)

        # Terminate children first (bottom-up)
        for child in reversed(children):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.terminate()

        # Terminate parent
        with contextlib.suppress(psutil.NoSuchProcess):
            parent.terminate()

        # Wait for graceful termination
        _, alive = psutil.wait_procs([*children, parent], timeout=timeout)

        # Force kill any still alive
        for p in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                p.kill()

        # Final wait
        psutil.wait_procs(alive, timeout=2)

    except ImportError:
        # psutil not available, fallback to taskkill
        _terminate_process_tree_windows_fallback(proc, timeout)
    finally:
        # Always reclaim Popen object and pipe handles
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def _terminate_process_tree_windows_fallback(proc: subprocess.Popen, timeout: float) -> None:
    """Fallback process tree termination on Windows using taskkill."""
    try:
        # Try taskkill with process tree flag
        taskkill_result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=timeout + 2,
        )

        if taskkill_result.returncode == 0:
            # taskkill succeeded, wait for process
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            # taskkill failed, force kill direct process
            proc.kill()
            proc.wait(timeout=2)

    except subprocess.TimeoutExpired:
        # taskkill itself timed out, force kill
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
    except Exception:
        # Any other error, force kill
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=1)


def _terminate_process_tree_posix(proc: subprocess.Popen, timeout: float) -> None:
    """Terminate process tree on POSIX using process groups."""
    import os
    import signal

    # Get process group ID (use getattr for safety)
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)

    pgid = None
    if getpgid:
        try:
            pgid = getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None

    if pgid is not None and killpg:
        # Send SIGTERM to entire process group
        with contextlib.suppress(ProcessLookupError, OSError):
            killpg(pgid, signal.SIGTERM)

        # Wait for graceful termination
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout)

        # Check if process group still exists (child processes may ignore SIGTERM)
        # killpg(pgid, 0) checks if we can send a signal without actually sending one
        try:
            killpg(pgid, 0)  # Signal 0: check if process group exists
            # Process group still exists, escalate to SIGKILL
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            with contextlib.suppress(ProcessLookupError, OSError):
                killpg(pgid, sigkill)
            # Wait for SIGKILL to take effect
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        except (ProcessLookupError, OSError):
            # Process group doesn't exist anymore, all processes terminated
            pass
    else:
        # No process group, just kill the process
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def attempt_json_run_in_temp_repo() -> dict[str, Any]:
    """
    Attempt to execute harmless JSON run in temporary Git repository.
    Returns run result or skip record.

    Security measures:
    - Uses environment whitelist (only passes safe env vars)
    - Ensures process tree cleanup on timeout (kills entire process group)
    - Runs in isolated temp directory
    """
    import os
    import tempfile

    result = {
        "attempted": False,
        "success": False,
        "skip_reason": None,
        "json_events": [],
        "is_synthetic": False,
    }

    try:
        # Check for credentials
        import platform

        # Environment whitelist - only pass safe environment variables
        SAFE_ENV_VARS = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "SHELL",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENCODE_API_KEY",
        }

        # Create filtered environment
        safe_env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}

        has_credentials = any(
            key in os.environ for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENCODE_API_KEY"]
        )

        if not has_credentials:
            result["attempted"] = True
            result["skip_reason"] = "No API credentials found in environment"
            return result

        # Create temporary Git repository
        with tempfile.TemporaryDirectory() as temp_dir:
            # Initialize Git repository
            subprocess.run(
                ["git", "init", temp_dir],
                capture_output=True,
                timeout=10,
            )

            # Try to run a simple JSON command
            # Note: Here we only test JSON output capability, not actual complex operations
            test_message = "Hello, this is a test message"

            if platform.system() == "Windows":
                cmd = ["cmd", "/c", "opencode", "run", "--format", "json", test_message]
            else:
                cmd = ["opencode", "run", "--format", "json", test_message]

            # Use Popen with process group for proper cleanup on timeout
            try:
                if platform.system() == "Windows":
                    # Windows: CREATE_NEW_PROCESS_GROUP for process tree management
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=temp_dir,
                        env=safe_env,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                else:
                    # Unix: use setsid to create new session for process group
                    # Note: os.setsid is not available on Windows
                    preexec_fn = getattr(os, "setsid", None)
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=temp_dir,
                        env=safe_env,
                        preexec_fn=preexec_fn,
                    )

                try:
                    stdout_bytes, _stderr_bytes = proc.communicate(timeout=30)
                    result["attempted"] = True

                    if proc.returncode == 0:
                        stdout = stdout_bytes.decode("utf-8", errors="ignore")

                        # Try to parse JSON events
                        try:
                            json_events = []
                            for line in stdout.strip().split("\n"):
                                if line.strip():
                                    try:
                                        event = json.loads(line)
                                        json_events.append(event)
                                    except json.JSONDecodeError:
                                        continue

                            if json_events:
                                result["success"] = True
                                result["json_events"] = json_events
                            else:
                                result["skip_reason"] = "No valid JSON events in output"
                        except Exception as e:
                            result["skip_reason"] = f"JSON parsing failed: {e}"
                    else:
                        result["skip_reason"] = f"Command failed with return code {proc.returncode}"

                except subprocess.TimeoutExpired:
                    result["attempted"] = True
                    result["skip_reason"] = "Command timed out after 30 seconds"

                    # Kill entire process tree using dedicated function
                    _terminate_process_tree(proc, timeout=5.0)

            except Exception as e:
                result["attempted"] = True
                result["skip_reason"] = f"Process execution failed: {e}"

    except Exception as e:
        result["attempted"] = True
        result["skip_reason"] = f"Unexpected error: {e}"

    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate OpenCode capability manifest")
    parser.add_argument(
        "--attempt-json-run",
        action="store_true",
        default=False,
        help="Attempt real JSON run in temp repo (requires API credentials)",
    )
    args = parser.parse_args()

    # Generate manifest - with explicit control over JSON probe
    manifest = create_capability_manifest(attempt_json_run=args.attempt_json_run)

    # Save to file
    output_path = Path("adapters/capabilities/opencode-manifest.json")
    save_manifest(manifest, output_path)

    print(f"Manifest saved to: {output_path}")
    print(f"OpenCode version: {manifest['binary']['version']}")
    print(f"Pure mode support: {manifest['capabilities']['pure_mode']}")
    print(f"JSON output support: {manifest['capabilities']['json_output']}")
    print(f"JSON run attempted: {manifest['json_run_evidence']['attempted']}")
