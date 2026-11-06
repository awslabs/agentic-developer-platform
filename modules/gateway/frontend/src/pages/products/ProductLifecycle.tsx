import { Link } from 'react-router-dom';
import { serviceCapabilities, type L5Service } from './services/L5ServiceJourney';
import { productServices } from './services/productServiceNavigation';
import { agentPath, workspacePath } from './productWorkspaceNavigation';

type Props = { productId: string; projectId: string };
export const agentNames: Record<L5Service, string> = {
  security: 'AWS Security Agent', devops: 'AWS DevOps Agent', finops: 'AWS FinOps Agent',
  transform: 'AWS Transform', well_architected: 'AWS Well-Architected Agent',
};

function CapabilityRow({ productId, projectId, service, capability, workspace, verb }: Props & {
  service: L5Service; capability: string; workspace: 'release' | 'operate'; verb?: string;
}) {
  const item = serviceCapabilities[service].find(entry => entry.id === capability)!;
  return <li className="product-activity-row">
    <div><h3>{verb ? `${verb} · ` : ''}{item.title}</h3><p className="product-muted">{item.description}</p>
      <p className="product-muted">{agentNames[service]}</p>
      <details><summary>Requirements</summary><p>{item.requirements}</p></details></div>
    {item.unavailable ? <span className="product-availability">Not available yet in ADP</span>
      : <Link className="product-action-link" to={`${agentPath(productId, projectId, service, workspace)}?capability=${capability}#l5-${service}-${service === 'transform' ? 'setup' : 'runs'}`}>Open {item.title.toLowerCase()}</Link>}
  </li>;
}

export function ProductLifecycle({ workspace, ...props }: Props & { workspace: 'release' | 'operate' }) {
  return <section className="blueprint-card product-card product-lifecycle" aria-label={workspace === 'release' ? 'Release workspace' : 'Operate workspace'}>
    <header><h2>{workspace === 'release' ? 'Release' : 'Operate'}</h2><p className="product-muted">{workspace === 'release'
      ? 'Prepare a change, deploy it to an environment and verify the result.'
      : 'Monitor, detect, triage and fix issues in your running product.'}</p></header>
    {workspace === 'release' ? <>
      <section><h3>Prepare</h3><p className="product-muted">Checks use the product’s saved sources. Review the exact revision before starting work.</p>
        <ul><CapabilityRow {...props} workspace={workspace} service="security" capability="threat_model" />
          <CapabilityRow {...props} workspace={workspace} service="security" capability="code_review" />
          <CapabilityRow {...props} workspace={workspace} service="well_architected" capability="architecture_review" />
          <CapabilityRow {...props} workspace={workspace} service="devops" capability="release_readiness" />
          <li className="product-activity-row"><div><h3>Regression testing</h3><p className="product-muted">Requires a linked CI pipeline, test suite and revision-specific results.</p></div><span className="product-availability">Release trigger not available yet in ADP</span></li>
          <CapabilityRow {...props} workspace={workspace} service="security" capability="pentesting" /></ul>
      </section>
      <section><h3>Deploy</h3><p>Use your deployment pipeline with an explicit environment and release approval.</p>
        <p className="product-muted">Deployment from this workspace is not available yet. Saved pipelines and environments are in Product profile.</p>
        <Link className="product-action-link" to={`${workspacePath(props.productId, props.projectId, 'profile')}#product-connections`}>Review environments &amp; delivery</Link></section>
      <section><h3>Verify</h3><ul><CapabilityRow {...props} workspace={workspace} service="devops" capability="release_verification" /></ul></section>
    </> : <>
      <section><h3>Monitor &amp; detect</h3><p>AWS DevOps Agent uses connected telemetry and alerts in its native workspace. ADP does not yet show a consolidated environment health feed here.</p>
        <Link className="product-action-link" to={agentPath(props.productId, props.projectId, 'devops', 'operate')}>Review DevOps connection &amp; access</Link>
      </section>
      <section><h3>Triage &amp; fix</h3><p className="product-muted">Select an environment in the investigation. Inspect evidence and available actions through ADP sign-in to the DevOps Agent app.</p>
        <ul><CapabilityRow {...props} workspace={workspace} service="devops" capability="investigation" />
          <CapabilityRow {...props} workspace={workspace} service="transform" capability="refactor" />
          <CapabilityRow {...props} workspace={workspace} service="transform" capability="dependency_upgrade" /></ul>
        <Link className="product-action-link" to={workspacePath(props.productId, props.projectId, 'build')}>Track code fixes in Build</Link>
      </section>
      <section><h3>Improve</h3><ul><CapabilityRow {...props} workspace={workspace} service="finops" capability="cost_inquiry" />
        <CapabilityRow {...props} workspace={workspace} service="well_architected" capability="architecture_review" />
        <CapabilityRow {...props} workspace={workspace} service="devops" capability="prevention" /></ul></section>
    </>}
    <details><summary>Setup, schedules and retained results</summary><p>Each activity uses its agent’s saved setup. Review what came from the Product profile, add required inputs, then start or schedule supported work. Results stay with the agent. Opening an activity does not start a run.</p>
      <Link className="product-action-link" to={workspacePath(props.productId, props.projectId, 'agents')}>Open all agents</Link></details>
  </section>;
}

export function ProductAgents(props: Props) {
  return <section className="blueprint-card product-card product-lifecycle" aria-label="Product agents"><header><h2>Agents</h2><p className="product-muted">Shared setup, capabilities, runs and retained results for all five AWS agents.</p></header>
    <ul>{productServices.map(service => <li className="product-activity-row" key={service.id}><div><h3>{agentNames[service.id]}</h3><p className="product-muted">{service.description}</p>
      <details><summary>Capabilities</summary><ul>{serviceCapabilities[service.id].map(capability => <li key={capability.id}>{capability.title}{capability.unavailable && ' · Not available yet in ADP'}</li>)}</ul></details></div>
      <Link className="product-action-link" to={agentPath(props.productId, props.projectId, service.id)}>Open {service.name}</Link></li>)}</ul>
  </section>;
}
