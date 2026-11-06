// Preserve server page order and newer individual readbacks when pages overlap.
export function appendReadbackPage<T extends object>(current: T[], page: T[], identity: (item: T) => string): T[] {
  const revision = (item: T) => 'revision' in item && typeof item.revision === 'number' ? item.revision : 0;
  const items = new Map(current.map(item => [identity(item), item]));
  for (const item of page) {
    const previous = items.get(identity(item));
    if (!previous || revision(item) > revision(previous)) items.set(identity(item), item);
  }
  return [...items.values()];
}
