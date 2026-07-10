# Agent Hub Frontend

The frontend is a Vite, React, and TypeScript application. `HUB-000` provides the tested shell and pinned dependencies; graph contracts and generated TypeScript types are owned by `HUB-010`.

```powershell
npm ci
npm run lint
npm run test
npm run build
npm run dev
```

Runtime workflow data must come from the Agent Hub API. The browser must never invoke coding-agent CLIs directly.
