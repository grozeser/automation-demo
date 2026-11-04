// Скриншоты сценариев из редактора n8n (последнее успешное выполнение): node shots.cjs → docs/<n>.png
// Нужны запущенный стенд и .env от check.py.
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');
const { chromium } = createRequire('E:/Портфолио/omega-hakaton/e2e/')('@playwright/test');

const N8N = 'http://localhost:5678';
const env = Object.fromEntries(fs.readFileSync(path.join(__dirname, '.env'), 'utf8').split(/\r?\n/).filter(Boolean).map((l) => l.split(/=(.*)/s).slice(0, 2)));
const api = (p) => fetch(`${N8N}/api/v1${p}`, { headers: { 'X-N8N-API-KEY': env.N8N_API_KEY } }).then((r) => r.json());

(async () => {
    fs.mkdirSync(path.join(__dirname, 'docs'), { recursive: true });
    const browser = await chromium.launch({ channel: 'msedge' });
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 820 }, deviceScaleFactor: 1.5 });
    const login = await ctx.request.post(`${N8N}/rest/login`, { data: { emailOrLdapLoginId: env.N8N_EMAIL, password: env.N8N_PASSWORD } });
    if (!login.ok()) throw new Error(`login ${login.status()}`);
    const page = await ctx.newPage();
    const { data: flows } = await api('/workflows');
    for (const wf of flows.sort((a, b) => a.name.localeCompare(b.name))) {
        // самое первое успешное выполнение — основной сценарий из check.py (новая заявка, успешная оплата…)
        const { data: all } = await api(`/executions?workflowId=${wf.id}&status=success&limit=50`);
        const runs = all.slice(-1);
        const url = runs.length ? `${N8N}/workflow/${wf.id}/executions/${runs[0].id}` : `${N8N}/workflow/${wf.id}`;
        await page.goto(url, { waitUntil: 'load' }); // у n8n постоянное соединение — networkidle не наступит
        await page.locator('[data-test-id="canvas-node"]').first().waitFor({ timeout: 30000 }).catch(() => {});
        // на странице выполнения холст внутри iframe-превью или основного вида — ждём узлы
        await page.waitForTimeout(2500);
        for (const sel of ['button:has-text("Got it")', 'button[aria-label="Close"]']) {
            const b = page.locator(sel).first();
            if (await b.isVisible().catch(() => false)) await b.click().catch(() => {});
        }
        const n = wf.name.split('.')[0];
        await page.screenshot({ path: path.join(__dirname, 'docs', `${n}.png`), clip: { x: 375, y: 58, width: 1125, height: 640 } }); // только холст
        console.log('ok', wf.name, runs.length ? 'выполнение ' + runs[0].id : 'без выполнений');
    }
    await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
