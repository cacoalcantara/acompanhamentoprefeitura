// Valida o painel em Chromium real e gera capturas em docs/capturas/. SOMENTE simulacao.
// Uso: PAINEL_URL=http://127.0.0.1:8799 ROBOWIN_TOKEN=... DIR_DADOS=<dir do EMERGENCIA> node scripts/capturas_painel.mjs
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const URL_BASE = process.env.PAINEL_URL || "http://127.0.0.1:8799";
const TOKEN = process.env.ROBOWIN_TOKEN || "";
const DIR_DADOS = process.env.DIR_DADOS || "";
const SAIDA = path.resolve(process.env.SAIDA || "docs/capturas");
fs.mkdirSync(SAIDA, { recursive: true });

const PAGINAS = [
  ["visao-geral", "/", "Visão geral"], ["decisoes", "/decisoes", "Decisões"],
  ["operacoes", "/operacoes", "Operações"], ["desempenho", "/desempenho", "Desempenho"],
  ["modelos", "/modelos", "Modelos"], ["auditoria", "/auditoria", "Auditoria"],
];
const INGLES = [/\bLoading\b/, /\bundefined\b/i, /\bNaN\b/, /\bTraceback\b/, /\bInternal Server Error\b/, /\bNo data\b/];
const falhas = [], ok = [];
const check = (c, msg) => (c ? ok : falhas).push(msg);

const browser = await chromium.launch();
async function rodar(rotulo, contextoOpts) {
  const ctx = await browser.newContext(contextoOpts);
  const page = await ctx.newPage();
  const erros = [];
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) erros.push(`${m.type()}: ${m.text()}`); });
  page.on("pageerror", (e) => erros.push("pageerror: " + e.message));
  page.on("response", (r) => { if (r.status() >= 400) erros.push(`HTTP ${r.status()} ${r.url()}`); });
  for (const [slug, rota, nome] of PAGINAS) {
    erros.length = 0;
    await page.goto(URL_BASE + rota, { waitUntil: "networkidle" });
    if (slug === "desempenho") await page.waitForSelector("#grafico-capital canvas", { timeout: 8000 }).catch(() => {});
    const tag = `[${rotulo}] ${nome}`;
    const corpo = await page.innerText("body");
    check(corpo.includes("SIMULAÇÃO — sem ordens reais"), `${tag}: faixa SIMULAÇÃO presente`);
    check(corpo.includes("DADOS SINTÉTICOS"), `${tag}: faixa DADOS SINTÉTICOS presente`);
    check((await page.getAttribute("html", "lang")) === "pt-BR", `${tag}: html lang=pt-BR`);
    check(!INGLES.some((r) => r.test(corpo)), `${tag}: sem textos em ingles/placeholder`);
    check(erros.length === 0, `${tag}: sem erros de console/rede ${erros.join(" | ")}`);
    if (slug === "desempenho") {
      check((await page.locator("#grafico-capital canvas").count()) > 0, `${tag}: uPlot renderizou canvas`);
    }
    const sc = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(sc <= 0, `${tag}: sem scroll horizontal (excesso ${sc}px)`);
    await page.screenshot({ path: path.join(SAIDA, `${slug}${rotulo === "celular" ? "-celular" : ""}.png`), fullPage: true });
  }
  return { ctx, page };
}

const desk = await rodar("desktop", { viewport: { width: 1280, height: 900 } });
{ // htmx: o resumo deve ser trocado periodicamente (hx-trigger every 5s)
  const page = desk.page;
  await page.goto(URL_BASE + "/", { waitUntil: "networkidle" });
  await page.evaluate(() => { document.querySelector("#resumo").dataset.marca = "antigo"; });
  const r = await page.waitForResponse((x) => x.url().includes("/fragmentos/resumo") && x.status() === 200, { timeout: 9000 }).catch(() => null);
  await page.waitForTimeout(300);
  const marca = await page.evaluate(() => document.querySelector("#resumo")?.dataset.marca || null);
  check(!!r && marca === null, "htmx: /fragmentos/resumo requisitado e #resumo substituido");
  check((await page.locator("#resumo").count()) === 1, "htmx: #resumo unico e presente apos o swap");
}
{ // kill switch
  const page = desk.page;
  const flag = DIR_DADOS ? path.join(DIR_DADOS, "EMERGENCIA") : null;
  if (flag && fs.existsSync(flag)) fs.unlinkSync(flag);
  const post = (data) => page.request.post(URL_BASE + "/acoes/emergencia",
    { headers: { "content-type": "application/x-www-form-urlencoded" }, data, maxRedirects: 0 });
  let r = await post("confirmacao=EMERGENCIA");
  check(r.status() === 401, `kill switch sem token bloqueado (HTTP ${r.status()})`);
  if (flag) check(!fs.existsSync(flag), "kill switch sem token: flag NAO gravada");
  r = await post(`token=${encodeURIComponent(TOKEN)}&confirmacao=errado`);
  check(r.status() === 400, `token certo sem frase de confirmacao rejeitado (HTTP ${r.status()})`);
  if (flag) check(!fs.existsSync(flag), "sem frase: flag NAO gravada");
  await page.goto(URL_BASE + "/", { waitUntil: "networkidle" });
  page.once("dialog", (d) => d.accept());
  await page.fill('input[name="token"]', TOKEN);
  await page.fill('input[name="confirmacao"]', "EMERGENCIA");
  await Promise.all([page.waitForURL(/msg=emergencia/), page.click("button.perigo")]);
  await page.waitForLoadState("networkidle");
  const t = await page.innerText("body");
  check(/aguardando confirmação do gateway/.test(t), "UI: mostra 'aguardando confirmação do gateway'");
  check(!/Confirmado:/.test(t), "UI: NAO inventa confirmacao do gateway");
  if (flag) check(fs.existsSync(flag), "UI: flag EMERGENCIA gravada em disco");
  await page.goto(URL_BASE + "/", { waitUntil: "networkidle" });
  const t2 = await page.innerText("body");
  check(/aguardando confirmação do gateway/.test(t2) && !/Confirmado:/.test(t2), "UI apos recarregar: segue aguardando (sem confirmacao)");
  await page.screenshot({ path: path.join(SAIDA, "kill-switch-aguardando.png"), fullPage: true });
}
await desk.ctx.close();
const cel = await rodar("celular", { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
await cel.ctx.close();
await browser.close();

console.log(ok.map((m) => "OK    " + m).join("\n"));
console.log(falhas.map((m) => "FALHA " + m).join("\n"));
console.log(`\n${ok.length} ok, ${falhas.length} falhas`);
process.exit(falhas.length ? 1 : 0);
