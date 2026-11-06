import test from "node:test";
import assert from "node:assert/strict";
import {handler, validateInvoice} from "./index.mjs";
test("valid zero and bounded integer amounts", () => {
  assert.equal(validateInvoice('{"invoice_id":"example-1","amount_cents":0}').amount_cents, 0);
  for (const amount of [-1, 1.5, 1000001]) assert.throws(() => validateInvoice(JSON.stringify({invoice_id:"sample",amount_cents:amount})));
});
test("partial failures preserve only failed message IDs", async () => {
  const result = await handler({Records:[{messageId:"good",body:'{"invoice_id":"a","amount_cents":17}'},{messageId:"bad",body:"not-json"}]});
  assert.deepEqual(result, {batchItemFailures:[{itemIdentifier:"bad"}]});
});
test("unsafe IDs and ambiguous envelope fail", async () => {
  assert.throws(() => validateInvoice('{"invoice_id":"../escape","amount_cents":1}'));
  await assert.rejects(handler({Records:[{body:"{}"}]}), /message_id_required/);
});
