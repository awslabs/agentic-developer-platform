/** Live-evaluation fault injection. Does not replace SDK messages or its transport. */
export function injectReadFailure<T>(
  stream: AsyncIterable<T>,
  shouldFail: () => boolean,
  onFailure: () => void,
): void {
  const original = stream[Symbol.asyncIterator].bind(stream);
  stream[Symbol.asyncIterator] = () => {
    const iterator = original();
    let injected = false;
    return {
      async next(...args: [] | [undefined]) {
        if (!injected && shouldFail()) {
          injected = true;
          onFailure();
          throw new Error('Claude SDK API error: overloaded_error — injected stream failure');
        }
        return iterator.next(...args);
      },
      ...(iterator.return ? { return: iterator.return.bind(iterator) } : {}),
      ...(iterator.throw ? { throw: iterator.throw.bind(iterator) } : {}),
    };
  };
}
