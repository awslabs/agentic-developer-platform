import { mkdtemp, mkdir, readFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { recordCrewStop } from './crew-stop-result';

describe('retained Crew stop record', () => {
  let home: string;
  beforeEach(async () => { home = await mkdtemp(join(tmpdir(), 'adp-crew-stop-')); });
  afterEach(async () => { await rm(home, { recursive: true, force: true }); });

  it('records the local outcome without claiming a verified checkpoint or known pointer', async () => {
    const record = await recordCrewStop(home, 'completed');
    expect(record).toEqual({ status: 'checkpoint_unverified', last_verified_checkpoint_id: null,
      last_verified_checkpoint_id_known: false, consolidation: 'completed' });
    expect(JSON.parse(await readFile(join(home, 'adp-crew-session-map', '.stop-result.json'), 'utf8'))).toEqual(record);
    const failure = await recordCrewStop(home, 'failed');
    expect(JSON.parse(await readFile(join(home, 'adp-crew-session-map', '.stop-result.json'), 'utf8'))).toEqual(failure);
    expect(failure.consolidation).toBe('failed');
  });

  it('refuses a session map symlink', async () => {
    const outside = join(home, 'outside');
    await mkdir(outside);
    await symlink(outside, join(home, 'adp-crew-session-map'));
    await expect(recordCrewStop(home, 'failed')).rejects.toThrow('must not be a symlink');
  });
});
