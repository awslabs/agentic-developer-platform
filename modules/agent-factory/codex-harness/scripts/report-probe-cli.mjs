import { readFile } from 'node:fs/promises';
import { captureReportProbe } from './report-probe.mjs';
const [persona, model] = process.argv.slice(2);
const manifest = JSON.parse(await readFile(new URL('./report-probe-manifest.json', import.meta.url)));
if (!manifest.personas[persona]?.[model]) throw new Error('Unknown report contract');
const captured = await captureReportProbe(persona, model);
if (captured.digest !== manifest.personas[persona][model]) throw new Error('Report SDK contract changed');
process.stdout.write(JSON.stringify(captured));
