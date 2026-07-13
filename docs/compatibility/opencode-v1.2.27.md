# OpenCode CLI Compatibility Report

## Version Information

- **OpenCode Version**: 1.2.27
- **Report Generated**: 2026-07-12
- **Agent**: HUB-030 OpenCode CLI Capability Spike

## Executive Summary

OpenCode CLI version 1.2.27 provides JSON output support via `--format json` flag but lacks `--pure` mode support. Legacy mode is available but **requires explicit opt-in** due to security considerations. Process management capabilities (timeout, cancellation) are unverified and require HUB-300 Runner testing.

## Capability Manifest

The complete capability manifest is available at: `adapters/capabilities/opencode-manifest.json`

### Key Capabilities

| Capability | Supported | Notes |
|------------|-----------|-------|
| JSON Output | ✅ Yes | Via `--format json` flag |
| Pure Mode | ❌ No | Not available in version 1.2.27 |
| Directory Selection | ✅ Yes | Via `--dir` flag |
| Legacy Mode | ⚠️ Requires Explicit Opt-in | Available but must be explicitly chosen |

### Command Structure

OpenCode CLI supports the following commands:

1. **Core Commands**
   - `opencode [project]` - Start TUI (default)
   - `opencode run [message..]` - Run with a message
   - `opencode serve` - Start headless server
   - `opencode web` - Start server and open web interface

2. **Management Commands**
   - `opencode completion` - Shell completion
   - `opencode acp` - ACP server
   - `opencode mcp` - MCP server management
   - `opencode providers` - AI provider management
   - `opencode agent` - Agent management
   - `opencode session` - Session management
   - `opencode db` - Database tools

3. **Utility Commands**
   - `opencode upgrade` - Upgrade CLI
   - `opencode uninstall` - Uninstall
   - `opencode models` - List models
   - `opencode stats` - Usage statistics
   - `opencode export` - Export session data
   - `opencode import` - Import session data
   - `opencode github` - GitHub agent
   - `opencode pr` - PR checkout and run

### Global Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--help` | boolean | - | Show help |
| `--version` | boolean | - | Show version |
| `--print-logs` | boolean | false | Print logs to stderr |
| `--log-level` | string | - | Log level (DEBUG, INFO, WARN, ERROR) |
| `--port` | number | 0 | Port to listen on |
| `--hostname` | string | "127.0.0.1" | Hostname to listen on |
| `--mdns` | boolean | false | Enable mDNS service discovery |
| `--mdns-domain` | string | "opencode.local" | Custom mDNS domain |
| `--cors` | array | [] | Additional CORS domains |
| `--model` | string | - | Model to use (provider/model) |
| `--continue` | boolean | - | Continue last session |
| `--session` | string | - | Session ID to continue |
| `--fork` | boolean | - | Fork session when continuing |
| `--prompt` | string | - | Prompt to use |
| `--agent` | string | - | Agent to use |

### Run Command Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--command` | string | - | Command to run |
| `--continue` | boolean | - | Continue last session |
| `--session` | string | - | Session ID to continue |
| `--fork` | boolean | - | Fork session before continuing |
| `--share` | boolean | - | Share the session |
| `--model` | string | - | Model to use |
| `--agent` | string | - | Agent to use |
| `--format` | string | "default" | Output format (default/json) |
| `--file` | array | - | Files to attach |
| `--title` | string | - | Session title |
| `--attach` | string | - | Attach to running server |
| `--password` | string | - | Basic auth password |
| `--dir` | string | - | Working directory |
| `--port` | number | - | Local server port |
| `--variant` | string | - | Model variant |
| `--thinking` | boolean | false | Show thinking blocks |

## JSON Output Support

OpenCode supports JSON output via the `--format json` flag. This produces raw JSON events that can be parsed programmatically.

### Example Usage

```bash
# Get JSON output
opencode run --format json "Hello, world!"

# With directory selection
opencode run --format json --dir /path/to/project "Analyze this code"
```

## Compatibility Considerations

### Version 1.2.27 Limitations

1. **No Pure Mode**: Version 1.2.27 does not support `--pure` mode. Legacy mode is the only available option.
2. **Legacy Mode Requires Explicit Opt-in**: Legacy mode must be explicitly chosen; it is never recommended.
3. **JSON Output Available**: JSON output is supported via `--format json` flag.
4. **Process Management Unverified**: Timeout and cancellation capabilities are unverified and require HUB-300 Runner testing.

### Recommended Configuration

For version 1.2.27, the recommended configuration is:

```json
{
  "mode": "none",
  "format": "json",
  "timeout_seconds": 300,
  "requires_explicit_opt_in": true
}
```

**Note**: Legacy mode is available but not recommended. If legacy mode is required, it must be explicitly configured.

### Security Considerations

1. **Legacy Mode**: Legacy mode provides full access to CLI capabilities and requires explicit opt-in.
2. **No Pure Mode**: Without pure mode, there's no restricted execution environment.
3. **Directory Selection**: The `--dir` flag allows specifying working directory.
4. **Process Management**: Timeout and cancellation capabilities are **unverified** - marked as such in manifest until HUB-300 Runner testing confirms them.

## Integration Notes

### For HUB-300 (CliAgentSpec/CliAgentRunner)

- Use `--format json` for structured output
- Implement timeout handling (default 300 seconds)
- Use `--dir` for workspace isolation
- Store argv as arrays, not shell strings
- **Verify process management capabilities** (currently marked as "unverified")

### For HUB-310 (Executor/Planner Adapter)

- **Explicitly configure legacy mode** (requires opt-in)
- Validate resolved configuration
- Implement proper error handling for non-pure mode

### For HUB-420 (ConsoleStream)

- Parse JSON events from `--format json` output
- Implement streaming redaction for sensitive information
- Handle chunked output artifacts

## Testing Recommendations

1. **Fixture Validation**: Ensure fixtures contain no secrets or absolute paths
2. **JSON Parsing**: Validate JSON output parsing
3. **Timeout Handling**: Test timeout and cancellation scenarios
4. **Directory Isolation**: Verify directory selection works correctly

## Conclusion

OpenCode CLI version 1.2.27 provides a foundation for CLI agent integration with JSON output support. Key considerations:

- **Legacy mode requires explicit opt-in** - it is never recommended, only available
- **Process management capabilities are unverified** - require HUB-300 Runner testing
- **No pure mode** - security-sensitive contexts require careful consideration

The manifest enforces fail-closed behavior: if any critical probe fails, legacy mode is disabled and explicit opt-in is always required.

## References

- [OpenCode CLI Documentation](https://opencode.ai/docs)
- [Capability Manifest](adapters/capabilities/opencode-manifest.json)
- [HUB-030 Task Brief](development-tasks/next-wave/HUB-030-opencode.md)