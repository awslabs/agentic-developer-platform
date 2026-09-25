# Generic Claude Agent SDK Task runtime

`runner.mjs` owns the SDK lifecycle, loopback ADP model transport, bounded turns,
Task input/cancellation, isolated SDK environment and cleanup. Personas inject the
pinned Claude SDK query function, MCP server definitions, exact allowed tool names,
and their system prompt. The runtime contains no cyber tools or investigation logic.

`protocol.mjs` sends generic `tool.request` frames. The Python Task host resolves
exact names through its trusted `ADP_TASK_TOOL_ROUTES` registry, signs the request,
and attaches Task workload proof. Model processes never receive AWS credentials.
Services validate their own input schemas and current client/Task permissions.

The cyber package supplies its schemas, reporting rules and skills. Its build
copies this shared runtime into the persona distribution; its lockfile pins the
Claude Agent SDK dependency. Existing legacy personas are not migrated by this change.
