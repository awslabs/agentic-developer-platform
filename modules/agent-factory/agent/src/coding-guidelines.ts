import * as fs from 'fs';
import * as path from 'path';

/** Installed policy, independent of repository overrides and working directory. */
export function loadCodingGuidelines(): string {
  const candidates = [
    path.resolve(__dirname, '../rules/coding-guidelines.md'), // worker image
    path.resolve(__dirname, '../../rules/coding-guidelines.md'), // source checkout
  ];
  for (const file of candidates) {
    if (fs.existsSync(file)) {
      const content = fs.readFileSync(file, 'utf-8');
      if (!content.trim()) throw new Error('Installed coding guidelines are empty');
      return content;
    }
  }
  throw new Error('Shared coding guidelines are missing from the installation');
}
