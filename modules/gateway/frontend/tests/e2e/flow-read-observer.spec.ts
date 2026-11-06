import { test, expect } from '@playwright/test';
import { waitForVisibleFlowNodes } from './flow-read-observer';

test('large ready graph uses bounded browser lookups', async ({ page }) => {
  const refs = Array.from({ length: 100 }, (_, index) => `story-${index}`);
  await page.setContent(refs.map(ref => `<div data-testid="node-${ref}">${ref}</div>`).join(''));
  let lookups = 0;
  const counted = new Proxy(page, {
    get(target, property) {
      if (property === 'getByTestId') return (id: string | RegExp) => {
        lookups++;
        return target.getByTestId(id);
      };
      return Reflect.get(target, property);
    },
  });
  await waitForVisibleFlowNodes(counted, refs);
  // Regression: a ready graph must not pay one protocol lookup per story.
  expect(lookups).toBeLessThanOrEqual(2);
});

test('counts duplicate references and ignores hidden copies', async ({ page }) => {
  await page.setContent(`
    <div data-testid="node-story">first</div>
    <div data-testid="node-story">second</div>
    <div hidden><div data-testid="node-story">hidden duplicate</div></div>
    <div data-testid="node-special[]/雪">literal reference</div>
    <div data-testid="node-unrelated">unrequested node</div>
  `);
  await waitForVisibleFlowNodes(page, ['story', 'story', 'special[]/雪']);
  await expect(waitForVisibleFlowNodes(page, ['story'], 150)).rejects.toThrow();
});

test('waits for delayed visible content within the existing deadline', async ({ page }) => {
  await page.setContent('<div hidden data-testid="node-delayed">delayed</div>');
  await page.evaluate(() => {
    setTimeout(() => { document.querySelector<HTMLElement>('[data-testid="node-delayed"]')!.hidden = false; }, 50);
  });
  await waitForVisibleFlowNodes(page, ['delayed']);
});

for (const content of [
  '<div data-testid="node-other">wrong node</div>',
  '<div style="visibility:hidden" data-testid="node-required">invisible node</div>',
  '<div hidden data-testid="node-required">collapsed node</div>',
]) {
  test(`refuses missing visible content: ${content}`, async ({ page }) => {
    await page.setContent(content);
    await expect(waitForVisibleFlowNodes(page, ['required'], 150)).rejects.toThrow();
  });
}

test('requires all expected nodes to be visible at the same observation', async ({ page }) => {
  await page.setContent('<div data-testid="node-first">first</div><div hidden data-testid="node-second">second</div>');
  // Alternate real DOM visibility; both nodes exist but are never visible
  // together. A sequence of individually successful observations is insufficient.
  await page.evaluate(() => {
    const first = document.querySelector<HTMLElement>('[data-testid="node-first"]')!;
    const second = document.querySelector<HTMLElement>('[data-testid="node-second"]')!;
    let flips = 0;
    setInterval(() => {
      first.hidden = !first.hidden;
      second.hidden = !second.hidden;
      document.body.dataset.flips = String(++flips);
    }, 16);
  });
  await expect(waitForVisibleFlowNodes(page, ['first', 'second'], 300)).rejects.toThrow();
  expect(Number(await page.locator('body').getAttribute('data-flips'))).toBeGreaterThan(1);
});
