import { afterEach, expect, it, vi } from 'vitest'
import { createApi } from './client'

afterEach(() => vi.unstubAllGlobals())

it('reuses the exact body and key after an uncertain mutation response', async () => {
  const fetch = vi.fn()
    .mockRejectedValueOnce(new TypeError('connection interrupted'))
    .mockResolvedValueOnce(new Response('{}', { status: 202 }))
  vi.stubGlobal('fetch', fetch)
  const body = { expected_semantic_version: 1 }
  const retry = createApi('synthetic-test-token').mutation('POST', '/sessions/example/plans', body)
  await expect(retry()).rejects.toThrow()
  body.expected_semantic_version = 2
  await retry()
  const [first, second] = fetch.mock.calls
  expect(first[0]).toBe('/api/v1/sessions/example/plans')
  expect(first[1].body).toBe('{"expected_semantic_version":1}')
  expect(second[1].body).toBe(first[1].body)
  const key = first[1].headers.get('Idempotency-Key')
  expect(key).toBeTruthy()
  expect(second[1].headers.get('Idempotency-Key')).toBe(key)
  expect(second[1].headers.get('Authorization')).toBe('Bearer synthetic-test-token')
  expect(second[1].redirect).toBe('error')
})

it('rejects a diff that is not bound to the pending approval patch', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('unexpected patch', {
    headers: { 'X-Content-SHA256': 'different' },
  })))
  await expect(createApi('synthetic').diff('/diff', 'expected')).rejects.toThrow('patch hash')
})
