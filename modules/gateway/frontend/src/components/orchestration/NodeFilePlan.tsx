import type { NodeFileGroup } from '@/types/orchestration';

/** Keep cards compact; reveal exact paths grouped by the work they belong to. */
export function NodeFilePlan({ groups }: { groups?: NodeFileGroup[] }) {
  if (!groups?.length) return null;

  const created = groups.reduce((count, group) => count + group.create.length, 0);
  const modified = groups.reduce((count, group) => count + group.modify.length + group.modify_after.length, 0);
  const names = groups.slice(0, 2).map(group => group.group).join(' · ');

  return (
    <details className="mt-2 min-w-0 rounded border border-gray-200 text-xs dark:border-gray-700">
      <summary className="cursor-pointer rounded p-2 text-gray-700 focus-visible:outline-2 focus-visible:outline-blue-500 dark:text-gray-200">
        <span className="font-medium">Planned files</span>
        <span className="ml-2 text-gray-500 dark:text-gray-400">{created} new · {modified} to modify</span>
        <span className="mt-1 block text-gray-500 dark:text-gray-400">
          {names}{groups.length > 2 ? ` · +${groups.length - 2} ${groups.length === 3 ? 'group' : 'groups'}` : ''}
        </span>
      </summary>
      <div className="max-h-80 space-y-4 overflow-y-auto border-t border-gray-200 p-2 dark:border-gray-700">
        {groups.map(group => (
          <section key={group.group} aria-label={group.group}>
            <h4 className="font-medium text-gray-800 dark:text-gray-100">{group.group}</h4>
            {group.purpose && <p className="mt-1 text-gray-500 dark:text-gray-400">{group.purpose}</p>}
            <ul className="mt-2 space-y-2">
              {[
                ...group.create.map(path => ({ path, action: 'Create', after: null })),
                ...group.modify.map(path => ({ path, action: 'Modify', after: null })),
                ...group.modify_after.map(edit => ({ path: edit.path, action: 'Modify', after: edit.created_by })),
              ].map(file => (
                <li key={file.path} className="min-w-0">
                  <span className="text-gray-600 dark:text-gray-400">
                    {file.action}{file.after ? ` after ${file.after}` : ''}
                  </span>
                  <code className="block whitespace-normal break-words text-gray-800 [overflow-wrap:anywhere] dark:text-gray-100">{file.path}</code>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </details>
  );
}
