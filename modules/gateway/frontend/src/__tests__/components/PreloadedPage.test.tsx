import { Component, Suspense, useState, type ReactNode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { preloadedPage } from '@/routing/preloadedPage';

function deferred() {
  let resolve!: (value: { default: () => ReactNode }) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<{ default: () => ReactNode }>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

it('mounts a completed preload synchronously without showing Suspense or running the page early', async () => {
  const page = vi.fn(() => <h1>Flow</h1>);
  const importer = vi.fn(async () => ({ default: page }));
  const { load, Page } = preloadedPage(importer);
  await load();
  expect(page).not.toHaveBeenCalled();
  const fallback = vi.fn(() => <p>Loading</p>);
  const Fallback = fallback;
  render(<Suspense fallback={<Fallback />}><Page /></Suspense>);
  expect(screen.getByRole('heading', { name: 'Flow' })).toBeVisible();
  expect(fallback).not.toHaveBeenCalled();
  expect(importer).toHaveBeenCalledTimes(1);
});

it('shares a pending preload with the route and preserves mounted page state on rerender', async () => {
  const module = deferred();
  const importer = vi.fn(() => module.promise);
  const { load, Page } = preloadedPage(importer);
  function Counter() {
    const [count, setCount] = useState(0);
    return <button onClick={() => setCount(count + 1)}>Count {count}</button>;
  }
  expect(load()).toBe(load());
  const tree = () => <Suspense fallback={<p>Loading</p>}><Page /></Suspense>;
  const view = render(tree());
  expect(screen.getByText('Loading')).toBeVisible();
  await act(async () => { module.resolve({ default: Counter }); await load(); });
  fireEvent.click(screen.getByRole('button', { name: 'Count 0' }));
  view.rerender(tree());
  expect(screen.getByRole('button', { name: 'Count 1' })).toBeVisible();
  expect(importer).toHaveBeenCalledTimes(1);
});

it('keeps cold routes lazy and renders them when their import resolves', async () => {
  const module = deferred();
  const importer = vi.fn(() => module.promise);
  const { Page } = preloadedPage(importer);
  expect(importer).not.toHaveBeenCalled();
  render(<Suspense fallback={<p>Loading</p>}><Page /></Suspense>);
  expect(screen.getByText('Loading')).toBeVisible();
  await act(async () => { module.resolve({ default: () => <h1>Cold flow</h1> }); });
  expect(screen.getByRole('heading', { name: 'Cold flow' })).toBeVisible();
});

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <p>Route failed</p> : this.props.children; }
}

it('lets the route error boundary handle a rejected code preload', async () => {
  const module = deferred();
  const { load, Page } = preloadedPage(() => module.promise);
  const pending = load();
  module.reject(new Error('Chunk unavailable'));
  await expect(pending).rejects.toThrow('Chunk unavailable');
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  try {
    await act(async () => {
      render(<Boundary><Suspense fallback={<p>Loading</p>}><Page /></Suspense></Boundary>);
    });
    expect(screen.getByText('Route failed')).toBeVisible();
  } finally { error.mockRestore(); }
});
