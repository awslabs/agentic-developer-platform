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

The trusted Python host can publish additional report artifacts with
`ADP_TASK_REPORT_RENDERERS`, a JSON mapping from persona to a fixed
`package.module:function`. The callable receives the validated report and a
bounded record of tool receipts, and returns `content` bytes and `content_type`.
It is configured by the operator, never by Task inputs or model output. Domain
renderers live with their app; generic output storage and authorization remain
in the Task framework.
