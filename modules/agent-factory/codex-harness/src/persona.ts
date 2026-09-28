import { createHash } from "node:crypto";
import { z } from "zod";

export const capabilitySchema = z.enum([
  "repository.read", "repository.write", "branch.push", "change.create",
  "change.update", "review.submit", "change.merge", "story.create", "tests.run",
  "artifacts.publish", "agents.delegate", "aws.assume", "aws.mutate",
]);
export type Capability = z.infer<typeof capabilitySchema>;
export const surfaceSchema = z.enum(["github", "gitlab", "task-api", "delegation"]);
const digest = z.string().regex(/^[a-f0-9]{64}$/);
const unique = <T>(values: T[]) => new Set(values).size === values.length;

export const personaSchema = z.object({
  schemaVersion: z.literal(1),
  key: z.string().regex(/^gpt-[a-z][a-z0-9-]{0,62}$/),
  revision: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/),
  displayName: z.string().min(1).max(100),
  instructions: z.string().min(1).max(24000),
  sharedRules: z.object({
    version: z.literal(1), persona: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/),
    sources: z.array(z.object({ path: z.string().regex(/^[a-zA-Z0-9_.\/-]+$/).max(255), sha256: digest }).strict()).min(1).max(32),
  }).strict().optional(),
  skills: z.array(z.object({
    id: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/), sha256: digest,
    requiredCapabilities: z.array(capabilitySchema).refine(unique).optional(),
    requiredTools: z.array(z.string().regex(/^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$/)).max(32).refine(unique).optional(),
  }).strict()).max(16).refine(v => unique(v.map(s => s.id)), "Duplicate skill"),
  requiredCapabilities: z.array(capabilitySchema).refine(unique),
  optionalCapabilities: z.array(capabilitySchema).refine(unique),
  surfaces: z.array(surfaceSchema).min(1).refine(unique),
  completionPolicy: z.enum(["report", "validated-change", "review-repair-merge", "operations", "aidlc"]),
  effort: z.enum(["low", "medium", "high", "xhigh"]),
  limits: z.object({
    maxTurns: z.number().int().min(1).max(100),
    maxContextBytes: z.number().int().min(1024).max(262144),
    maxDurationMs: z.number().int().min(1000).max(21600000),
  }).strict(),
}).strict().superRefine((value, context) => {
  if (value.requiredCapabilities.some(c => value.optionalCapabilities.includes(c))) {
    context.addIssue({ code: "custom", message: "Required and optional capabilities overlap" });
  }
  const needs: Partial<Record<typeof value.completionPolicy, Capability[]>> = {
    "validated-change": ["repository.read", "repository.write", "tests.run", "change.create"],
    "review-repair-merge": ["repository.read", "repository.write", "tests.run", "branch.push", "review.submit", "change.merge"],
    operations: ["aws.assume"], aidlc: ["agents.delegate", "artifacts.publish"],
  };
  if (needs[value.completionPolicy]?.some(c => !value.requiredCapabilities.includes(c))) {
    context.addIssue({ code: "custom", message: "Completion policy lacks required capabilities" });
  }
});
export type Persona = z.infer<typeof personaSchema>;

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
export const sha256 = (text: string) => createHash("sha256").update(text).digest("hex");

/** Parse configuration, not authority. Only an authorized catalogue can publish it. */
export function snapshotPersona(raw: string, skillContent: ReadonlyMap<string, string>) {
  if (Buffer.byteLength(raw) > 65536) throw new Error("Persona definition exceeds 64 KiB");
  const persona = personaSchema.parse(JSON.parse(raw));
  if (persona.sharedRules && (new Set(persona.sharedRules.sources.map(s => s.path)).size !== persona.sharedRules.sources.length
    || persona.sharedRules.sources.some(s => s.path.startsWith('/') || s.path.split('/').some(p => !p || p === '.' || p === '..')))) {
    throw new Error("Invalid shared rule references");
  }
  const skills = persona.skills.map(skill => {
    const content = skillContent.get(skill.id);
    if (content === undefined || Buffer.byteLength(content) > 65536 || sha256(content) !== skill.sha256) {
      throw new Error(`Missing or mismatched skill: ${skill.id}`);
    }
    return content;
  });
  const shared = persona.sharedRules;
  if (shared && persona.key !== `gpt-${shared.persona}`) throw new Error("Shared rules belong to another persona");
  const instructions = [persona.instructions, ...skills].join("\n\n");
  if (Buffer.byteLength(instructions) > persona.limits.maxContextBytes) {
    throw new Error("Persona and skills exhaust the context budget");
  }
  // Return serialized immutable data; callers cannot mutate the admitted snapshot.
  return Object.freeze({ definition: canonical(persona), digest: sha256(canonical(persona)), instructions,
    skillSources: JSON.stringify(persona.skills.map((skill, index) => [skill.id, skills[index]])),
  });
}

/** Reconstruct composed instructions from digest-bound sources after transport. */
export function verifySnapshot(snapshot: ReturnType<typeof snapshotPersona>) {
  if (Buffer.byteLength(snapshot.skillSources) > 2 * 1024 * 1024) throw new Error("Snapshot skill sources exceed bound");
  const sources = z.array(z.tuple([z.string(), z.string()])).max(16).parse(JSON.parse(snapshot.skillSources));
  if (new Set(sources.map(([id]) => id)).size !== sources.length) throw new Error("Duplicate snapshot skill source");
  const verified = snapshotPersona(snapshot.definition, new Map(sources));
  if (verified.digest !== snapshot.digest || verified.instructions !== snapshot.instructions) throw new Error("Snapshot instruction binding mismatch");
  return verified;
}
