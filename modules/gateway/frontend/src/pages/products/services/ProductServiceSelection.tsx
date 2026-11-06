import { useId } from 'react';
import { Link } from 'react-router-dom';
import { productServicePath, productServices } from './productServiceNavigation';
import './productServiceSelection.css';

export default function ProductServiceSelection({ productId, projectId }: { productId: string; projectId: string }) {
  const heading = useId();
  return <section aria-labelledby={heading} className="blueprint-card product-card product-service-selection">
    <h2 id={heading}>Services</h2>
    <p className="product-muted">Choose a service for this delivery scope. Review its setup, available capabilities, runs and results.</p>
    <nav aria-label="Choose a Product service" className="product-service-cards">
      {productServices.map(service => <Link key={service.id} to={productServicePath(productId, projectId, service.id)}
        aria-label={`Open ${service.name}`} aria-describedby={`${heading}-${service.id}`}>
        <h3>{service.name}</h3>
        <p id={`${heading}-${service.id}`}>{service.description}</p>
        <span aria-hidden="true">Open service →</span>
      </Link>)}
    </nav>
    <p className="product-muted">Opening a service does not start a run. Setup and access requirements are shown inside each service.</p>
  </section>;
}
