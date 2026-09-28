import * as fs from 'fs';
import * as path from 'path';

/** Shared presentation policy, independent of the chosen/overridden persona. */
export function loadHumanCommunication(personaDirs: string[]): string {
  const candidates = [...personaDirs, '/app/personas', path.resolve(__dirname, '../../rules/personas')];
  for (const dir of candidates) {
    const file = path.join(dir, 'shared', 'human-communication.md');
    if (fs.existsSync(file)) return fs.readFileSync(file, 'utf-8');
  }
  console.warn('[persona-loader] Shared human communication policy is missing from the installation');
  return '';
}
