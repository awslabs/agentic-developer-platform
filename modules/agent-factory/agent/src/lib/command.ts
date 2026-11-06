import { execFileSync, execSync, type ExecSyncOptionsWithStringEncoding } from 'child_process';

/** Use argv for commands containing repository, model, or exception text. */
export function runCommand(command: string | readonly string[], options: ExecSyncOptionsWithStringEncoding): string {
  if (typeof command === 'string') return execSync(command, options);
  if (command.length === 0) throw new Error('Command must name an executable');
  return execFileSync(command[0], command.slice(1), { ...options, shell: false });
}

/** Preserve argv through the monitoring callback's existing POSIX shell interface. */
export function shellCommand(file: string, args: readonly string[]): string {
  return [file, ...args].map(value => `'${value.replace(/'/g, "'\\''")}'`).join(' ');
}
