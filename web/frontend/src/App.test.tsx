import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

describe('App', () => {
  it('requires credentials and a repository before creating a session', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Workflow' })).toBeInTheDocument()
    expect(screen.getByText('Mock Agent')).toBeInTheDocument()
    expect(screen.getByLabelText('Bearer token')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Create session' })).toBeDisabled()
  })
})
