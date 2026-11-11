import {bundle} from '@remotion/bundler';
import {getCompositions, renderMedia, renderStill} from '@remotion/renderer';
import path from 'node:path';
import fs from 'node:fs';

const root = path.resolve('.');
const browserExecutable = process.env.REMOTION_BROWSER;
if (!browserExecutable) throw new Error('Set REMOTION_BROWSER to an installed Chromium executable before rendering.');
const serveUrl = await bundle({entryPoint: path.join(root, 'src/system-agentic-intro.tsx'), onProgress: () => {}});
const compositions = await getCompositions(serveUrl, {browserExecutable, chromiumOptions: {gl: 'angle'}, logLevel: 'error'});
const composition = compositions.find((item) => item.id === 'SystemAgenticIntro');
if (!composition) throw new Error('SystemAgenticIntro composition missing');
fs.mkdirSync(path.join(root, 'out'), {recursive: true});
const mode = process.argv[2] || 'video';
if (mode === 'stills') {
  for (const second of [0, 5, 13, 22, 33, 43, 51, 53]) {
    await renderStill({serveUrl, composition, output: path.join(root, 'out', `frame-${String(second).padStart(2, '0')}.png`), frame: second * 60, browserExecutable, chromiumOptions: {gl: 'angle'}, logLevel: 'error'});
    console.log(`STILL ${second}s`);
  }
} else {
  await renderMedia({serveUrl, composition, outputLocation: path.join(root, 'out', 'system-agentic-intro-1080p.mp4'), codec: 'h264', crf: 15, pixelFormat: 'yuv420p', imageFormat: 'png', browserExecutable, chromiumOptions: {gl: 'angle'}, concurrency: 4, logLevel: 'error', onProgress: ({progress}) => {
    const mark = Math.floor(progress * 10) * 10;
    if (mark !== globalThis.lastMark) { globalThis.lastMark = mark; console.log(`RENDER ${mark}%`); }
  }});
  console.log('RENDER_DONE');
}
