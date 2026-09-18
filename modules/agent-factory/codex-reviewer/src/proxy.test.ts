import assert from "node:assert/strict";
import test from "node:test";
import { isAllowedProxyRequest } from "./proxy.js";

test("proxy permits only POST requests to the Codex Responses surface", () => {
  assert.equal(isAllowedProxyRequest("POST", "/openai/v1/responses"), true);
  assert.equal(isAllowedProxyRequest("POST", "/openai/v1/responses/compact"), true);
  assert.equal(isAllowedProxyRequest("GET", "/openai/v1/responses"), false);
  assert.equal(isAllowedProxyRequest("POST", "/internal/v1/github-installation-token"), false);
});

test("proxy rejects traversal and encoded traversal toward privileged routes", () => {
  assert.equal(
    isAllowedProxyRequest("POST", "/openai/v1/responses/../../../internal/v1/github-installation-token"),
    false,
  );
  assert.equal(
    isAllowedProxyRequest("POST", "/openai/v1/responses/%2e%2e/%2e%2e/internal"),
    false,
  );
  assert.equal(isAllowedProxyRequest("POST", "/openai/v1/responses?target=internal"), false);
});
