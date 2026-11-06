import { productPath } from '@/services/products';
import type { L5Service } from './L5ServiceJourney';

// Discovery is independent of profile completeness and service execution readiness.
// Each service loads its own scoped configuration and capability availability.
export const productServices: ReadonlyArray<{ id: L5Service; name: string; description: string }> = [
  { id: 'security', name: 'Security', description: 'Threat modeling and code scanning.' },
  { id: 'devops', name: 'DevOps', description: 'Investigate environments and operational issues.' },
  { id: 'finops', name: 'FinOps', description: 'Explore costs and billing questions.' },
  { id: 'transform', name: 'Transform', description: 'Refactor code and upgrade dependencies.' },
  { id: 'well_architected', name: 'Well-Architected', description: 'Review architecture against selected pillars.' },
];

export const productServicePath = (productId: string, projectId: string, service?: L5Service) =>
  `${productPath(productId, projectId)}/services${service ? `/${service}` : ''}`;

export const productServiceFromPath = (pathname: string): L5Service | undefined => {
  const id = pathname.replace(/\/+$/, '').split('/services/')[1];
  return productServices.find(service => service.id === id)?.id;
};
