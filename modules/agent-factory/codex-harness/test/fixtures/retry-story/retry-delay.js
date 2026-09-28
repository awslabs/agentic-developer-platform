'use strict';

// Existing API; currently lacks validation, defaults, capping and jitter.
function retryDelay(options) {
  return options.baseMs * 2 ** options.attempt;
}

module.exports = { retryDelay };
