/* Demander ce service (V2-23b, §3.1) — guide voyageur.

   Pilote le VRAI /frontend/guide/app.js (initRequestService) dans Chrome headless
   (harnais guide-request-harness.html) : le bouton « Demander ce service » d'une
   section « requestable » ouvre un formulaire MONTÉ, le POST vers
   `${apiBase}/requests` (ici un lien de séjour → /b/{stay_token}/requests, V2-23c
   volet 1bis) transmet la section + le message, et le succès affiche un accusé de
   réception. Verdict lu dans le DOM dumpé (ignoré si aucun Chrome).

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

test("§3.1 : « Demander ce service » ouvre un formulaire, POST section+message, accuse réception", async (t) => {
  const chrome = requireChrome("guide-request.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "guide-request-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("guide-request-harness.html", verdict);
  } finally {
    server.close();
  }
});
