const BASE = import.meta.env.DEV ? '' : ''

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, opts)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText} ${text}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => req<T>(path),
  put: <T>(path: string, body: unknown) =>
    req<T>(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  post: <T>(path: string, body?: unknown) =>
    req<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  delete: <T>(path: string) => req<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => req<T>(path, { method: 'POST', body: form }),
}
