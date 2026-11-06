import { createContext, useContext } from 'react';

export interface ActivityScope { productId: string; projectId: string; readerKey?: string }
export const ActivityScopeContext = createContext<ActivityScope | undefined>(undefined);
export const useActivityScope = () => useContext(ActivityScopeContext);
export const activityBase = (scope?: ActivityScope) => scope
  ? `/v1/products/${encodeURIComponent(scope.productId)}/projects/${encodeURIComponent(scope.projectId)}/agent-invocations`
  : '/me/agent-invocations';
