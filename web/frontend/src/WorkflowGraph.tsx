import { graphlib, layout as arrange } from '@dagrejs/dagre'
import { Background, Controls, Position, ReactFlow } from '@xyflow/react'
import type { Node, Edge } from '@xyflow/react'
import type { AuthorGraph, CompiledGraph, WorkflowLayout } from './api/protocol'
import '@xyflow/react/dist/style.css'

const width = 184
const height = 64

export function WorkflowGraph({ graph, layout, selected, onSelect, statuses }: {
  graph: AuthorGraph | CompiledGraph
  layout?: WorkflowLayout
  selected?: string
  onSelect?: (id: string) => void
  statuses?: Record<string, string>
}) {
  const positions = new Map(layout?.nodes.map((node) => [node.node_id, node.position]))
  const dag = new graphlib.Graph()
    .setGraph({ rankdir: 'LR', ranksep: 50, nodesep: 32 })
    .setDefaultEdgeLabel(() => ({}))
  for (const node of graph.nodes) dag.setNode(node.id, { width, height })
  for (const edge of graph.edges) dag.setEdge(edge.from_node, edge.to_node)
  arrange(dag)
  const nodes: Node[] = graph.nodes.map((node) => ({
    id: node.id,
    position: positions.get(node.id) ?? {
      x: dag.node(node.id).x - width / 2, y: dag.node(node.id).y - height / 2,
    },
    ariaLabel: node.title,
    selected: selected === node.id,
    style: { width, minHeight: height },
    data: { label: <><strong title={node.title}>{node.title}</strong><small>{statuses?.[node.id] ?? node.node_type}</small></> },
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
  }))
  const edges: Edge[] = graph.edges.map((edge) => ({
    id: edge.id, source: edge.from_node, target: edge.to_node,
  }))
  return (
    <ReactFlow nodes={nodes} edges={edges} nodesDraggable={false} nodesConnectable={false}
      elementsSelectable={!!onSelect} onNodeClick={(_, node) => onSelect?.(node.id)}
      fitView minZoom={0.15} maxZoom={1.5}>
      <Background color="#c9d3db" gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}
