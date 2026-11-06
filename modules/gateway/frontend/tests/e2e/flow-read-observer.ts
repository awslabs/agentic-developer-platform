import { expect, type Page } from '@playwright/test';

/** Observe the whole graph together, without a browser round trip per node. */
export async function waitForVisibleFlowNodes(page: Page, nodeRefs: string[], timeout = 3_000): Promise<void> {
  const expected = new Map<string, number>();
  for (const ref of nodeRefs) expected.set(`node-${ref}`, (expected.get(`node-${ref}`) ?? 0) + 1);
  const ids = [...expected.keys()];
  const visible = page.getByTestId(/^node-/).filter({ visible: true });
  await expect.poll(async () => visible.evaluateAll((elements, ids) => {
    const counts = new Map<string, number>();
    for (const element of elements) {
      const id = element.getAttribute('data-testid')!;
      counts.set(id, (counts.get(id) ?? 0) + 1);
    }
    return ids.map(id => counts.get(id) ?? 0);
  }, ids), { timeout }).toEqual([...expected.values()]);
}
