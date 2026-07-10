import { Activity, Bot, Network } from 'lucide-react'
import './App.css'

const agents = [
  { name: 'Mock Agent', status: 'available' },
  { name: 'OpenCode', status: 'adapter pending' },
  { name: 'Codex', status: 'adapter pending' },
  { name: 'Claude Code', status: 'adapter pending' },
]

function App() {
  return (
    <main className="app-shell">
      <header className="app-header">
        <div className="brand">
          <Network aria-hidden="true" size={20} />
          <strong>Agent Hub</strong>
        </div>
        <span className="baseline-status">Baseline ready</span>
      </header>

      <aside className="agent-panel" aria-label="Agent catalog">
        <h2>
          <Bot aria-hidden="true" size={16} />
          Agents
        </h2>
        <ul>
          {agents.map((agent) => (
            <li key={agent.name}>
              <span>{agent.name}</span>
              <small>{agent.status}</small>
            </li>
          ))}
        </ul>
      </aside>

      <section className="workflow-panel" aria-labelledby="workflow-title">
        <div className="panel-heading">
          <div>
            <p>Author graph</p>
            <h1 id="workflow-title">Workflow</h1>
          </div>
          <span className="phase-label">HUB-000</span>
        </div>
        <div className="canvas-placeholder">
          <Network aria-hidden="true" size={32} />
          <p>Protocol graph pending contract freeze</p>
        </div>
      </section>

      <footer className="activity-bar">
        <Activity aria-hidden="true" size={16} />
        <span>Project baseline initialized</span>
      </footer>
    </main>
  )
}

export default App
