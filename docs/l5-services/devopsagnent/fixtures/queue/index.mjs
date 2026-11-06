// Synthetic invoice event validator. No network or provider side effects.
export function validateInvoice(body) {
  const value = JSON.parse(body);
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== "amount_cents,invoice_id") throw new Error("schema_invalid");
  if (typeof value.invoice_id !== "string" || !/^[a-z0-9-]{1,64}$/.test(value.invoice_id)) throw new Error("invoice_id_invalid");
  if (!Number.isSafeInteger(value.amount_cents) || value.amount_cents < 0 || value.amount_cents > 1000000) throw new Error("amount_invalid");
  return { invoice_id: value.invoice_id, amount_cents: value.amount_cents };
}
export async function handler(event) {
  if (!event || !Array.isArray(event.Records)) throw new Error("records_required");
  const batchItemFailures = [];
  for (const record of event.Records) {
    if (typeof record.messageId !== "string" || !record.messageId) throw new Error("message_id_required");
    try { validateInvoice(record.body); }
    catch { batchItemFailures.push({ itemIdentifier: record.messageId }); }
  }
  return { batchItemFailures };
}
