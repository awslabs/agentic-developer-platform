/**
 * Plan summary with an authorized execution policy — issue #5128.
 *
 * The load-bearing assertions here are about what the summary must NOT say, because
 * every failure mode of a permission summary is a misreading rather than a crash:
 *
 * * **Absence renders nothing.** A flow with no policy must produce no policy block
 *   at all — not an empty one, and not zeroed limits. Every flow accepted before
 *   policies existed has none permanently and runs with legacy semantics, so a
 *   summary reading "no autonomous actions, $0.00" describes the opposite of the
 *   truth.
 * * **A gated action is never shown as autonomous.** An action can be both permitted
 *   and gated in the accepted document — that is how an owner says "agents may
 *   prepare this, a person releases it". The server resolves the overlap; a client
 *   that re-derived it would show `merge` as unattended on exactly the policy that
 *   gated it.
 * * **Machine acceptance never reads as gate authority.** The two are different
 *   powers, and conflating them is believing a human approval can be satisfied by a
 *   machine.
 * * **Coordination never reads as authority over the work it may ask for.** An
 *   accepted coordinator may request an eligible child; each request is admitted on
 *   that child's own authorized action. A summary that showed the scope as things the
 *   coordinator may do would overstate what the owner accepted by exactly the amount
 *   that matters.
 * * **The spend figure is rendered verbatim.** It is a `Decimal` server-side; a test
 *   that accepted a reformatted number would let float rounding into the one number
 *   an owner authorized.
 *
 * The existing story/wave/gate counts are asserted too, since this component already
 * shipped and those must keep working.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PlanSummary } from '@/components/orchestration/PlanSummary';
import type { PolicySummary } from '@/types/orchestration';

function makePolicy(overrides: Partial<PolicySummary> = {}): PolicySummary {
  return {
    repository_ids: ['aws-e/adp'],
    environment_connection_ids: [],
    team_ids: ['team-platform'],
    autonomous_actions: ['develop', 'review'],
    human_decisions: [],
    machine_accepted_evaluations: 0,
    expires_at: '2026-12-31T00:00:00Z',
    limits: {
      max_wall_clock_seconds: 3600,
      max_spend_usd: '50.00',
      max_attempts_per_node: 3,
      max_concurrent_actions: 4,
    },
    ...overrides,
  };
}

const COUNTS = { stories: 4, waves: 2, gates: 1, evaluations: 3, evaluationStories: 2 };

// ---------------------------------------------------------------------------

describe('the plan counts', () => {
  it('reports stories, waves, gates and evaluations', () => {
    render(<PlanSummary {...COUNTS} />);

    expect(screen.getByTestId('plan-summary')).toHaveTextContent('6 stories across 2 waves');
    expect(screen.getByTestId('plan-summary')).toHaveTextContent('4 implementation · 2 evaluation · 1 approval gate · 1 evaluation checkpoint');
  });

  it('singularizes a one-story, one-wave plan', () => {
    render(<PlanSummary stories={1} waves={1} gates={1} evaluations={1} evaluationStories={0} />);

    expect(screen.getByTestId('plan-summary')).toHaveTextContent('1 story across 1 wave');
  });
});

describe('an unpolicied flow shows no policy at all', () => {
  it('renders no policy block when the prop is absent', () => {
    render(<PlanSummary {...COUNTS} />);

    expect(screen.queryByTestId('plan-summary-policy')).not.toBeInTheDocument();
  });

  it('renders no policy block when the server sent null', () => {
    // Distinct from `undefined` on purpose: the API sends explicit `null` for a
    // flow with no policy, and an older release omits the key. Both must be silent.
    render(<PlanSummary {...COUNTS} policy={null} />);

    expect(screen.queryByTestId('plan-summary-policy')).not.toBeInTheDocument();
    expect(screen.queryByTestId('policy-limits')).not.toBeInTheDocument();
  });

  it('never renders a zero spend figure in place of a missing policy', () => {
    render(<PlanSummary {...COUNTS} policy={null} />);

    expect(screen.getByTestId('plan-summary')).not.toHaveTextContent('$0');
  });
});

describe('what the owner authorized', () => {
  it('names the repositories the authority applies to', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ repository_ids: ['aws-e/adp', 'aws-e/tools'] })} />);

    expect(screen.getByTestId('policy-targets')).toHaveTextContent('aws-e/adp, aws-e/tools');
  });

  it('says so explicitly when no environment may be deployed to', () => {
    // "No deployment targets" is a meaningful authorization. Omitting the line
    // would read as information missing rather than as a boundary.
    render(<PlanSummary {...COUNTS} policy={makePolicy({ environment_connection_ids: [] })} />);

    expect(screen.getByTestId('policy-targets')).toHaveTextContent('no deployment targets');
  });

  it('names the deployment targets when there are some', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ environment_connection_ids: ['conn-dev'] })} />);

    expect(screen.getByTestId('policy-targets')).toHaveTextContent('deploys to conn-dev');
  });

  it('states the expiry, because a grant without one is what this is defined against', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy()} />);

    expect(screen.getByTestId('plan-summary-policy')).toHaveTextContent(/You authorized this delivery until/);
  });

  it('falls back to the raw expiry rather than rendering "Invalid Date"', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ expires_at: 'not-a-date' })} />);

    expect(screen.getByTestId('plan-summary-policy')).toHaveTextContent('not-a-date');
    expect(screen.getByTestId('plan-summary-policy')).not.toHaveTextContent('Invalid Date');
  });
});

describe('autonomous actions versus human decisions', () => {
  it('lists what agents may do without asking, in operator language', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ autonomous_actions: ['develop', 'review', 'repair'] })} />);

    expect(screen.getByTestId('policy-autonomous')).toHaveTextContent('write code, review, fix failures');
  });

  it('does NOT list a gated action as autonomous', () => {
    // The central assertion. The server already split these; a component that
    // re-derived them from `allowed_actions` would show `merge` here.
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({ autonomous_actions: ['develop', 'review'], human_decisions: ['merge', 'deploy'] })}
      />
    );

    expect(screen.getByTestId('policy-autonomous')).not.toHaveTextContent('merge');
    expect(screen.getByTestId('policy-autonomous')).not.toHaveTextContent('deploy');
    expect(screen.getByTestId('policy-human-decisions')).toHaveTextContent('merge, deploy');
  });

  it('says plainly when a policy authorizes nothing unattended', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ autonomous_actions: [], human_decisions: ['merge'] })} />);

    expect(screen.getByTestId('policy-autonomous')).toHaveTextContent('every action needs a person');
  });

  it('omits the human-decision line when the policy gates nothing', () => {
    // An empty "waits for you: none" reads as reassurance that a control exists on
    // the policy where none does.
    render(<PlanSummary {...COUNTS} policy={makePolicy({ human_decisions: [] })} />);

    expect(screen.queryByTestId('policy-human-decisions')).not.toBeInTheDocument();
  });
});

describe('machine acceptance is a mode, not gate authority', () => {
  it('reports the count and reasserts that approval gates need a person', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ machine_accepted_evaluations: 2 })} />);

    const block = screen.getByTestId('policy-machine-acceptance');
    expect(block).toHaveTextContent('2 evaluations may be concluded automatically');
    expect(block).toHaveTextContent('Approval gates still require you');
  });

  it('says nothing about machine acceptance when no evaluation is marked for it', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy({ machine_accepted_evaluations: 0 })} />);

    expect(screen.queryByTestId('policy-machine-acceptance')).not.toBeInTheDocument();
  });
});

describe('the limits', () => {
  it('renders the authorized spend exactly as the server sent it', () => {
    // Verbatim, not reformatted: the figure is a Decimal server-side and passing it
    // through a JS float would put rounding into the number an owner authorized.
    render(<PlanSummary {...COUNTS} policy={makePolicy({ limits: { ...makePolicy().limits, max_spend_usd: '1234.56' } })} />);

    expect(screen.getByTestId('policy-limits')).toHaveTextContent('$1234.56');
  });

  it('reports the concurrency and attempt bounds', () => {
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({ limits: { ...makePolicy().limits, max_concurrent_actions: 4, max_attempts_per_node: 3 } })}
      />
    );

    expect(screen.getByTestId('policy-limits')).toHaveTextContent('4 at a time');
    expect(screen.getByTestId('policy-limits')).toHaveTextContent('3 attempts per stage');
  });

  it('never renders an "unlimited" reading, because the schema cannot express one', () => {
    render(<PlanSummary {...COUNTS} policy={makePolicy()} />);

    const block = screen.getByTestId('plan-summary-policy');
    expect(block).not.toHaveTextContent(/unlimited/i);
    expect(block).not.toHaveTextContent(/no limit/i);
  });
});

describe('the accepted coordination scope', () => {
  it('shows the child personas and actions a coordinator may ask for, and the assigned step count', () => {
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({
          autonomous_actions: ['develop', 'review', 'coordinate'],
          coordination: { assigned_node_count: 3, allowed_child_personas: ['developer', 'reviewer'], allowed_child_actions: ['develop', 'review'] },
        })}
      />
    );

    const block = screen.getByTestId('policy-coordination');
    expect(block).toHaveTextContent('3 assigned steps');
    expect(block).toHaveTextContent('developers, reviewers');
    expect(block).toHaveTextContent('write code, review');
  });

  it('renders nothing when the policy accepted no coordinator', () => {
    // Absence, not a zeroed block: "0 assigned steps, no one" describes an
    // accepted-but-useless coordinator, which is a different fact from "no
    // coordinator was accepted" and the more alarming of the two to show an owner
    // who accepted neither.
    render(<PlanSummary {...COUNTS} policy={makePolicy()} />);

    expect(screen.queryByTestId('policy-coordination')).toBeNull();
  });

  it('says coordination cannot approve a gate, merge, deploy or conclude an evaluation', () => {
    // The misreading this whole section exists to prevent. An owner who believes
    // "may ask for further work" reaches an approval, a merge, a deploy or an
    // evaluation conclusion has accepted something materially larger than what the
    // server will actually admit.
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({
          coordination: { assigned_node_count: 1, allowed_child_personas: ['developer'], allowed_child_actions: ['develop'] },
        })}
      />
    );

    const block = screen.getByTestId('policy-coordination');
    expect(block).toHaveTextContent('checked again on its own terms');
    expect(block).toHaveTextContent('cannot approve a gate, merge, deploy or conclude an evaluation');
  });

  it('describes `coordinate` as asking for work rather than performing it', () => {
    // `coordinate` in `autonomous_actions` means the coordinator may make requests
    // unattended — not that the work it requests is unattended. A label reading like
    // the other verbs would collapse that distinction on the one action that performs
    // nothing at all.
    render(<PlanSummary {...COUNTS} policy={makePolicy({ autonomous_actions: ['coordinate'] })} />);

    const autonomous = screen.getByTestId('policy-autonomous');
    expect(autonomous).toHaveTextContent('ask for further work');
    expect(autonomous.textContent).not.toMatch(/\bcoordinate\b/);
  });

  it('still gates an action the policy gated, even when that action is coordination-adjacent', () => {
    // A coordinator's scope can never name `merge`; the server refuses such a scope at
    // acceptance and refuses the request again at admission. So an accepted coordinator
    // alongside a gated merge must read as: asks for development, merge still waits.
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({
          autonomous_actions: ['develop', 'coordinate'],
          human_decisions: ['merge'],
          coordination: { assigned_node_count: 2, allowed_child_personas: ['developer'], allowed_child_actions: ['develop'] },
        })}
      />
    );

    expect(screen.getByTestId('policy-human-decisions')).toHaveTextContent('merge');
    expect(screen.getByTestId('policy-coordination').textContent).not.toMatch(/\bmerge\b(?!,? deploy)/);
  });
});

describe('what the summary must not leak', () => {
  it('renders no internal graph address', () => {
    // §7.2: `flow/epic/wave/node` is the internal cost join key and is never shown.
    // The summary carries a machine-acceptance COUNT rather than the address map for
    // exactly this reason, so there is no address available to render by mistake.
    render(<PlanSummary {...COUNTS} policy={makePolicy({ machine_accepted_evaluations: 1 })} />);

    expect(screen.getByTestId('plan-summary-policy').textContent).not.toMatch(/\w+\/\w+\/\w+\/\w+/);
  });

  it('renders no assigned coordinator address, only their count', () => {
    // The coordination scope is the summary's second address-shaped field, and the
    // reason it is a count server-side. Asserted separately from the machine-acceptance
    // case so removing either projection fails a test that names it.
    render(
      <PlanSummary
        {...COUNTS}
        policy={makePolicy({
          autonomous_actions: ['develop', 'coordinate'],
          coordination: { assigned_node_count: 4, allowed_child_personas: ['developer'], allowed_child_actions: ['develop'] },
        })}
      />
    );

    expect(screen.getByTestId('policy-coordination').textContent).not.toMatch(/\w+\/\w+\/\w+\/\w+/);
    expect(screen.getByTestId('policy-coordination')).toHaveTextContent('4 assigned steps');
  });
});


it('explains user credential permissions and provider lifetime without claiming narrower action enforcement', () => {
  render(<PlanSummary {...COUNTS} policy={makePolicy({
    human_decisions: ['deploy'],
    user_credentials: {
      permission_mode: 'user_configured', lifetime: 'provider_managed',
      vault_credential_count: 1,
      aws_role_count: 1, actions: ['develop'],
    },
  })} />);
  const description = screen.getByTestId('policy-user-credentials');
  expect(description).toHaveTextContent('retain their configured permissions');
  expect(description).toHaveTextContent('Vault credentials: 1');
  expect(description).toHaveTextContent('AWS roles: 1');
  expect(description).toHaveTextContent('Credentials already issued follow');
  expect(description).toHaveTextContent('may permit additional actions');
  expect(screen.getByTestId('policy-human-decisions')).toHaveTextContent('deploy');
});
