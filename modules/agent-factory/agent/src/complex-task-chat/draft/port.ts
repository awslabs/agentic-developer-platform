/**
 * IntentDraft port — the live draft an intake conversation builds up.
 *
 * Issue #4208. The draft is what the user watches fill in beside the chat while
 * the intent-refinement persona interviews them. It is stored per session and
 * streamed to the browser as an AG-UI STATE_DELTA patch.
 *
 * Implementations: DynamoDraftStore, NoopDraftStore
 */

/**
 * The draft itself. Every field is optional — a draft one turn into a
 * conversation legitimately has only `intent` set, and the panel renders
 * whatever is present.
 *
 * NOTE: this is deliberately FLAT. The frontend applies STATE_DELTA patches by
 * JSON Pointer, and the whole draft rides a single top-level `/draft` op, so
 * adding a nested object here is fine for the panel but any future
 * field-granular patching must still target `/draft`.
 */
export interface IntentDraft {
  /** One or two sentences: the outcome the user wants. */
  intent?: string;
  /** Model-authored name and purpose for the initial delivery wave. */
  waveDisplay?: { title: string; description: string };
  /** Model-authored capability, motivation and boundaries for the epic. */
  epicDisplay?: { title: string; description: string };
  /** Why they want it — the problem solved or the cost of not having it. */
  motivation?: string;
  /** Concrete, observable results that mean this worked. */
  outcomes?: string[];
  /** Deadlines, systems that must be used or avoided, compliance, budget. */
  constraints?: string[];
  /** What is still unknown, or decisions the user has deferred. */
  openQuestions?: string[];
  /** ISO-8601 timestamp of the last write. Set by the store, not the model. */
  updatedAt?: string;
}

export interface DraftStore {
  /** Read the current draft for a session. Returns null if none exists yet. */
  get(sessionId: string): Promise<IntentDraft | null>;

  /**
   * Replace the draft for a session. Whole-object write, not a merge: the
   * persona is instructed to always send the complete current picture, which
   * keeps "field removed" expressible and avoids read-modify-write races
   * between concurrent turns of the same session.
   *
   * Returns the stored draft including the `updatedAt` the store stamped.
   */
  put(sessionId: string, draft: IntentDraft): Promise<IntentDraft>;
}
