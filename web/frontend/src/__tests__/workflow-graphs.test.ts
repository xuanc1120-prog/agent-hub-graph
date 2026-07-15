import { describe, expect, it } from 'vitest';

import {
  SAMPLE_AUTHOR_GRAPH,
  SAMPLE_COMPILED_GRAPH,
  validateAuthorGraphType,
  validateCompiledGraphType,
} from '../fixtures/workflow-graphs';

describe('frozen workflow graph fixtures', () => {
  it('provides a valid editable AuthorGraph', () => {
    expect(validateAuthorGraphType(SAMPLE_AUTHOR_GRAPH)).toBe(true);
    expect(SAMPLE_AUTHOR_GRAPH.nodes.map((node) => node.id)).toEqual([
      'input-1',
      'investigate-1',
      'fix-1',
      'output-1',
    ]);
    expect(SAMPLE_AUTHOR_GRAPH.edges.map((edge) => edge.from_node)).toEqual([
      'input-1',
      'investigate-1',
      'fix-1',
    ]);
  });

  it('keeps candidate permissions author-editable', () => {
    const fix = SAMPLE_AUTHOR_GRAPH.nodes.find((node) => node.id === 'fix-1');
    expect(fix?.assignment_mode).toBe('auto');
    expect(fix?.allowed_files_candidate).toEqual(['src/auth/login.ts']);
    expect(fix?.new_files_candidate).toEqual(['tests/auth/login.test.ts']);
    expect(fix?.effective_allowed_files).toBeUndefined();
  });

  it('provides a valid CompiledGraph snapshot', () => {
    expect(validateCompiledGraphType(SAMPLE_COMPILED_GRAPH)).toBe(true);
    expect(SAMPLE_COMPILED_GRAPH.source_author_hash).toHaveLength(64);
    expect(SAMPLE_COMPILED_GRAPH.integration_base_commit).toHaveLength(40);
    expect(SAMPLE_COMPILED_GRAPH.agent_catalog_snapshot_hash).toHaveLength(64);
  });

  it('contains compiler-resolved assignment and permission fields', () => {
    const fix = SAMPLE_COMPILED_GRAPH.nodes.find((node) => node.id === 'fix-1');
    expect(fix?.resolved_agent_id).toBe('agent-opencode-1');
    expect(fix?.resolved_agent_spec_sha256).toBe('d'.repeat(64));
    expect(fix?.effective_allowed_files).toEqual(['src/auth/login.ts']);
    expect(fix?.effective_new_files).toEqual(['tests/auth/login.test.ts']);
    expect(fix?.policy_risk_floor).toBe('L2');
    expect(fix?.requires_changeset_approval).toBe(true);
  });

  it('shows the system-injected test path as real graph data', () => {
    const testNode = SAMPLE_COMPILED_GRAPH.nodes.find(
      (node) => node.id === 'system-test-fix-1'
    );
    expect(testNode?.node_type).toBe('test');
    expect(testNode?.system_managed).toBe(true);
    expect(testNode?.source_node_id).toBe('fix-1');
    expect(testNode?.test_kind).toBe('command');
    expect(testNode?.test_argv).toEqual(['npm', 'test', '--', 'login.test.ts']);

    expect(
      SAMPLE_COMPILED_GRAPH.edges.some(
        (edge) => edge.from_node === 'fix-1' && edge.to_node === 'system-test-fix-1'
      )
    ).toBe(true);
  });

  it('round-trips both graph contracts as JSON', () => {
    expect(JSON.parse(JSON.stringify(SAMPLE_AUTHOR_GRAPH))).toEqual(SAMPLE_AUTHOR_GRAPH);
    expect(JSON.parse(JSON.stringify(SAMPLE_COMPILED_GRAPH))).toEqual(
      SAMPLE_COMPILED_GRAPH
    );
  });
});
