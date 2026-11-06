export async function pair(invokeRemote, code) {
  if (typeof code !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(code)) {
    throw new Error('Enter the connection code from ADP.');
  }
  const result = await invokeRemote({
    path: '/api/auth/connections/atlassian-mcp/forge/pair',
    method: 'POST', body: { pairing_code: code },
  });
  if (result.status !== 200 || result.body?.status !== 'ready') {
    throw new Error('Connection could not be verified. Check the code in ADP and try again.');
  }
}
