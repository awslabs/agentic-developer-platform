import { useAuth } from '@/hooks/useAuth';
import { useLocation, useNavigate } from 'react-router-dom';
import AgentActivity from '@/pages/AgentActivity';
import ProductDeliveryFlows from './ProductDeliveryFlows';
import { workspacePath } from './productWorkspaceNavigation';

export default function ProductBuild(props: { productId: string; projectId: string }) {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const activity = location.pathname.endsWith('/activity');
  const base = workspacePath(props.productId, props.projectId, 'build');
  const paths = [base, `${base}/activity`];
  return <div><nav className="product-build-tabs" aria-label="Build views" role="tablist" onKeyDown={event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : activity ? 0 : 1;
    navigate(paths[next]);
    (event.currentTarget.querySelectorAll('button')[next] as HTMLButtonElement).focus();
  }}>
    {['Delivery flows', 'Agent activity'].map((label, index) => <button type="button" key={label} id={`build-tab-${index}`} role="tab" aria-selected={activity === (index === 1)} aria-controls="product-build-panel" tabIndex={activity === (index === 1) ? 0 : -1} onClick={() => navigate(paths[index])}>{label}</button>)}
  </nav><div id="product-build-panel" role="tabpanel" aria-labelledby={`build-tab-${activity ? 1 : 0}`}>
    {activity ? <AgentActivity readerKey={`${user?.orgId}:${user?.id}`} productId={props.productId} projectId={props.projectId} /> : <ProductDeliveryFlows {...props} />}
  </div></div>;
}
