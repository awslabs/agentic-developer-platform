export async function restrictToFirstParty(context, address) {
  const origin = new URL(address).origin;
  if (new URL(address).protocol !== 'https:') throw new Error('first-party HTTPS origin required');
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.protocol !== 'https:' || url.origin !== origin) {
      await route.abort();
      return;
    }
    const response = await route.fetch({ maxRedirects: 0 });
    if (response.status() >= 300 && response.status() < 400) {
      await route.abort();
      return;
    }
    await route.fulfill({ response });
  });
}
