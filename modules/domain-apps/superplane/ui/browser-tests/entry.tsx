import { createRoot } from 'react-dom/client';
import { ServingPanel } from '@superplane-ui/ServingPanel';
import { browserReceiptStore } from '@superplane-ui/operations';
import '@/index.css';

// Fixture network responses are provided by the isolated browser runner.
window.sessionStorage.setItem('cognito_access_token', 'browser-fixture');
createRoot(document.getElementById('root')!).render(
  <main className="mx-auto max-w-5xl p-4">
    <ServingPanel workspaceId="11111111-1111-4111-8111-111111111111"
      scope={{ deploymentId: window.location.origin, orgId: 'fixture-org' }}
      store={browserReceiptStore(window.localStorage)} />
  </main>,
);
