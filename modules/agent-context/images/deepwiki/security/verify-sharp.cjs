// Exercise the sharp instance that Next's image optimizer actually resolves.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const fromNext = createRequire('/app/node_modules/next/package.json');
const sharp = fromNext('sharp');
const { optimizeImage } = fromNext('./dist/server/image-optimizer');

(async () => {
  assert.equal(sharp.versions.sharp, '0.35.5');
  const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="blue"/></svg>');
  const results = {};
  for (const [contentType, format] of [['image/jpeg', 'jpeg'], ['image/webp', 'webp'], ['image/png', 'png']]) {
    const output = await optimizeImage({ buffer: svg, width: 32, quality: 80, contentType });
    const metadata = await sharp(output).metadata();
    assert.equal(metadata.width, 32);
    assert.equal(metadata.height, 24);
    assert.equal(metadata.format, format);
    results[contentType] = 'passed';
  }
  const raster = await sharp(svg).png().toBuffer();
  const resized = await optimizeImage({ buffer: raster, width: 16, quality: 80, contentType: 'image/webp' });
  assert.equal((await sharp(resized).metadata()).width, 16);
  results.raster_resize = 'passed';
  await assert.rejects(() => optimizeImage({ buffer: Buffer.from('invalid image'), width: 32, quality: 80, contentType: 'image/webp' }));
  console.log(JSON.stringify({ sharp: sharp.versions, next_image_optimizer: results, malformed_rejected: true }));
})().catch(error => { console.error(error); process.exitCode = 1; });
