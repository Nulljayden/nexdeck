/**
 * System > Extensions: what is imported, what could not be read, and an
 * import that sends the file's text as it is.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ExtensionsSettings } from './ExtensionsSettings'

const state = vi.hoisted(() => ({ posts: [] as { path: string; body: unknown }[] }))

vi.mock('../../api/client', () => {
  class ApiError extends Error {}
  return {
    ApiError,
    api: vi.fn(async () => new Response('id: whisparr\n')),
    get: vi.fn(async () => ({
      folder: '/data/extensions',
      extensions: [
        {
          id: 'whisparr', kind: 'ext-whisparr', name: 'Whisparr', version: '1.0.0', description: 'The queue.', author: 'nexdeck',
          icon: 'whisparr', category: 'downloads', docs_url: '', file: 'whisparr.yaml', connections: 2,
          widgets: [{ id: 'queue', name: 'Queue', renderer: 'list' }, { id: 'health', name: 'Health', renderer: 'value' }],
        },
      ],
      broken: [{ file: 'half.yaml', error: 'widgets: Field required' }],
    })),
    post: vi.fn(async (path: string, body: unknown) => {
      state.posts.push({ path, body })
      return { name: 'Simple', version: '1.0.0' }
    }),
    del: vi.fn(async () => undefined),
  }
})

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ExtensionsSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ExtensionsSettings', () => {
  it('lists the imported extensions with their cards, and the files that failed', async () => {
    show()
    expect(await screen.findByText('Whisparr')).toBeInTheDocument()
    expect(screen.getByText('Queue')).toBeInTheDocument()
    expect(screen.getByText('Health')).toBeInTheDocument()
    expect(screen.getByText('half.yaml')).toBeInTheDocument()
    expect(screen.getByText('widgets: Field required')).toBeInTheDocument()
  })

  it('imports a pasted file as it was written', async () => {
    show()
    await screen.findByText('Whisparr')
    await userEvent.click(screen.getByRole('button', { name: 'Paste' }))
    await userEvent.type(screen.getByRole('textbox'), 'id: simple')
    await userEvent.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(state.posts).toContainEqual({ path: '/extensions', body: { text: 'id: simple' } }))
  })
})
