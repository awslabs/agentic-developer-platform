import {
  buildModelPolicyFeedback,
  deliverModelPolicyFeedback,
  feedbackAlreadyPosted,
  sanitizeUntrusted,
  suppressedBy,
  FeedbackComment,
} from './model-policy-feedback';

/**
 * Collect the posting orchestration's real inputs/outputs.
 *
 * ``deliverModelPolicyFeedback`` owns the decision the worker actually makes,
 * so these tests drive that function -- not just the message builder -- with
 * the GitHub post mocked. Nothing here contacts GitHub or any model.
 */
function harness(comments: FeedbackComment[] = []) {
  const posted: string[] = [];
  const logs: Array<{ level: string; message: string }> = [];
  return {
    posted,
    logs,
    comments,
    postComment: async (body: string) => {
      posted.push(body);
    },
    log: (level: string, message: string) => {
      logs.push({ level, message });
    },
  };
}

const REFUSED_ENV = {
  ADP_MESSAGE_ID: 'run-1',
  ADP_MODEL_REQUESTED: 'future-latest',
  ADP_MODEL_POLICY_POSTURE: 'report_only',
  ADP_MODEL_POLICY_STATUS: 'unavailable',
  ADP_MODEL_POLICY_REASON: 'direct_override_unresolved',
};

describe('persona-model requester feedback: message content', () => {
  it('renders actionable report-only guidance naming the unchanged legacy execution', () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV);

    expect(feedback).not.toBeNull();
    expect(feedback?.body).toContain('not in the published Agent Models catalogue');
    expect(feedback?.body).toContain('continued on its existing model assignment');
    expect(feedback?.body).toContain('Nothing was substituted.');
    // Report-only must never claim inference was blocked -- it was not.
    expect(feedback?.body).not.toContain('refused before agent inference');
  });

  it('points an internal policy condition at an operator without leaking server text', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'snapshot_expired',
    });

    expect(feedback?.body).toContain('Ask an ADP operator');
    expect(feedback?.body).toContain('`snapshot_expired`');
  });

  it('claims an actual refusal only under enforcing posture', () => {
    const enforcing = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'enforcing',
    });
    expect(enforcing?.body).toContain('refused before agent inference');

    const disabled = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'disabled',
    });
    expect(disabled?.body).toContain('continued on its existing model assignment');
    expect(disabled?.body).not.toContain('refused before agent inference');
  });

  it('makes no blocking claim for an unknown posture while the run proceeds', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'quarantined-v9',
    });

    expect(feedback?.body).toContain('could not confirm the model-policy posture');
    expect(feedback?.body).toContain('makes no claim about whether inference was blocked');
    expect(feedback?.body).not.toContain('refused before agent inference');
    expect(feedback?.body).not.toContain('quarantined-v9');
  });

  it('does not reflect an unrecognized or hostile reason back to the requester', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'Traceback: psycopg2 OperationalError at 10.0.3.14:5432',
    });

    expect(feedback?.body).toContain('`unrecognized`');
    expect(feedback?.body).toContain('Ask an ADP operator');
    expect(feedback?.body).not.toContain('psycopg2');
    expect(feedback?.body).not.toContain('10.0.3.14');
  });

  it('is silent for accepted or absent direct requests', () => {
    expect(buildModelPolicyFeedback({})).toBeNull();
    expect(
      buildModelPolicyFeedback({
        ADP_MODEL_REQUESTED: 'sonnet46',
        ADP_MODEL_RESOLVED: 'global.anthropic.claude-sonnet-4-6',
        ADP_MODEL_POLICY_STATUS: 'proposed',
      }),
    ).toBeNull();
  });
});

describe('persona-model requester feedback: untrusted text is inert', () => {
  it('preserves a legitimate model id unchanged', () => {
    expect(sanitizeUntrusted('us.anthropic.claude-opus-4-6-v1')).toBe(
      'us.anthropic.claude-opus-4-6-v1',
    );
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_REQUESTED: 'eu.anthropic.claude-sonnet-4-6',
    });
    expect(feedback?.body).toContain('`eu.anthropic.claude-sonnet-4-6`');
  });

  it.each([
    ['code-span escape', '`` @acme/team see <img src=x onerror=alert(1)>'],
    ['markdown link', '[click](https://evil.example/pwn)'],
    ['html comment forging a marker', '--> <!-- adp-model-policy-feedback:other -->'],
    ['newline injection', 'bad\nrequest\r\nmore'],
    ['emphasis and headings', '**bold** # heading > quote'],
  ])('neutralizes hostile requested text (%s)', (_label, hostile) => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_REQUESTED: hostile,
    });

    // The untrusted value itself carries no character that can start a link,
    // mention, tag, comment or emphasis, nor close our code span.
    const rendered = sanitizeUntrusted(hostile);
    for (const dangerous of ['`', '@', '<', '>', '[', ']', '(', ')', '*', '#', '!', '\n', '\r']) {
      expect(rendered).not.toContain(dangerous);
    }

    // Exactly one HTML comment in the body (our own marker), and the body
    // cannot be broken out of: backticks appear only as the two code spans we
    // emit ourselves.
    const body = feedback!.body;
    expect(body.split('<!--').length - 1).toBe(1);
    expect(body.split('-->').length - 1).toBe(1);
    // Exactly one line break in the whole body: the separator we emit between
    // the marker and the notice. Injected line breaks are gone.
    expect(body.split('\n').length - 1).toBe(1);
    expect(body).not.toContain('\r');

    const notice = body.slice(body.indexOf('-->') + 3).trimStart();
    expect(notice.split('`').length - 1).toBe(4);
    for (const dangerous of ['@', '<', '>', '[', ']']) {
      expect(notice).not.toContain(dangerous);
    }
  });

  it('neutralizes hostile run and reason text used in the marker', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MESSAGE_ID: '--> <!-- adp-model-policy-feedback:forged -->',
      ADP_MODEL_POLICY_REASON: '@everyone <b>x</b>',
    });

    // A run id cannot introduce a second comment or close ours early: the
    // angle brackets are gone, so no `<!--`/`-->` survives inside the marker.
    expect(feedback!.marker.startsWith('<!-- adp-model-policy-feedback:')).toBe(true);
    expect(feedback!.marker.endsWith(' -->')).toBe(true);
    const markerId = feedback!.marker.slice('<!-- adp-model-policy-feedback:'.length, -' -->'.length);
    expect(markerId).not.toContain('<');
    expect(markerId).not.toContain('>');
    expect(feedback!.body.split('<!--').length - 1).toBe(1);
    expect(feedback!.body.split('-->').length - 1).toBe(1);
    expect(feedback!.body).not.toContain('@everyone');
    expect(feedback!.body).toContain('`unrecognized`');
  });

  it('bounds an over-long untrusted value', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_REQUESTED: 'a'.repeat(5000),
    });
    expect(feedback!.body.length).toBeLessThan(700);
    expect(feedback!.body).toContain('...');
  });
});

