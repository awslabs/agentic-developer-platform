import { FeatureGate } from '@/components/FeatureGate';
import { useSearchParams } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';
import { MonthlySpendView } from '@/components/budget/MonthlySpend';
import { BudgetHierarchy } from '@/components/budget/BudgetHierarchy';
import { BudgetManagement } from './BudgetManagement';

export default function BudgetSpend() {
  const [params, setParams] = useSearchParams();
  const { isPlatformAdmin, isOrgAdmin, canViewBudgets } = usePermissions();
  const platformAdmin = isPlatformAdmin();
  const canManage = platformAdmin || (isOrgAdmin() && canViewBudgets());
  const manage = canManage && params.get('view') === 'manage';
  const content = <div className="space-y-6 text-gray-900 dark:text-gray-100">
    <div><h1 className="text-2xl font-bold">Budget &amp; Spend</h1><p className="mt-1 text-sm text-gray-500 dark:text-gray-400">One monthly budget for your direct usage and cloud agents.</p></div>
    {canManage && <nav aria-label="Budget views" className="flex gap-6 border-b border-gray-200 dark:border-gray-700">{['My spend', 'Manage budgets'].map((label, i) => <button key={label} aria-current={manage === !!i ? 'page' : undefined} className={`pb-3 text-sm font-medium ${manage === !!i ? 'border-b-2 border-primary-500 text-primary-700 dark:text-primary-300' : 'text-gray-500'}`} onClick={() => setParams(i ? { view: 'manage' } : {})}>{label}</button>)}</nav>}
    {manage ? <div className="space-y-5">
      {platformAdmin ? <BudgetHierarchy /> : <p className="text-sm">Monthly organization, team and individual budgets are managed by a platform admin because they apply across workspaces.</p>}
      <details className="rounded-lg border border-gray-200 dark:border-gray-700 p-4"><summary className="cursor-pointer font-medium">Additional restrictions &amp; automation</summary><p className="my-4 text-sm text-gray-500 dark:text-gray-400">Existing workspace and shared controls still apply. Review and manage them here.</p><BudgetManagement additionalOnly /></details>
    </div> : <MonthlySpendView />}
  </div>;
  return canManage ? content : <FeatureGate feature="budget_spend">{content}</FeatureGate>;
}
