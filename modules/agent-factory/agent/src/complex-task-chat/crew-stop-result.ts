import { constants } from 'node:fs';
import { mkdir, open, realpath, rename, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';

export type CrewStopResult = {
  status: 'checkpoint_unverified';
  last_verified_checkpoint_id: null;
  last_verified_checkpoint_id_known: false;
  consolidation: 'completed' | 'failed';
};

export async function recordCrewStop(home: string, consolidation: CrewStopResult['consolidation']): Promise<CrewStopResult> {
  if (resolve(home) !== home) throw new Error('Crew stop requires the admitted home');
  const directory = join(home, 'adp-crew-session-map');
  await mkdir(directory, { recursive: true, mode: 0o700 });
  if (await realpath(directory) !== join(await realpath(home), 'adp-crew-session-map')) {
    throw new Error('Crew stop map must not be a symlink');
  }
  const result: CrewStopResult = {
    status: 'checkpoint_unverified', last_verified_checkpoint_id: null,
    last_verified_checkpoint_id_known: false, consolidation,
  };
  const path = join(directory, '.stop-result.json');
  const temporary = `${path}.${process.pid}.tmp`;
  const handle = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { await handle.writeFile(JSON.stringify(result) + '\n'); await handle.sync(); }
  finally { await handle.close(); }
  try { await rename(temporary, path); }
  catch (error) { await rm(temporary, { force: true }); throw error; }
  return result;
}