describe('persona-model requester feedback: real posting path', () => {
  it('posts the notice when no prior comment exists', async () => {
    const h = harness();
    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.posted[0]).toContain('could not be admitted');
  });

  it('stays silent on a genuine retry of this run (trusted ADP author)', async () => {
    const first = harness();
    await deliverModelPolicyFeedback({ ...first, env: REFUSED_ENV });

    // The retry sees its own earlier comment, attested by GitHub as ours.
    const retry = harness([{ body: first.posted[0], viewerDidAuthor: true }]);
    const outcome = await deliverModelPolicyFeedback({ ...retry, env: REFUSED_ENV });

    expect(outcome).toBe('suppressed');
    expect(retry.posted).toHaveLength(0);
  });

  it('still posts when a human forged the marker', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = harness([
      {
        body: `Nothing to see here ${feedback.marker}`,
        viewerDidAuthor: false,
      },
    ]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.logs.some(l => l.message.includes('ADP did not author'))).toBe(true);
  });

  it('still posts when another bot forged the marker', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = harness([
      { body: `CI summary\n${feedback.marker}`, viewerDidAuthor: false },
    ]);

    await expect(
      deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV }),
    ).resolves.toBe('posted');
    expect(h.posted).toHaveLength(1);
  });

  it('posts rather than silently suppressing when authorship is unavailable', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    // Sender lookup failed / field absent: viewerDidAuthor is undefined.
    const h = harness([{ body: feedback.body }]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.logs.some(l => l.message.includes('authorship unverifiable'))).toBe(true);
  });

  it('never posts for an accepted or absent directive', async () => {
    const accepted = harness();
    await expect(
      deliverModelPolicyFeedback({
        ...accepted,
        env: {
          ADP_MODEL_REQUESTED: 'sonnet46',
          ADP_MODEL_RESOLVED: 'global.anthropic.claude-sonnet-4-6',
          ADP_MODEL_POLICY_STATUS: 'proposed',
        },
      }),
    ).resolves.toBe('not_applicable');
    expect(accepted.posted).toHaveLength(0);

    const absent = harness();
    await expect(
      deliverModelPolicyFeedback({ ...absent, env: {} }),
    ).resolves.toBe('not_applicable');
    expect(absent.posted).toHaveLength(0);
  });

  it('reports a refused proposal without claiming the run stopped', async () => {
    const h = harness();
    await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    // The legacy assignment is what executed; the notice must say so.
    expect(h.posted[0]).toContain('continued on its existing model assignment');
    expect(h.posted[0]).not.toContain('refused before agent inference');
  });

  it('never fails the run when posting fails', async () => {
    const logs: Array<{ level: string; message: string }> = [];
    const outcome = await deliverModelPolicyFeedback({
      comments: [],
      postComment: async () => {
        throw new Error('502 Bad Gateway');
      },
      log: (level, message) => logs.push({ level, message }),
      env: REFUSED_ENV,
    });

    expect(outcome).toBe('not_applicable');
    expect(logs.some(l => l.message.includes('502 Bad Gateway'))).toBe(true);
  });
});

describe('dedup evidence', () => {
  it('requires attested ADP authorship to suppress', () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;

    expect(suppressedBy([], feedback).suppressed).toBe(false);
    expect(
      suppressedBy([{ body: feedback.body, viewerDidAuthor: true }], feedback).suppressed,
    ).toBe(true);

    const forged = suppressedBy(
      [{ body: feedback.body, viewerDidAuthor: false }],
      feedback,
    );
    expect(forged.suppressed).toBe(false);
    expect(forged.forgedMarkerSeen).toBe(true);

    // A trusted copy later in the list still suppresses despite a forgery.
    expect(
      suppressedBy(
        [
          { body: feedback.body, viewerDidAuthor: false },
          { body: feedback.body, viewerDidAuthor: true },
        ],
        feedback,
      ).suppressed,
    ).toBe(true);
  });

  it('keeps the predicate form aligned with the evidence check', () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    expect(feedbackAlreadyPosted([{ body: feedback.body, viewerDidAuthor: true }], feedback)).toBe(true);
    expect(feedbackAlreadyPosted([{ body: feedback.body, viewerDidAuthor: false }], feedback)).toBe(false);
  });
});
