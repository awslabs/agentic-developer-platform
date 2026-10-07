const { readFileSync, writeFileSync } = require('node:fs');
const { dirname } = require('node:path');
const { TOKEN_FILE_PATH, writeTokenFile } = require('../../src/lib/tokenFile');

test('token writes use the isolated path captured before module import', () => {
  // Refuse BEFORE writing if isolation regresses, even with no inherited path.
  expect(dirname(dirname(TOKEN_FILE_PATH))).toBe(process.env.TOKEN_ISOLATION_ROOT);
  expect(TOKEN_FILE_PATH).not.toBe(process.env.TOKEN_ISOLATION_RUNTIME_FILE);
  writeTokenFile('fixture-token');
  expect(readFileSync(TOKEN_FILE_PATH, 'utf8')).toBe('fixture-token');
  writeFileSync(process.env.TOKEN_ISOLATION_RECEIPT, TOKEN_FILE_PATH);
  if (process.env.TOKEN_ISOLATION_FAIL === 'true') throw new Error('intentional fixture failure');
});
