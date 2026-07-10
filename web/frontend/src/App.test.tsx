import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

describe('App', () => {
  it('renders the HUB-000 baseline shell', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Workflow' })).toBeInTheDocument()
    expect(screen.getByText('Mock Agent')).toBeInTheDocument()
    expect(screen.getByText('HUB-000')).toBeInTheDocument()
  })
})
