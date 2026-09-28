import assert from "node:assert/strict";
import test from "node:test";
import { ModelExecutionBudget } from "./model-budget.js";

test("model deadline includes retained turns but excludes external CI waits", async () => {
  let clock = 0;
  const budget = new ModelExecutionBudget(100, () => clock);
  await budget.run(async () => { clock += 60; });
  clock += 900000; // CI can run for fifteen minutes without model work.
  await budget.run(async () => { clock += 40; });
  await assert.rejects(budget.run(async () => assert.fail("budget must not reset")), /deadline exhausted/);
});

test("failed turns also consume the shared execution deadline", async () => {
  let clock = 0;
  const budget = new ModelExecutionBudget(10, () => clock);
  await assert.rejects(budget.run(async () => { clock += 11; throw new Error("provider refusal"); }), /provider refusal/);
  await assert.rejects(budget.run(async () => {}), /deadline exhausted/);
});
