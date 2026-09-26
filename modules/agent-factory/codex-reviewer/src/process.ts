import { spawn } from "node:child_process";

export interface RunOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  allowFailure?: boolean;
  input?: string;
}

export interface RunResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

export class ProcessError extends Error {
  constructor(command: string, args: string[], public readonly result: RunResult, signal: string | null) {
    const diagnostics = [result.stdout.trim(), result.stderr.trim()].filter(Boolean).join("\n").slice(0, 8192);
    const invocation = `${command} ${args.join(" ")}`.slice(0, 512);
    super(`${invocation} exited ${signal ? `on signal ${signal}` : result.exitCode}: ${diagnostics}`);
    this.name = "ProcessError";
  }
}

export async function run(
  command: string,
  args: string[],
  options: RunOptions = {},
): Promise<RunResult> {
  return await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env ?? process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stdin.end(options.input);
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => (stdout += chunk));
    child.stderr.on("data", (chunk: string) => (stderr += chunk));
    child.on("error", reject);
    child.on("close", (exitCode, signal) => {
      const result = { stdout, stderr, exitCode: exitCode ?? 1 };
      if (result.exitCode !== 0 && !options.allowFailure) {
        reject(new ProcessError(command, args, result, signal));
      } else {
        resolve(result);
      }
    });
  });
}
