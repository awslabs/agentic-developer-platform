import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { LiveTaskChecklist } from '@/components/LiveTaskChecklist';
import { parseLiveTasks } from '@/utils/liveTasks';
import type { StoryRunInsight } from '@/types/orchestration';
import '../live-activity.css';

/** The same board as Live Agent Activity, retained after the worker exits. */
export function StoryTaskBoard({ insight, accepted = false }: { insight: StoryRunInsight; accepted?: boolean }) {
  const board = insight.task_board;
  const tasks = board ? parseLiveTasks(board.text) : undefined;
  const complete = tasks?.filter(task => task.status === 'completed').length ?? 0;
  const blocked = tasks?.filter(task => task.status === 'blocked').length ?? 0;
  const failure = accepted ? null : insight.failure;
  const link = `/activity?id=${encodeURIComponent(insight.run_id)}`;
  return <div className="mt-3 space-y-2 text-xs">
    {failure && <section aria-label="Run failure diagnosis" className="rounded border border-amber-300 bg-amber-50 p-2 text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
      <p className="font-semibold">Why this run stopped</p>
      <p className="mt-1">{failure.detail}</p>
      <p className="mt-1"><strong>Responsible: </strong>{failure.responsible}</p>
      <p className="mt-1"><strong>Next step: </strong>{failure.next_action}</p>
      {failure.reported_blocker && <div className="mt-2 border-t border-amber-200 pt-2 dark:border-amber-800">
        <p><strong>Reported blocker: </strong>{failure.reported_blocker}</p>
        <p className="mt-1"><strong>Suggested fix: </strong>{failure.suggested_fix}</p>
      </div>}
      {failure.report_text && <details className="mt-2"><summary className="cursor-pointer font-medium">Agent's recorded failure report</summary>
        <p className="mt-1 whitespace-pre-wrap">{failure.report_text}</p>
      </details>}
      <Link className="mt-2 inline-block underline" to={link}>{failure.report_available ? 'Open run report and evidence' : 'Open run details and logs'}</Link>
      <details className="mt-1"><summary className="cursor-pointer">Diagnostic details</summary>
        <p>Attempt {insight.attempt} · {insight.persona} · {failure.code}{failure.exit_code !== null ? ` · exit ${failure.exit_code}` : ''}</p>
      </details>
    </section>}
    <details className="rounded border border-gray-200 p-2 dark:border-gray-700" data-testid="story-task-board">
      <summary className="cursor-pointer font-medium">
        <span aria-live="polite">Task board{tasks ? ` · ${complete}/${tasks.length} tasks complete` : ''}{blocked ? ` · ${blocked} blocked` : ''}</span>
        {tasks && <progress className="mt-1 block h-1.5 w-full accent-blue-600" aria-label="Reported task completion" value={complete} max={tasks.length} />}
      </summary>
      <div className="mt-2 space-y-2">
        <p>Attempt {insight.attempt} · {insight.persona}{insight.outcome ? ' · Run ended' : ''}</p>
        {board && <p>Last reported <time dateTime={board.observed_at}>{new Date(board.observed_at).toLocaleString()}</time></p>}
        {tasks ? <div className="live-activity live-checklist"><LiveTaskChecklist tasks={tasks} counts={new Map()} live={false} /></div>
          : board ? <><p>Task totals are unavailable for this checklist format.</p><div className="live-markdown"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={{ img: () => null }}>{board.text}</ReactMarkdown></div></>
            : <p>{insight.board_access === 'restricted' ? 'Task details require current product-team membership or activity-administrator access. For flows without a product, run owners also have access.' : 'No task board snapshot has been recorded for this run yet. Earlier runs may have a saved checklist in their transcript.'}</p>}
        <p className="text-gray-500 dark:text-gray-400">Agent-reported progress. Delivery still requires the engine's acceptance checks.</p>
        <Link className="inline-block text-blue-600 underline dark:text-blue-400" to={link}>View task activity and evidence</Link>
      </div>
    </details>
  </div>;
}
