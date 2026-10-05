import test from "node:test";
import assert from "node:assert/strict";
import { ModelHttpError, retryModelHttp } from "./model-http.js";

const signal = () => new AbortController().signal;
test("transient HTTP rejections retry with bounded backoff and return success", async () => {
  for (const status of [429, 500, 502, 503, 504]) {
    let attempts = 0;
    const waits: number[] = [];
    const result = await retryModelHttp(async () => ++attempts < 3 ? { httpStatus: status } : { value: "ok" }, signal(), async ms => { waits.push(ms); });
    assert.deepEqual(result, { value: "ok" });
    assert.equal(attempts, 3);
    assert.deepEqual(waits, [1000, 2000]);
  }
});
test("persistent errors exhaust three attempts and expose only status", async () => {
  let attempts = 0;
  await assert.rejects(retryModelHttp(async () => { attempts++; return { httpStatus: 503 }; }, signal(), async () => {}),
    { message: "Model request failed: HTTP 503" });
  assert.equal(attempts, 3);
});
test("permanent HTTP errors are never retried", async () => {
  for (const status of [400, 401, 403, 404, 409, 422]) {
    let attempts = 0;
    await assert.rejects(retryModelHttp(async () => { attempts++; return { httpStatus: status }; }, signal()), ModelHttpError);
    assert.equal(attempts, 1);
  }
});
test("unknown transport, malformed response and admission/budget errors are not replayed", async () => {
  for (const error of [new TypeError("network failed"), new SyntaxError("invalid JSON"), new Error("budget exhausted")]) {
    let attempts = 0;
    await assert.rejects(retryModelHttp(async () => { attempts++; throw error; }, signal()), value => value === error);
    assert.equal(attempts, 1);
  }
});
test("abort during backoff prevents another attempt", async () => {
  const controller = new AbortController();
  let attempts = 0;
  await assert.rejects(retryModelHttp(async () => { attempts++; controller.abort(); return { httpStatus: 500 }; }, controller.signal), { name: "AbortError" });
  assert.equal(attempts, 1);
});
test("every retry passes through the caller's budget/admission gate", async () => {
  let claims = 0;
  await assert.rejects(retryModelHttp(async () => {
    if (++claims > 1) throw new Error("budget exhausted");
    return { httpStatus: 500 };
  }, signal(), async () => {}), /budget exhausted/);
  assert.equal(claims, 2);
});
