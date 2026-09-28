import {
  COMMENT_PAGE_REQUEST_TIMEOUT_MS,
  createCommentPageFetcher,
  findSuppressingComment,
  buildModelPolicyFeedback,
  deliverModelPolicyFeedback,
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
  return pagedHarness([comments]);
}

/**
 * Drive the orchestration with a *paginated* comment history.
 *
 * Each element of ``pages`` is one page, so a test can put a genuine earlier
 * notice beyond the first page and prove the dedup lookup still finds it --
 * the case the worker's 20-comment LLM context window cannot cover.
 */
function pagedHarness(pages: FeedbackComment[][], failOnPage: number | null = null) {
  const posted: string[] = [];
  const logs: Array<{ level: string; message: string }> = [];
  const cursorsRequested: Array<string | null> = [];
  return {
    posted,
    logs,
    cursorsRequested,
    fetchCommentPage: async (cursor: string | null) => {
      cursorsRequested.push(cursor);
      const index = cursor === null ? 0 : Number(cursor);
      if (failOnPage !== null && index === failOnPage) {
        throw new Error('502 Bad Gateway from comment lookup');
      }
      const comments = pages[index] ?? [];
      const hasNextPage = index + 1 < pages.length;
      return { comments, endCursor: hasNextPage ? String(index + 1) : null, hasNextPage };
    },
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
    expect(feedback?.body).toContain('not listed in Agent Models');
    expect(feedback?.body).toContain('kept its existing model');
    expect(feedback?.body).toContain('The requested model change was not applied.');
    // Report-only must never claim inference was blocked -- it was not.
    expect(feedback?.body).not.toContain('refused before agent inference');
  });

  it('points an internal policy condition at an operator without leaking server text', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'snapshot_expired',
    });

    expect(feedback?.body).toContain('Contact your ADP administrator');
    expect(feedback?.body).not.toContain('`snapshot_expired`');
  });

  it('never claims inference was blocked, including under an enforcing posture', () => {
    // A posture is configuration, not evidence about control flow. This worker
    // posts feedback and then continues into the SDK regardless of the label,
    // so asserting a pre-inference refusal would be false about this very run.
    // The later runtime-posture stage establishes refusal from actual flow.
    const enforcing = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'enforcing',
    });
    expect(enforcing?.body).not.toContain('refused before agent inference');
    expect(enforcing?.body).toContain('Check the run status to see whether the agent started');

    const disabled = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'disabled',
    });
    expect(disabled?.body).toContain('kept its existing model');
    expect(disabled?.body).not.toContain('refused before agent inference');
  });

  it('makes no blocking claim for an unknown posture while the run proceeds', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_POSTURE: 'quarantined-v9',
    });

    expect(feedback?.body).toContain('could not confirm whether the agent started');
    expect(feedback?.body).not.toContain('posture');
    expect(feedback?.body).not.toContain('refused before agent inference');
    expect(feedback?.body).not.toContain('quarantined-v9');
  });

  it('does not reflect an unrecognized or hostile reason back to the requester', () => {
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'Traceback: psycopg2 OperationalError at 10.0.3.14:5432',
    });

    expect(feedback?.body).toContain('could not apply the requested model settings');
    expect(feedback?.body).toContain('Contact your ADP administrator');
    expect(feedback?.body).not.toContain('psycopg2');
    expect(feedback?.body).not.toContain('10.0.3.14');
  });

  it.each(['constructor', 'toString', '__proto__', 'valueOf', 'hasOwnProperty'])(
    'treats the inherited object property %s as an unrecognized reason',
    key => {
      // Plain-object indexing answers for inherited names: `constructor` and
      // `toString` returned native function source and `__proto__` an object,
      // rendering JS internals as the requester's guidance.
      const feedback = buildModelPolicyFeedback({
        ...REFUSED_ENV,
        ADP_MODEL_POLICY_REASON: key,
      });

      expect(feedback?.body).toContain('could not apply the requested model settings');
      expect(feedback?.body).toContain('Contact your ADP administrator');
      expect(feedback?.body).not.toContain('native code');
      expect(feedback?.body).not.toContain('[object Object]');
      expect(feedback?.body).not.toContain('function');
      expect(feedback?.body).not.toContain(key);
    },
  );

  it.each([
    'not_@permitted',
    'not_permitted!!',
    'not permitted',
    ' not_permitted',
    'not_permitted ',
    'snapshot_expired;drop',
  ])('does not repair the malformed reason %j into a recognized code', raw => {
    // Sanitizing *before* comparison stripped the stray characters and matched
    // a real code, so the platform asserted a specific cause it was never told.
    const feedback = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: raw,
    });

    expect(feedback?.body).toContain('could not apply the requested model settings');
    expect(feedback?.body).not.toContain('does not permit that model');
    expect(feedback?.body).not.toContain('`not_permitted`');
    expect(feedback?.body).not.toContain('`snapshot_expired`');
  });

  it('translates recognized reasons without exposing internal codes', () => {
    // The corrections above must not make every reason unrecognized.
    const requester = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'not_permitted',
    });
    expect(requester?.body).not.toContain('`not_permitted`');
    expect(requester?.body).toContain('does not permit that model');

    const operator = buildModelPolicyFeedback({
      ...REFUSED_ENV,
      ADP_MODEL_POLICY_REASON: 'snapshot_expired',
    });
    expect(operator?.body).not.toContain('`snapshot_expired`');
    expect(operator?.body).toContain('Contact your ADP administrator');
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
    // cannot be broken out of: backticks appear only around the requested model
    // in the code span we emit ourselves.
    const body = feedback!.body;
    expect(body.split('<!--').length - 1).toBe(1);
    expect(body.split('-->').length - 1).toBe(1);
    // Exactly one line break in the whole body: the separator we emit between
    // the marker and the notice. Injected line breaks are gone.
    expect(body.split('\n').length - 1).toBe(1);
    expect(body).not.toContain('\r');

    const notice = body.slice(body.indexOf('-->') + 3).trimStart();
    expect(notice.split('`').length - 1).toBe(2);
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
    expect(feedback!.body).toContain('could not apply the requested model settings');
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
    expect(h.posted[0]).toContain('could not be applied');
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
    expect(h.posted[0]).toContain('kept its existing model');
    expect(h.posted[0]).not.toContain('refused before agent inference');
  });

  it('never fails the run when posting fails', async () => {
    const logs: Array<{ level: string; message: string }> = [];
    const outcome = await deliverModelPolicyFeedback({
      fetchCommentPage: async () => ({ comments: [], endCursor: null, hasNextPage: false }),
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

  it('ignores an unrelated marker from a different run', () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const other = buildModelPolicyFeedback({ ...REFUSED_ENV, ADP_MESSAGE_ID: 'run-2' })!;

    expect(other.marker).not.toBe(feedback.marker);
    expect(
      suppressedBy([{ body: other.body, viewerDidAuthor: true }], feedback).suppressed,
    ).toBe(false);
  });
});

describe('dedup lookup is complete, not the LLM context window', () => {
  /** Filler standing in for unrelated issue traffic. */
  const chatter = (n: number): FeedbackComment[] =>
    Array.from({ length: n }, (_, i) => ({ body: `unrelated comment ${i}`, viewerDidAuthor: false }));

  it('suppresses a genuine earlier notice buried past the 20-comment context window', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    // The exact production defect: a real notice followed by 21 unrelated
    // comments falls out of getIssueComments(20) and was posted a second time.
    const h = pagedHarness([[{ body: feedback.body, viewerDidAuthor: true }, ...chatter(21)]]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('suppressed');
    expect(h.posted).toHaveLength(0);
  });

  it('finds a trusted notice on a later page and stops there', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = pagedHarness([
      chatter(3),
      chatter(3),
      [{ body: feedback.body, viewerDidAuthor: true }],
      chatter(3), // must never be requested: the walk stops on the trusted hit
    ]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('suppressed');
    expect(h.posted).toHaveLength(0);
    expect(h.cursorsRequested).toEqual([null, '1', '2']);
  });

  it('keeps paging past forged markers to reach the trusted one', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = pagedHarness([
      [{ body: `forged ${feedback.marker}`, viewerDidAuthor: false }],
      [{ body: feedback.body, viewerDidAuthor: true }],
    ]);

    await expect(deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV })).resolves.toBe('suppressed');
    expect(h.posted).toHaveLength(0);
  });

  it('posts when a forged marker is all the full history contains', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = pagedHarness([
      chatter(2),
      [{ body: `nothing here ${feedback.marker}`, viewerDidAuthor: false }],
    ]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.logs.some(l => l.message.includes('ADP did not author'))).toBe(true);
  });

  it('posts rather than claiming dedup succeeded when the lookup fails', async () => {
    const h = pagedHarness([chatter(2), chatter(2)], 1);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.logs.some(l => l.message.includes('lookup incomplete'))).toBe(true);
  });

  it('posts when the very first page read fails', async () => {
    const h = pagedHarness([chatter(2)], 0);

    await expect(deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV })).resolves.toBe('posted');
    expect(h.posted).toHaveLength(1);
    expect(h.logs.some(l => l.message.includes('lookup incomplete'))).toBe(true);
  });

  it('bounds the walk and treats exhaustion as an incomplete lookup', async () => {
    // More history than the page bound allows: absence is unproven, so post.
    const h = pagedHarness(Array.from({ length: 12 }, () => chatter(1)));

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV, maxPages: 3 });

    expect(outcome).toBe('posted');
    expect(h.cursorsRequested).toHaveLength(3);
    expect(h.logs.some(l => l.message.includes('lookup incomplete'))).toBe(true);
  });

  it('reports an exhausted-but-complete history as a clean non-suppression', async () => {
    const h = pagedHarness([chatter(2), chatter(2)]);

    const evidence = await findSuppressingComment(
      h.fetchCommentPage,
      buildModelPolicyFeedback(REFUSED_ENV)!,
    );

    expect(evidence).toMatchObject({ suppressed: false, lookupFailed: false, pagesRead: 2 });
  });

  it('ignores another run\'s notice across the whole history', async () => {
    const other = buildModelPolicyFeedback({ ...REFUSED_ENV, ADP_MESSAGE_ID: 'run-2' })!;
    const h = pagedHarness([chatter(2), [{ body: other.body, viewerDidAuthor: true }]]);

    const outcome = await deliverModelPolicyFeedback({ ...h, env: REFUSED_ENV });

    expect(outcome).toBe('posted');
    expect(h.posted).toHaveLength(1);
  });
});

