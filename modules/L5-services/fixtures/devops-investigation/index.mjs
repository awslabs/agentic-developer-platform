// Deterministic synthetic telemetry. No network, SDK, environment, or secrets.
export async function handler(event) {
  if (!event || Object.keys(event).sort().join(',') !== 'correlation_id,scenario,schema_version' ||
      event.schema_version !== 1 || !['dependency_timeout', 'recovered'].includes(event.scenario) ||
      typeof event.correlation_id !== 'string' || !/^[a-f0-9]{16}$/.test(event.correlation_id)) {
    throw new Error('fixture_input_invalid');
  }
  const record = {
    schema_version: 1,
    fixture: 'l5-devops-investigation',
    synthetic: true,
    correlation_id: event.correlation_id,
    scenario: event.scenario,
    outcome: event.scenario === 'dependency_timeout' ? 'failed' : 'recovered',
    code: event.scenario === 'dependency_timeout' ? 'SYNTHETIC_DEPENDENCY_TIMEOUT' : 'SYNTHETIC_RECOVERY',
  };
  console.log(JSON.stringify(record));
  if (event.scenario === 'dependency_timeout') {
    throw new Error('SYNTHETIC_DEPENDENCY_TIMEOUT');
  }
  return record;
}
