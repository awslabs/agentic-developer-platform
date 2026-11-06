import { createElement, lazy, type ComponentType } from 'react';

/** Share a route's code preload with its first render; no page data is cached. */
export function preloadedPage(importPage: () => Promise<{ default: ComponentType }>) {
  let component: ComponentType | undefined;
  let pending: Promise<{ default: ComponentType }> | undefined;
  const load = () => pending ??= importPage().then(module => {
    component = module.default;
    return module;
  });
  const LazyPage = lazy(load);
  // React.lazy suspends on its first render even if a separate import already
  // finished. Render completed preloads directly so an enclosing Suspense
  // boundary does not delay mounting the page and starting its data request.
  const Page = () => createElement(component ?? LazyPage);
  return { load, Page };
}
