import { useCallback, useEffect, useRef, useState } from 'react';
import type { SecurityConfiguration as Configuration, Versioned } from '@/services/l5Services';
import SecurityConfiguration from './SecurityConfiguration';
import SecurityBindings from './SecurityBindings';
import SecurityRuns from './SecurityRuns';
import SecurityResults from './SecurityResults';
import { ServiceJourney, type L5Service } from './L5ServiceJourney';
import { productServices } from './productServiceNavigation';
import './productServiceSelection.css';
import DevOpsServiceTab from './DevOpsServiceTab';
import FinOpsServiceTab from './FinOpsServiceTab';
import TransformServiceTab from './TransformServiceTab';
import WellArchitectedServiceTab from './WellArchitectedServiceTab';

type Service = L5Service;
interface Props { productId: string; projectId: string; tenantId: string; activeService?: Service; onServiceChange?: (service: Service) => void; compact?: boolean }

export default function ProductServices({ productId, projectId, tenantId, activeService, onServiceChange, compact = false }: Props) {
  const [service, setService] = useState<Service>(activeService ?? 'security');
  useEffect(() => { if (activeService) setService(activeService); }, [activeService]);
  const chooseService = (value: Service) => { setService(value); onServiceChange?.(value); };

  const [configuration, setConfiguration] = useState<Versioned<Configuration> | null>(null);
  const [revoked, setRevoked] = useState(false);
  const [accessEpoch, setAccessEpoch] = useState(0);
  const accessDenied = useRef(false);
  const onAccessDenied = useCallback(() => { accessDenied.current = true; setRevoked(true); setConfiguration(null); }, []);
  const onRead = useCallback((value: Versioned<Configuration> | null) => {
    if (!accessDenied.current) setConfiguration(value);
  }, []);
  return <div className="space-y-5">
    {!compact && <nav aria-label="Product services" className="product-service-switcher">
      {productServices.map(item => <button key={item.id} type="button" aria-current={service === item.id ? 'page' : undefined}
        onClick={() => chooseService(item.id)}>{item.name}</button>)}
    </nav>}
    {!compact && <h2>{productServices.find(item => item.id === service)?.name}</h2>}
    {service === 'well_architected' ? <WellArchitectedServiceTab key={`${tenantId}:${productId}:${projectId}`} productId={productId} projectId={projectId} tenantId={tenantId} /> : service === 'transform' ? <TransformServiceTab key={`${tenantId}:${productId}:${projectId}`} productId={productId} projectId={projectId} tenantId={tenantId} /> : service === 'finops' ? <FinOpsServiceTab key={`${tenantId}:${productId}:${projectId}`} productId={productId} projectId={projectId} tenantId={tenantId} /> : service === 'devops' ? <DevOpsServiceTab productId={productId} projectId={projectId} tenantId={tenantId} /> : <>
    {revoked ? <div role="alert">Security access is unavailable. Previously shown details have been removed.
      <button type="button" onClick={() => { setConfiguration(null); setAccessEpoch(current => current + 1); accessDenied.current = false; setRevoked(false); }}>Refresh Security access</button>
    </div> : <div key={accessEpoch} className="space-y-5">
      <ServiceJourney service="security">
        <SecurityConfiguration productId={productId} projectId={projectId} tenantId={tenantId} onRead={onRead} onAccessDenied={onAccessDenied}
          bindingsPanel={<SecurityBindings productId={productId} projectId={projectId} configuration={configuration} onAccessDenied={onAccessDenied} />}
          assessmentsPanel={<SecurityRuns productId={productId} projectId={projectId} configuration={configuration} onAccessDenied={onAccessDenied} />}
          resultsPanel={<SecurityResults productId={productId} projectId={projectId} configuration={configuration} onAccessDenied={onAccessDenied} />} />
      </ServiceJourney>
    </div>}
    </>}
  </div>;
}
