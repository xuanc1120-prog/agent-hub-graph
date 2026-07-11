# HUB-030: OpenCode CLI Capability Spike

Task ID: `HUB-030`

Owner: OpenCode + MiMo-V2.5-Pro

Reviewer: Codex

Branch: `agent/opencode-adapter`

Worktree: `E:\agent_hub_worktrees\opencode-adapter`

## Goal

Measure the installed OpenCode CLI instead of assuming its interface. Produce a machine-readable capability manifest, redacted fixtures and a compatibility report that HUB-300/310 can consume.

## Owned Paths

- `adapters/capabilities/**`
- `tests/fixtures/opencode/**`
- `tests/test_opencode_capability_manifest.py`
- `docs/compatibility/opencode-*.md`

## Forbidden Paths

- `protocol/**`
- production runner/adapter implementation
- `storage/**`, `workflow/**`, `workspace/**`, `security/**`
- source repo modification or real write-agent execution

## Required Behavior

1. Record `opencode --version`, top-level help and `opencode run --help` capabilities, including exact support for JSON output, directory selection and pure/legacy modes.
2. Store argv as arrays, never shell command strings. Do not record credentials, environment values or user-specific absolute paths.
3. If a harmless JSON run is possible, execute it only in a dedicated temporary Git repo with no secrets; otherwise record a clear credential-related skip and provide a synthetic fixture separately marked as synthetic.
4. Produce a versioned JSON manifest with binary name/version/hash, supported flags, output framing, exit behavior and compatibility verdict.
5. Confirm whether installed `1.2.27` lacks `--pure`; legacy compatibility must remain explicit opt-in and cannot be described as the secure default.

## Required Tests

- manifest JSON schema/shape validation
- captured JSON/JSONL fixture parsing smoke
- checks proving fixtures contain no bearer token, common secret key or absolute home path

## Acceptance

HUB-300 can select behavior from the manifest without parsing human help text at runtime. Commit once as `docs(HUB-030): ...`; do not implement the adapter or modify the frozen protocol.
