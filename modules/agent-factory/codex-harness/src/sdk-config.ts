import type { CodexOptions } from "@openai/codex-sdk";

/** Host-owned baseline for the pinned SDK. A persona cannot re-enable features.
 * Native agents/goals bypass ADP's delegated lifecycle; shell tools must instead
 * come from the capability broker. This is one layer, NOT filesystem isolation:
 * the pinned SDK still advertises view_image and request_user_input. Registration
 * remains blocked until residual built-ins and broker permissions are qualified.
 */
export function restrictedSdkConfig(): NonNullable<CodexOptions["config"]> {
  return { features: {
    multi_agent: false, goals: false, shell_tool: false, unified_exec: false,
    shell_snapshot: false, apps: false, remote_plugin: false, hooks: false,
    skill_mcp_dependency_install: false,
  } };
}
