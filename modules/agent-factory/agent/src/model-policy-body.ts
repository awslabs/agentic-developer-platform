import { canonicalJson } from './invocability-probe/canonical-json';

export function policyBody(value: unknown): Buffer {
  return Buffer.from(canonicalJson(value).replace(/[\u007f-\uffff]/g,
    c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`));
}
