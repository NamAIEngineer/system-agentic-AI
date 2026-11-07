import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const workbenchRoot = process.env.SYSTEM_AGENTIC_WORKBENCH_ROOT
  ? path.resolve(process.env.SYSTEM_AGENTIC_WORKBENCH_ROOT)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const playwrightEntry = process.env.SYSTEM_AGENTIC_PLAYWRIGHT
  || path.join(workbenchRoot, 'design-os-system-one/projects/voice-browser-agent/node_modules/playwright/index.mjs');
const { chromium } = await import(pathToFileURL(playwrightEntry).href);

const payload = JSON.parse(process.argv[2] || '{}');
const browser = await chromium.launch({ headless: payload.headless !== false, executablePath: payload.executable });
const page = await browser.newPage({ viewport: payload.viewport || { width: 1280, height: 800 } });
const consoleErrors = [];
const consoleErrorDetails = [];
const networkFailures = [];
const httpFailures = [];
page.on('console', msg => {
  if (msg.type() === 'error') {
    consoleErrors.push(msg.text());
    consoleErrorDetails.push({text: msg.text(), location: msg.location()});
  }
});
page.on('requestfailed', req => networkFailures.push({ url: req.url(), error: req.failure()?.errorText || 'request failed' }));
page.on('response', response => {
  if (response.status() >= 400) {
    const request = response.request();
    httpFailures.push({
      url: response.url(), status: response.status(), status_text: response.statusText(),
      resource_type: request.resourceType(), method: request.method(),
      frame_url: request.frame()?.url() || null,
    });
  }
});
const evidence = [];
try {
  await page.goto(payload.url, { waitUntil: 'domcontentloaded', timeout: payload.timeout_ms });
  await page.waitForTimeout(100);
  evidence.push({ type: 'navigation', url: page.url(), title: await page.title() });
  for (const item of payload.actions || []) {
    const action = item.action;
    if (action === 'wait_for') await page.locator(item.selector).waitFor({ state: item.state || 'visible', timeout: payload.timeout_ms });
    else if (action === 'click') await page.locator(item.selector).click({ timeout: payload.timeout_ms });
    else if (action === 'fill') await page.locator(item.selector).fill(String(item.value ?? ''), { timeout: payload.timeout_ms });
    else if (action === 'select') await page.locator(item.selector).selectOption(String(item.value), { timeout: payload.timeout_ms });
    else if (action === 'press') await page.locator(item.selector).press(String(item.key), { timeout: payload.timeout_ms });
    else if (action === 'assert_text') {
      const locator = page.locator(item.selector);
      const text = await locator.innerText({ timeout: payload.timeout_ms });
      if (!text.includes(String(item.text))) throw new Error(`assert_text failed for ${item.selector}: ${item.text}`);
      evidence.push({ type: 'assertion', selector: item.selector, expected: item.text, observed: text, passed: true });
    } else if (action === 'assert_url') {
      const observed = page.url();
      if (!observed.includes(String(item.contains))) throw new Error(`assert_url failed: ${item.contains}`);
      evidence.push({ type: 'assertion', expected_url_contains: item.contains, observed_url: observed, passed: true });
    } else if (action === 'screenshot') {
      const target = item.path || payload.screenshot;
      await page.screenshot({ path: target, fullPage: Boolean(item.full_page) });
      evidence.push({ type: 'screenshot', path: target });
    } else if (action === 'goto') {
      await page.goto(String(item.url), { waitUntil: 'domcontentloaded', timeout: payload.timeout_ms });
      await page.waitForTimeout(100);
      evidence.push({ type: 'navigation', url: page.url(), title: await page.title() });
    } else throw new Error(`unsupported browser action: ${action}`);
  }
  if (!(payload.actions || []).some(item => item.action === 'screenshot')) {
    await page.screenshot({ path: payload.screenshot, fullPage: true });
    evidence.push({ type: 'screenshot', path: payload.screenshot });
  }
  const domText = await page.locator('body').innerText();
  console.log(JSON.stringify({ status: 'COMPLETED', url: page.url(), title: await page.title(), dom_text: domText.slice(0, 12000), evidence, console_errors: consoleErrors, console_error_details: consoleErrorDetails, network_failures: networkFailures, http_failures: httpFailures, cleanup: 'browser.close completed' }));
} catch (error) {
  try {
    await page.screenshot({ path: payload.screenshot, fullPage: true });
    evidence.push({ type: 'failure-screenshot', path: payload.screenshot });
  } catch (_) {}
  console.log(JSON.stringify({ status: 'FAILED', url: page.url(), title: await page.title(), dom_text: (await page.locator('body').innerText().catch(() => '')).slice(0, 12000), evidence, console_errors: consoleErrors, console_error_details: consoleErrorDetails, network_failures: networkFailures, http_failures: httpFailures, error: String(error?.message || error) }));
  process.exitCode = 1;
} finally {
  await Promise.race([browser.close().catch(() => {}), new Promise(resolve => setTimeout(resolve, 1500))]);
  // Playwright can leave a system Chrome helper event loop alive on macOS;
  // this runner owns the disposable context and must terminate deterministically.
  process.exit(process.exitCode || 0);
}
