export function crewToolsServer(adapter: { baseUrl: string; secret: string }, runId: string | undefined) {
  if (!/^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(adapter.baseUrl) || !/^[a-f0-9]{64}$/.test(adapter.secret) ||
      !runId || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(runId)) {
    throw new Error('Scoped Crew tools adapter unavailable');
  }
  return { command: 'adp', args: ['mcp'], env: {
    ADP_CREW_TOOLS_URL: adapter.baseUrl, ADP_CREW_TOOLS_TOKEN: adapter.secret, ADP_CREW_RUN_ID: runId,
  } };
}

export type CrewToolsServer = ReturnType<typeof crewToolsServer>;

/** The installed Crew bundle prefix on a Dev Box (install_crew.py keeps `current` at the active revision). */
export const CREW_HOST_BUNDLE_PREFIX = '/opt/adp/crew/current';

/**
 * The same shim on an EC2 Dev Box (#8380): the bundle carries `adp-mcp.py` under `host/adp/` and
 * the relocatable Python the Crew venv runs on, so the server is addressed by path rather than
 * through an `adp` launcher on PATH. The binding is identical to the pod's: the loopback model
 * adapter is the tools endpoint and the current host run is the only run it will speak for.
 */
export function crewHostToolsServer(adapter: { baseUrl: string; secret: string }, runId: string | undefined,
  prefix: string = CREW_HOST_BUNDLE_PREFIX) {
  if (!/^\/[A-Za-z0-9_./-]+$/.test(prefix) || prefix.includes('..')) throw new Error('Scoped Crew tools adapter unavailable');
  const { env } = crewToolsServer(adapter, runId);
  return { command: `${prefix}/venv/bin/python3`, args: [`${prefix}/host/adp/adp-mcp.py`], env };
}
