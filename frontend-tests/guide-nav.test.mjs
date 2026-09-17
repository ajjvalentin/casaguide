/* Navigation par grille de pictogrammes du guide voyageur (V2-12).

   Pilote le VRAI /frontend/guide/app.js dans Chrome headless (harnais
   guide-nav-harness.html) : tap tuile → hash #autour/{cat} + défilement au bloc,
   retour arrière (popstate), réinitialisation du filtre à la navigation,
   coexistence avec les listes repliées et le filtre cuisine. Verdict lu dans le
   DOM dumpé (test ignoré proprement si aucun Chrome n'est disponible).

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

/* Lance le harnais dans Chrome headless et renvoie le verdict lu dans #result.
   `--dump-dom` écrit le DOM mais NE QUITTE PAS proprement (macOS, headless=new) :
   on lit le flux et on tue Chrome dès que le verdict est présent. */
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
    // Chrome peut encore écrire son cache au moment du kill → nettoyage tolérant
    // (retries) et jamais fatal : le verdict a déjà été lu.
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 120 }); }
    catch { /* profil temporaire : sans importance */ }
  }
}

test("grille V2-12 : tap tuile → hash + défilement, retour arrière, filtre, listes repliées", async (t) => {
  const chrome = requireChrome("guide-nav.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "guide-nav-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("guide-nav-harness.html", verdict);
  } finally {
    server.close();
  }
});
