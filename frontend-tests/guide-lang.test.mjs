/* Précédence de langue sur le lien de séjour (V2-23c, §3.5).

   Pilote le VRAI /frontend/guide/app.js `initLang` dans Chrome headless, dans les
   DEUX branches :
     · guest_lang renseignée (data-guest-lang) → aucune redirection M-09 (la fiche
       fait foi, ni navigator.language ni la préférence mémorisée ne l'écrasent) ;
     · guest_lang vide → M-09 intact (redirection vers la langue de l'appareil).
   On espionne location.replace (jamais de navigation réelle). Verdict lu dans le
   DOM dumpé (ignoré proprement si aucun Chrome). Patron calendar-harness.

   Exécuter : node --test frontend-tests/ */

import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import os from "node:os";
import { spawn } from "node:child_process";
import { requireChrome, reportVerdict } from "./_harness.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

function startServer() {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split("?")[0]);
    const abs = path.join(REPO_ROOT, path.normalize(rel));
    if (!abs.startsWith(REPO_ROOT)) { res.writeHead(403).end(); return; }
    fs.readFile(abs, (err, buf) => {
      if (err) { res.writeHead(404).end(); return; }
      res.writeHead(200, { "Content-Type": MIME[path.extname(abs)] || "application/octet-stream" });
      res.end(buf);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function runHarness(chrome, port, harness) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "casaguide-chrome-"));
  try {
    const url = `http://127.0.0.1:${port}/frontend-tests/${harness}`;
    const child = spawn(chrome, [
      "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
      "--no-first-run", "--disable-extensions", `--user-data-dir=${profile}`,
      "--virtual-time-budget=6000", "--dump-dom", url,
    ], { stdio: ["ignore", "pipe", "ignore"] });

    let dom = "";
    return await new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(new Error("délai dépassé (aucun verdict)")), 45000);
      const finish = (v) => { clearTimeout(deadline); resolve(v); };
      child.stdout.on("data", (chunk) => {
        dom += chunk;
        const m = dom.match(/<pre id="result">([\s\S]*?)<\/pre>/);
        if (m && m[1].trim() !== "PENDING") finish(m[1].trim());
      });
      child.on("error", (e) => { clearTimeout(deadline); reject(e); });
      child.on("close", () => {
        const m = dom.match(/<pre id="result">([\s\S]*?)<\/pre>/);
        finish(m ? m[1].trim() : "");
      });
    }).finally(() => { child.kill("SIGKILL"); });
  } finally {
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 120 }); }
    catch { /* profil temporaire : sans importance */ }
  }
}

async function withServer(fn, name) {
  const chrome = requireChrome(name);   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) return "SKIP";           // CASAGUIDE_ALLOW_NO_CHROME : sauté MAIS compté
  const server = await startServer();
  try { return await fn(chrome, server.address().port); }
  finally { server.close(); }
}

test("§3.5 : guest_lang renseignée → aucune redirection (la fiche fait foi)", async (t) => {
  const r = await withServer(async (chrome, port) => {
    const v = await runHarness(chrome, port, "guide-lang-harness.html");
    return v;
  }, "guide-lang-harness.html");
  if (r === "SKIP") { t.skip("aucun navigateur — cf. bandeau final"); return; }
  reportVerdict("guide-lang-harness.html", r);
});

test("§3.5 : guest_lang vide → M-09 intact (redirection vers la langue de l'appareil)", async (t) => {
  const r = await withServer(async (chrome, port) => {
    const v = await runHarness(chrome, port, "guide-lang-m09-harness.html");
    return v;
  }, "guide-lang-m09-harness.html");
  if (r === "SKIP") { t.skip("aucun navigateur — cf. bandeau final"); return; }
  reportVerdict("guide-lang-m09-harness.html", r);
});

test("§3.5 amdt 2 : clé guest /b/ surclasse guest_lang + M-09 (sert un autre /b/)", async (t) => {
  const r = await withServer(async (chrome, port) => {
    const v = await runHarness(chrome, port, "guide-lang-b-harness.html");
    return v;
  }, "guide-lang-b-harness.html");
  if (r === "SKIP") { t.skip("aucun navigateur — cf. bandeau final"); return; }
  reportVerdict("guide-lang-b-harness.html", r);
});

test("§3.5 amdt 2 : fiche muette + clé guest posée → clé guest gagne", async (t) => {
  const r = await withServer(async (chrome, port) => {
    const v = await runHarness(chrome, port, "guide-lang-b-muet-harness.html");
    return v;
  }, "guide-lang-b-muet-harness.html");
  if (r === "SKIP") { t.skip("aucun navigateur — cf. bandeau final"); return; }
  reportVerdict("guide-lang-b-muet-harness.html", r);
});
