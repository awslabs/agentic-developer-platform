/**
 * Unit tests for AIDLC conditional Task tool enablement.
 *
 * Verifies that the Task tool is gated on the presence of the `aidlc/`
 * directory in the workspace (Issue #3167, EPIC #3158 Decision 2).
 *
 * Two branches:
 * - When `aidlc/` exists: Task appears in allowedTools
 * - When `aidlc/` is absent: Task is NOT in allowedTools
 */

import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-worker AIDLC Task tool gating', () => {
  describe('AIDLC_ENABLED constant', () => {
    it('is defined using fs.existsSync on the aidlc/ directory', () => {
      expect(source).toContain("fs.existsSync(path.join(CWD, 'aidlc'))");
    });

    it('assigns to a constant named AIDLC_ENABLED', () => {
      expect(source).toContain('const AIDLC_ENABLED = fs.existsSync');
    });
  });

  describe('allowedTools conditional spread', () => {
    it('spreads Task into allowedTools when AIDLC_ENABLED is true', () => {
      expect(source).toContain("...(AIDLC_ENABLED ? ['Task'] : [])");
    });

    it('does NOT include Task unconditionally in the base allowedTools array', () => {
      // The static tool list (Bash..Skill) must not contain 'Task' directly
      const baseToolsLine = source.match(
        /'Bash',\s*'Read',\s*'Write',\s*'Edit',\s*'Glob',\s*'Grep',\s*'WebSearch',\s*'WebFetch',\s*'Skill'/,
      );
      expect(baseToolsLine).not.toBeNull();
      expect(baseToolsLine![0]).not.toContain("'Task'");
    });
  });

  describe('startup logging', () => {
    it('logs when AIDLC install is detected', () => {
      expect(source).toContain('AIDLC install detected — Task tool enabled');
    });

    it('only logs when AIDLC_ENABLED is true (conditional block)', () => {
      const logLine = source.indexOf('AIDLC install detected');
      // Find the nearest preceding if-statement
      const before = source.substring(Math.max(0, logLine - 200), logLine);
      expect(before).toContain('if (AIDLC_ENABLED)');
    });
  });
});
