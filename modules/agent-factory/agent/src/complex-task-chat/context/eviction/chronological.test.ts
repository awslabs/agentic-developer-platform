import { ChronologicalEviction } from './chronological';
import { ResolvedItem } from '../types';

describe('ChronologicalEviction', () => {
  const evictor = new ChronologicalEviction();

  function makeItem(ordinal: number, tokens: number): ResolvedItem {
    return {
      ordinal,
      message: { role: 'user', content: `msg-${ordinal}` },
      tokens,
      type: 'message',
      id: `msg_${ordinal}`,
    };
  }

  it('returns empty for no evictable items', () => {
    expect(evictor.pick([], 1000, 'hello')).toEqual([]);
  });

  it('keeps all items if budget allows', () => {
    const items = [makeItem(0, 100), makeItem(1, 100), makeItem(2, 100)];
    const kept = evictor.pick(items, 500, 'hello');
    expect(kept).toHaveLength(3);
    expect(kept[0].ordinal).toBe(0);
    expect(kept[2].ordinal).toBe(2);
  });

  it('drops oldest items when budget is tight', () => {
    const items = [makeItem(0, 100), makeItem(1, 100), makeItem(2, 100)];
    const kept = evictor.pick(items, 200, 'hello');
    expect(kept).toHaveLength(2);
    expect(kept[0].ordinal).toBe(1);
    expect(kept[1].ordinal).toBe(2);
  });

  it('keeps items in chronological order', () => {
    const items = [makeItem(0, 50), makeItem(1, 50), makeItem(2, 50), makeItem(3, 50)];
    const kept = evictor.pick(items, 150, 'hello');
    expect(kept).toHaveLength(3);
    // Should keep ordinals 1, 2, 3 (drop 0)
    expect(kept[0].ordinal).toBe(1);
    expect(kept[1].ordinal).toBe(2);
    expect(kept[2].ordinal).toBe(3);
  });

  it('returns nothing if budget is 0', () => {
    const items = [makeItem(0, 100)];
    const kept = evictor.pick(items, 0, 'hello');
    expect(kept).toHaveLength(0);
  });
});
