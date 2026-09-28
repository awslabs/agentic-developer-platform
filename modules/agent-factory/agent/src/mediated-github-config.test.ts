/**
 * Mediated GitHub instructions (issue #5223).
 *
 * Two properties matter here, and they are different in kind:
 *
 *  1. The flag parses exactly like `entrypoint._mediated_github_enabled`. These two
 *     decide the same question in two languages — Python withholds the token, this
 *     module tells the agent what to do instead. If they disagree, the run either
 *     holds a token while being told it has none, or is told to use a mediated path
 *     that is not active. Both are worse than either state alone, so the accepted
 *     spellings are pinned on both sides.
 *
 *  2. The prompt names the real helper API. Prose that teaches a call that does not
 *     exist is worse than no prose: the agent burns the run on an AttributeError
 *     instead of on an auth failure. The signatures asserted below are checked
 *     against `lib/mediated_github.py`'s actual exports by
 *     `tests/test_mediated_github_wiring.py`, which imports the module and compares.
 */

import { MEDIATED_GITHUB_ENABLED, MEDIATED_GITHUB_PROMPT } from './mediated-github-config';

describe('mediated-github-config (#5223)', () => {
  describe('flag parsing', () => {
    it('defaults to false so no environment changes behavior on merge', () => {
      // Evaluated at import time with ADP_MEDIATED_GITHUB_ENABLED unset.
      expect(MEDIATED_GITHUB_ENABLED).toBe(false);
    });

    // Re-imported per case: the export is a module-level const, so the env has to
    // be set before the module is evaluated.
    const parse = (value: string | undefined): boolean => {
      jest.resetModules();
      const original = process.env.ADP_MEDIATED_GITHUB_ENABLED;
      if (value === undefined) delete process.env.ADP_MEDIATED_GITHUB_ENABLED;
      else process.env.ADP_MEDIATED_GITHUB_ENABLED = value;
      try {
        return require('./mediated-github-config').MEDIATED_GITHUB_ENABLED;
      } finally {
        if (original === undefined) delete process.env.ADP_MEDIATED_GITHUB_ENABLED;
        else process.env.ADP_MEDIATED_GITHUB_ENABLED = original;
      }
    };

    // Exactly the set entrypoint._mediated_github_enabled accepts.
    it.each(['1', 'true', 'TRUE', 'yes', 'Yes'])('%s enables', value => {
      expect(parse(value)).toBe(true);
    });

    it.each(['', '0', 'false', 'no', 'off', 'maybe'])('%s does not enable', value => {
      expect(parse(value)).toBe(false);
    });

    it('absent does not enable', () => {
      expect(parse(undefined)).toBe(false);
    });
  });

  describe('the instructions name the real helper API', () => {
    it.each([
      'from lib import mediated_github',
      'publish_commit(',
      'upsert_pull_request(',
      'publish_review(',
      'read_repository(',
    ])('mentions %s', needle => {
      expect(MEDIATED_GITHUB_PROMPT).toContain(needle);
    });

    it('names each error class the helper actually raises', () => {
      for (const cls of ['MediatedConflict', 'MediatedUnavailable', 'MediatedRefused']) {
        expect(MEDIATED_GITHUB_PROMPT).toContain(cls);
      }
    });

    it('passes repo and message to publish_commit, which are its required arguments', () => {
      expect(MEDIATED_GITHUB_PROMPT).toContain("publish_commit(repo='.', message=");
    });
  });

  describe('what the agent must not conclude', () => {
    it('says the auth failure is by design, not a bug to report or route around', () => {
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/by design/i);
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/do not report the auth failure as a bug/i);
    });

    it('forbids hunting for a credential', () => {
      // The failure mode this prevents is the agent "fixing" the auth error by
      // searching for a token — the exfiltration attempt withholding exists to stop.
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/do not look for one/i);
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/no credential anywhere in this environment/i);
    });

    it('states merge is unavailable by every route, not merely discouraged', () => {
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/merge is not available to you/i);
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/human gate/i);
    });

    it('tells the agent local git still works, so it does not stop committing', () => {
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/local git still works/i);
      expect(MEDIATED_GITHUB_PROMPT).toContain('git commit');
    });

    it('marks Refused as terminal and Unavailable as safe to retry', () => {
      // Reversing these wastes a run: retrying a refusal never succeeds, and
      // treating a transient as terminal abandons work that would have landed.
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/MediatedRefused[\s\S]{0,220}will\s*\n?\s*not help/i);
      expect(MEDIATED_GITHUB_PROMPT).toMatch(/MediatedUnavailable[\s\S]{0,200}retrying is safe/i);
    });
  });

  describe('prompt-prefix safety', () => {
    it('interpolates nothing, so it stays run-invariant (#4183)', () => {
      // A run-specific value here would move the prompt-cache boundary.
      expect(MEDIATED_GITHUB_PROMPT).not.toMatch(/\$\{/);
    });

    it('is delimited, so it cannot bleed into neighbouring prompt sections', () => {
      expect(MEDIATED_GITHUB_PROMPT).toContain('<mediated-github>');
      expect(MEDIATED_GITHUB_PROMPT).toContain('</mediated-github>');
    });
  });
});
