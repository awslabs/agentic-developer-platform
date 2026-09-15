import { Link } from 'react-router-dom';
import type { GraphNode } from '@/types/orchestration';
import { storyJourney } from '@/utils/storyJourney';

/** Keep the whole route visible even on narrow story cards. */
export function StoryJourney({ node }: { node: GraphNode }) {
  const { headline, steps, historyNote } = storyJourney(node);
  return (
    <div className="mt-2" data-testid={`story-journey-${node.node_ref}`}>
      <p className="text-sm font-medium text-blue-700 dark:text-blue-300" data-testid={`node-stage-${node.node_ref}`}>
        {headline}
      </p>
      <ol aria-label="Story journey" className="mt-3 space-y-2 border-l border-gray-200 pl-3 dark:border-gray-700">
        {steps.map(step => (
          <li key={step.id} aria-current={step.progress === 'current' || (step.id === 'merged' && node.state === 'passed') ? 'step' : undefined}
            data-stage={step.id} data-progress={step.progress} className="relative text-xs">
            <span aria-hidden="true" className={`absolute -left-[1.15rem] top-0 rounded-full bg-white dark:bg-gray-900 ${step.progress === 'complete' ? 'text-green-700 dark:text-green-400' : step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-500'}`}>
              {step.progress === 'complete' ? '✓' : step.progress === 'current' ? '▶' : step.progress === 'observed' ? '•' : '○'}
            </span>
            <span className={`font-medium ${step.progress === 'current' ? 'text-blue-700 dark:text-blue-300' : 'text-gray-800 dark:text-gray-200'}`}>{step.label}</span>
            <span className="block text-gray-600 dark:text-gray-400">{step.detail}</span>
            {node.run_id && step.run && step.id !== 'development' && (
              <Link to={`/activity?chain=${encodeURIComponent(node.run_id)}&highlight=${encodeURIComponent(step.run.invocation_id)}`}
                className="text-blue-600 underline dark:text-blue-400">
                {step.id === 'review' ? 'View review run' : 'View fixes run'}
              </Link>
            )}
          </li>
        ))}
      </ol>
      {historyNote && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{historyNote}</p>}
    </div>
  );
}
