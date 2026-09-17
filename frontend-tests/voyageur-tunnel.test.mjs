/* Tunnel voyageur — le refus imprécis se VOIT (V2-68b).

   Pilote le VRAI /frontend/js/views/voyageur.js (renderAdresse) dans Chrome headless
   (harnais voyageur-tunnel-harness.html) : « Tokyo » seul → géocodage 'mismatch'. Vérifie
   qu'après le refus imprécis, la carte s'affiche, le message d'imprécision paraît, le choix
   de quartiers est proposé, le paiement reste bloqué tant qu'aucun ancrage, et le bouton
   « Situer » est réarmé ; choisir un quartier débloque le paiement. Reproduit et prévient
   le gel prod 14/09 (TDZ syncPay). Verdict lu dans le DOM dumpé (ignoré si aucun Chrome).
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

test("V2-68b : le tunnel gère le refus imprécis (carte + message + quartiers + réarmé)", async (t) => {
  const chrome = requireChrome("voyageur-tunnel.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "voyageur-tunnel-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("voyageur-tunnel-harness.html", verdict);
  } finally {
    server.close();
  }
});

test("V2-68c : une adresse introuvable ouvre le placement manuel (repère + paiement)", async (t) => {
  const chrome = requireChrome("voyageur-tunnel.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port,
                                     "voyageur-notfound-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("voyageur-notfound-harness.html", verdict);
  } finally {
    server.close();
  }
});

test("V2-68c : les quartiers ne bloquent plus l'ouverture de la carte", async (t) => {
  const chrome = requireChrome("voyageur-tunnel.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port,
                                     "voyageur-slowhoods-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("voyageur-slowhoods-harness.html", verdict);
  } finally {
    server.close();
  }
});