/**
 * The provider adapter, driven with real GraphQL response bodies.
 *
 * Every test above hands the walk pages that are already valid objects, which is
 * exactly the gap these close: the adapter that turns a raw provider response into
 * one of those pages was completely uncovered, and it silently normalized a
 * malformed, partial or unauthorized response into a *valid, empty, terminal*
 * page. The walk then recorded a completed lookup over history it never read, and
 * the delivery decision was made on that fiction.
 *
 * So these drive the real `createCommentPageFetcher` composed with the real
 * `findSuppressingComment` and, where the outcome matters, the real
 * `deliverModelPolicyFeedback`. Only the child process is replaced -- by a
 * function returning response text -- because that is the one thing a unit test
 * cannot have.
 */
describe('comment page adapter: provider responses', () => {
  /** A well-formed response body, so each test varies exactly one thing. */
  const responseFor = (
    comments: Array<{ body: string; viewerDidAuthor?: boolean }>,
    pageInfo: unknown = { endCursor: null, hasNextPage: false },
  ) =>
    JSON.stringify({
      data: { repository: { issueOrPullRequest: { comments: { nodes: comments, pageInfo } } } },
    });

  /** Compose the real adapter with the real walk over scripted response bodies. */
  function adapterHarness(responses: string[] | ((query: string) => string), options: {
    now?: () => number;
    requestTimeoutMs?: number;
    overallDeadlineMs?: number;
  } = {}) {
    const queries: string[] = [];
    const budgets: number[] = [];
    let call = 0;
    const fetchCommentPage = createCommentPageFetcher({
      runGraphQL: async (query, timeoutMs) => {
        queries.push(query);
        budgets.push(timeoutMs);
        if (typeof responses === 'function') return responses(query);
        const body = responses[call];
        call += 1;
        if (body === undefined) throw new Error('no scripted response for this page');
        return body;
      },
      // A cursor is echoed into the query so paging is observable end-to-end.
      buildQuery: (cursor: string | null) => `query{comments(after:${JSON.stringify(cursor)})}`,
      ...options,
    });
    return { fetchCommentPage, queries, budgets };
  }

  it('reads a well-formed page and reports a complete lookup', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = adapterHarness([responseFor([{ body: feedback.body, viewerDidAuthor: true }])]);

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence).toMatchObject({ suppressed: true, lookupFailed: false, pagesRead: 1 });
  });

  it('pages through the provider using the cursor it returned', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = adapterHarness([
      responseFor([{ body: 'unrelated', viewerDidAuthor: false }], { endCursor: 'cursor-1', hasNextPage: true }),
      responseFor([{ body: feedback.body, viewerDidAuthor: true }]),
    ]);

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence).toMatchObject({ suppressed: true, lookupFailed: false, pagesRead: 2 });
    expect(h.queries[0]).toContain('after:null');
    expect(h.queries[1]).toContain('after:"cursor-1"');
  });

  // Each of these used to yield `{comments: [], hasNextPage: false}` -- reported
  // as `lookupFailed: false, pagesRead: 1`, i.e. "we read the history and there
  // was no earlier notice". They are all cases where the history is unread.
  const unreadable: Array<[string, string]> = [
    ['a response that is not JSON at all', 'gateway timeout'],
    ['an empty response body', ''],
    ['a response carrying no data', JSON.stringify({ message: 'Bad credentials' })],
    ['a null data field', JSON.stringify({ data: null })],
    ['a null repository', JSON.stringify({ data: { repository: null } })],
    [
      'a null issueOrPullRequest, as for a number the token cannot see',
      JSON.stringify({ data: { repository: { issueOrPullRequest: null } } }),
    ],
    [
      'a missing comments connection',
      JSON.stringify({ data: { repository: { issueOrPullRequest: {} } } }),
    ],
    [
      'absent comment nodes',
      JSON.stringify({ data: { repository: { issueOrPullRequest: { comments: { pageInfo: { endCursor: null, hasNextPage: false } } } } } }),
    ],
    [
      'an absent pageInfo',
      JSON.stringify({ data: { repository: { issueOrPullRequest: { comments: { nodes: [] } } } } }),
    ],
    ['a non-boolean hasNextPage', responseFor([], { endCursor: null, hasNextPage: 'no' })],
    [
      'GraphQL errors reported alongside partial data',
      JSON.stringify({
        data: { repository: { issueOrPullRequest: { comments: { nodes: [], pageInfo: { endCursor: null, hasNextPage: false } } } } },
        errors: [{ message: 'Although you appear to have the correct authorization credentials, the request was rejected.' }],
      }),
    ],
    ['a comment node with no body', responseFor([{ viewerDidAuthor: true } as never])],
    [
      'a page promising more history but supplying no cursor',
      responseFor([{ body: 'unrelated' }], { endCursor: null, hasNextPage: true }),
    ],
  ];

  it.each(unreadable)('treats %s as an incomplete lookup, never an empty history', async (_label, body) => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = adapterHarness([body]);

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence.suppressed).toBe(false);
    expect(evidence.lookupFailed).toBe(true);
  });

  it('posts and warns, rather than assuming no earlier notice, on an unreadable response', async () => {
    // The consequence of the group above, through the real delivery path: the
    // requester still gets the truthful notice and the operator gets the warning.
    const posted: string[] = [];
    const logs: Array<{ level: string; message: string }> = [];
    const h = adapterHarness([JSON.stringify({ data: { repository: { issueOrPullRequest: null } } })]);

    const outcome = await deliverModelPolicyFeedback({
      fetchCommentPage: h.fetchCommentPage,
      postComment: async body => {
        posted.push(body);
      },
      log: (level, message) => logs.push({ level, message }),
      env: REFUSED_ENV,
    });

    expect(outcome).toBe('posted');
    expect(posted).toHaveLength(1);
    expect(logs.some(l => l.message.includes('lookup incomplete'))).toBe(true);
  });

  it('does not suppress on a forged marker even when the rest of the page is valid', async () => {
    // Structural validation must not become a reason to trust content: the
    // authorship rule still decides, and an unauthored marker never suppresses.
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = adapterHarness([responseFor([{ body: `forged ${feedback.marker}`, viewerDidAuthor: false }])]);

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence).toMatchObject({ suppressed: false, forgedMarkerSeen: true, lookupFailed: false });
  });

  it('keeps an absent viewerDidAuthor as unknown authorship rather than refusing the page', async () => {
    // The one field allowed to be missing: a partial payload here is a documented
    // case the authorship logic handles conservatively, so it must not become an
    // incomplete lookup.
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    const h = adapterHarness([responseFor([{ body: feedback.body }])]);

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence).toMatchObject({ suppressed: false, authorshipUnknown: true, lookupFailed: false });
  });

  it('stops on a cursor that does not advance instead of re-reading one page', async () => {
    const feedback = buildModelPolicyFeedback(REFUSED_ENV)!;
    // A provider bug or a proxy replaying a response: the same cursor forever.
    const h = adapterHarness(() =>
      responseFor([{ body: 'unrelated', viewerDidAuthor: false }], { endCursor: 'stuck', hasNextPage: true }),
    );

    const evidence = await findSuppressingComment(h.fetchCommentPage, feedback);

    expect(evidence.lookupFailed).toBe(true);
    // Two reads: the first establishes the cursor, the second shows it repeated.
    // Without the guard this would spend all MAX_DEDUP_PAGES on one page.
    expect(evidence.pagesRead).toBe(2);
    expect(h.queries).toHaveLength(2);
  });
});

