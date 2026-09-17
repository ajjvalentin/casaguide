/* Régression V2-18c — le stepper d'add-on (`frontend/js/views/subscription.js`)
   doit MONTER son bouton « Confirmer ». Le composant est vérifié sur son arbre
   RENDU (querySelector dans un vrai DOM Chrome), jamais via une référence
   interne : un élément câblé mais non inséré dans le retour était « vert » et a
   laissé passer le bug en prod.

   Exécuter : node --test frontend-tests/
   (test ignoré proprement si aucun Chrome n'est disponible). */

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

// Serveur statique minimal enraciné au dépôt : les imports ES absolus
// (/frontend/js/…) résolvent quel que soit l'emplacement du harnais.
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

/* Lance un harnais dans Chrome headless et renvoie le verdict lu dans #result.
   `--dump-dom` écrit le DOM sur stdout mais NE QUITTE PAS proprement sur ce build
   (macOS, headless=new) : on lit le flux et on tue Chrome dès que le verdict est
   dans le DOM, sans attendre la sortie du process. */
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
    // Chrome, tué au SIGKILL, écrit encore dans son profil → rmSync peut lever
    // ENOTEMPTY. C'était le SEUL des 30 fichiers sans cette garde : d'où un rouge
    // ALÉATOIRE sur les tests d'abonnement, qui ne signifiait rien (V2-76 : un rouge
    // doit vouloir dire quelque chose). Un profil temporaire résiduel est sans effet.
    try { fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 120 }); }
    catch { /* profil temporaire : sans importance */ }
  }
}

test("le bouton « Confirmer » du stepper add-on est monté dans le DOM (V2-18c)", async (t) => {
  const chrome = requireChrome("subscription.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "subscription-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("subscription-harness.html", verdict);
  } finally {
    server.close();
  }
});

test("« Passer en Solo » (downgrade) → confirmation datée puis change-plan (V2-18d/e)", async (t) => {
  const chrome = requireChrome("subscription.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "changeplan-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("changeplan-harness.html", verdict);
  } finally {
    server.close();
  }
});

test("bandeau de changement programmé + annulation (V2-18e)", async (t) => {
  const chrome = requireChrome("subscription.test.mjs");   // pas de navigateur → LÈVE (V2-76)
  if (!chrome) { t.skip("aucun navigateur — cf. bandeau final"); return; }   // hatch explicite
  const server = await startServer();
  try {
    const verdict = await runHarness(chrome, server.address().port, "scheduledchange-harness.html");
    assert.ok(verdict, "verdict du harnais introuvable dans le DOM dumpé");
    reportVerdict("scheduledchange-harness.html", verdict);
  } finally {
    server.close();
  }
});
