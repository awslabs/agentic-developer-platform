import { preloadedPage } from './preloadedPage';

// Startup prepares code only. Onboarding and feature gates still own mounting
// the pages and therefore starting their authenticated data reads.
export const { load: loadFlowsList, Page: FlowsListPage } = preloadedPage(() => import('../pages/FlowsList'));
export const { load: loadFlowGraph, Page: FlowGraphPage } = preloadedPage(() => import('../pages/GraphView'));