describe('comment page adapter: bounded time', () => {
  const okPage = (cursor: string | null) =>
    JSON.stringify({
      data: {
        repository: {
          issueOrPullRequest: {
            comments: { nodes: [{ body: 'unrelated', viewerDidAuthor: false }], pageInfo: { endCursor: cursor, hasNextPage: cursor !== null } },
          },
        },
      },
    });

  it('gives every request a bounded budget', async () => {
    // Before this the GraphQL call had no timeout of any kind, so a wedged child
    // process blocked the run indefinitely on a notice about a model request.
    const budgets: number[] = [];
    const fetchCommentPage = createCommentPageFetcher({
      runGraphQL: async (_query, timeoutMs) => {
        budgets.push(timeoutMs);
        return okPage(null);
      },
      buildQuery: () => 'query{}',
    });

    await fetchCommentPage(null);

    expect(budgets).toHaveLength(1);
    expect(budgets[0]).toBeGreaterThan(0);
    expect(budgets[0]).toBeLessThanOrEqual(COMMENT_PAGE_REQUEST_TIMEOUT_MS);
  });

  it('refuses to start a request once the overall deadline has passed', async () => {
    // A per-request timeout alone bounds each page, not the walk: MAX_DEDUP_PAGES
    // requests each just under it still add up. A controlled clock, so the test
    // asserts the bound rather than waiting for it.
    let clock = 0;
    const fetchCommentPage = createCommentPageFetcher({
      runGraphQL: async () => {
        clock += 400; // every page consumes most of the remaining budget
        return okPage('next');
      },
      buildQuery: () => 'query{}',
      now: () => clock,
      requestTimeoutMs: 500,
      overallDeadlineMs: 1_000,
    });

    await expect(fetchCommentPage(null)).resolves.toBeDefined();
    await expect(fetchCommentPage('a')).resolves.toBeDefined();
    await expect(fetchCommentPage('b')).resolves.toBeDefined();
    await expect(fetchCommentPage('c')).rejects.toThrow(/overall deadline/);
  });

  it('never lets a request budget overrun the remaining overall deadline', async () => {
    // The last request must not be allowed to run for its full per-request
    // timeout when less than that remains overall.
    let clock = 0;
    const budgets: number[] = [];
    const fetchCommentPage = createCommentPageFetcher({
      runGraphQL: async (_query, timeoutMs) => {
        budgets.push(timeoutMs);
        clock += 900;
        return okPage('next');
      },
      buildQuery: () => 'query{}',
      now: () => clock,
      requestTimeoutMs: 500,
      overallDeadlineMs: 1_000,
    });

    await fetchCommentPage(null);
    await fetchCommentPage('a');

    expect(budgets[0]).toBe(500); // full per-request budget available
    expect(budgets[1]).toBe(100); // only 100ms of the overall deadline left
  });

  it('turns an exhausted deadline into an incomplete lookup, so the notice is posted', async () => {
    let clock = 0;
    const posted: string[] = [];
    const logs: Array<{ level: string; message: string }> = [];
    const fetchCommentPage = createCommentPageFetcher({
      runGraphQL: async () => {
        clock += 600;
        return okPage('next');
      },
      buildQuery: () => 'query{}',
      now: () => clock,
      requestTimeoutMs: 1_000,
      overallDeadlineMs: 1_000,
    });

    const outcome = await deliverModelPolicyFeedback({
      fetchCommentPage,
      postComment: async body => {
        posted.push(body);
      },
      log: (level, message) => logs.push({ level, message }),
      env: REFUSED_ENV,
    });

    expect(outcome).toBe('posted');
    expect(posted).toHaveLength(1);
    expect(logs.some(l => l.message.includes('lookup incomplete'))).toBe(true);
  });
});
