/** Frozen-contract fixtures used before the production React Flow editor exists. */

import type { AuthorGraph, CompiledGraph, WorkflowEdge, WorkflowNode } from '../api/protocol';

const authorNodes: WorkflowNode[] = [
  {
    id: 'input-1',
    node_type: 'input',
    title: 'Accept bug report',
    instruction: 'Collect bug details and reproduction steps.',
    assignment_mode: 'auto',
    recommended_agents: [],
    allowed_files_candidate: [],
    new_files_candidate: [],
    allowed_commands_candidate: [],
    risk_level_hint: 'L0',
    requires_write: false,
    system_managed: false,
  },
  {
    id: 'investigate-1',
    node_type: 'agent_task',
    task_kind: 'analyze',
    title: 'Investigate root cause',
    instruction: 'Analyze the relevant code and evidence.',
    assignment_mode: 'auto',
    recommended_agents: [
      {
        agent_id: 'agent-opencode-1',
        score: 80,
        reason: 'Supports code analysis.',
      },
    ],
    allowed_files_candidate: ['src/auth/login.ts'],
    new_files_candidate: [],
    allowed_commands_candidate: [],
    risk_level_hint: 'L1',
    requires_write: false,
    system_managed: false,
  },
  {
    id: 'fix-1',
    node_type: 'agent_task',
    task_kind: 'implement',
    title: 'Implement fix',
    instruction: 'Apply the smallest change that fixes the root cause.',
    assignment_mode: 'auto',
    recommended_agents: [
      {
        agent_id: 'agent-opencode-1',
        score: 100,
        reason: 'Supports bounded writes and patch generation.',
      },
    ],
    allowed_files_candidate: ['src/auth/login.ts'],
    new_files_candidate: ['tests/auth/login.test.ts'],
    allowed_commands_candidate: [['npm', 'test', '--', 'login.test.ts']],
    risk_level_hint: 'L2',
    requires_write: true,
    system_managed: false,
  },
  {
    id: 'output-1',
    node_type: 'output',
    title: 'Summarize fix and verification',
    assignment_mode: 'auto',
    recommended_agents: [],
    allowed_files_candidate: [],
    new_files_candidate: [],
    allowed_commands_candidate: [],
    risk_level_hint: 'L0',
    requires_write: false,
    system_managed: false,
  },
];

const authorEdges: WorkflowEdge[] = [
  {
    id: 'edge-1',
    from_node: 'input-1',
    to_node: 'investigate-1',
    condition: 'success',
    system_managed: false,
  },
  {
    id: 'edge-2',
    from_node: 'investigate-1',
    to_node: 'fix-1',
    condition: 'success',
    system_managed: false,
  },
  {
    id: 'edge-3',
    from_node: 'fix-1',
    to_node: 'output-1',
    condition: 'success',
    system_managed: false,
  },
];

export const SAMPLE_AUTHOR_GRAPH: AuthorGraph = {
  schema_version: '1',
  nodes: authorNodes,
  edges: authorEdges,
};

export const SAMPLE_COMPILED_GRAPH: CompiledGraph = {
  schema_version: '1',
  source_author_hash: 'a'.repeat(64),
  integration_base_commit: 'b'.repeat(40),
  policy_version: 'demo-v1',
  agent_catalog_snapshot_hash: 'c'.repeat(64),
  nodes: [
    { ...authorNodes[0], source_node_id: 'input-1' },
    {
      ...authorNodes[1],
      source_node_id: 'investigate-1',
      resolved_agent_id: 'agent-opencode-1',
      resolved_agent_spec_sha256: 'd'.repeat(64),
      effective_allowed_files: ['src/auth/login.ts'],
      effective_new_files: [],
      effective_allowed_commands: [],
      policy_risk_floor: 'L1',
      requires_changeset_approval: false,
    },
    {
      ...authorNodes[2],
      source_node_id: 'fix-1',
      resolved_agent_id: 'agent-opencode-1',
      resolved_agent_spec_sha256: 'd'.repeat(64),
      effective_allowed_files: ['src/auth/login.ts'],
      effective_new_files: ['tests/auth/login.test.ts'],
      effective_allowed_commands: [['npm', 'test', '--', 'login.test.ts']],
      policy_risk_floor: 'L2',
      requires_changeset_approval: true,
    },
    {
      id: 'system-test-fix-1',
      node_type: 'test',
      title: 'Run focused login tests',
      assignment_mode: 'auto',
      recommended_agents: [],
      allowed_files_candidate: [],
      new_files_candidate: [],
      allowed_commands_candidate: [],
      risk_level_hint: 'L2',
      requires_write: false,
      system_managed: true,
      source_node_id: 'fix-1',
      system_rule_id: 'rule-test-after-write',
      test_kind: 'command',
      test_argv: ['npm', 'test', '--', 'login.test.ts'],
    },
    { ...authorNodes[3], source_node_id: 'output-1' },
  ],
  edges: [
    authorEdges[0],
    authorEdges[1],
    {
      id: 'system-edge-fix-test',
      from_node: 'fix-1',
      to_node: 'system-test-fix-1',
      condition: 'success',
      system_managed: true,
    },
    {
      id: 'system-edge-test-output',
      from_node: 'system-test-fix-1',
      to_node: 'output-1',
      condition: 'success',
      system_managed: true,
    },
  ],
};

function edgesReferenceKnownNodes(nodes: WorkflowNode[], edges: WorkflowEdge[]): boolean {
  const ids = new Set(nodes.map((node) => node.id));
  return edges.every((edge) => ids.has(edge.from_node) && ids.has(edge.to_node));
}

export function validateAuthorGraphType(graph: AuthorGraph): boolean {
  return (
    graph.schema_version === '1' &&
    graph.nodes.every((node) => node.id.length > 0 && node.title.length > 0) &&
    edgesReferenceKnownNodes(graph.nodes, graph.edges)
  );
}

export function validateCompiledGraphType(graph: CompiledGraph): boolean {
  return (
    graph.schema_version === '1' &&
    /^[0-9a-f]{64}$/.test(graph.source_author_hash) &&
    /^[0-9a-f]{40}([0-9a-f]{24})?$/.test(graph.integration_base_commit) &&
    graph.nodes.every((node) => typeof node.source_node_id === 'string') &&
    edgesReferenceKnownNodes(graph.nodes, graph.edges)
  );
}
