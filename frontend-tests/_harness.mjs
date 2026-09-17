/* Socle commun des harnais headless (V2-76) — DEUX garanties, une seule place.

   CE QUI EST VRAI (mesuré le 17/09, après correction d'une mesure fausse de V2-75) :
   `t.skip()` appelé dans le corps d'un test EST correctement comptabilisé — la ligne
   porte « # SKIP » et le récapitulatif affiche « # skipped N ». La mesure de V2-75 qui
   affirmait le contraire était FAUSSE : `CHROME_BIN=/nonexistent` ne masquait pas le
   navigateur, `findChrome` retombant en silence sur le Chrome système — les tests
   tournaient pour de bon (c'est corrigé ci-dessous : CHROME_BIN fait désormais autorité).

   CE QUI RESTE VRAI, ET SUFFIT À JUSTIFIER CE MODULE : sans navigateur, les 30 fichiers
   de test headless se sautaient TOUS, le process sortait en code 0, aucune ligne
   « not ok », et la couverture front d'une telle exécution était CREUSE. Toute barrière
   qui lit le code de sortie — ou tout humain qui cherche « not ok » — y voyait une
   réussite. Un test qui ne peut rien vérifier ne doit pas rendre un verdict vert.
   Précédent de la même famille : V2-67, où un `return` en tête de module rendait un
   harnais muet (verdict jamais écrit).

   D'où les deux garanties portées ici, et NULLE PART AILLEURS (la duplication de
   `findChrome` en trente exemplaires était précisément ce qui rendait le défaut
   invisible — et ce qui a fait échouer la mesure de V2-75) :

   1. `requireChrome()` — pas de navigateur, pas de test : on LÈVE. La suite est ROUGE.
      Échappatoire délibérée et voyante pour qui n'a pas de navigateur :
      `CASAGUIDE_ALLOW_NO_CHROME=1` → le harnais est sauté, MAIS compté, et un bandeau
      « N harnais NON EXÉCUTÉS » est imprimé sur stderr à la sortie du process, hors du
      flot des lignes `ok`.

   2. `reportVerdict()` — le harnais doit rendre son verdict ET la liste de ce qu'il a
      RÉELLEMENT vérifié (une ligne par assertion, cf. `chk()` côté navigateur). Un
      « PASS » sans aucune ligne de contrôle est un harnais MUET : il est refusé. Les
      lignes sont imprimées à chaque exécution — un harnais qui maigrit (8 contrôles
      hier, 2 aujourd'hui) se voit à l'œil nu.

   Protocole du verdict, écrit par le harnais dans `<pre id="result">` :

       PASS\n✓ premier contrôle\n✓ deuxième contrôle…
       FAIL\n✓ premier contrôle\n✗ deuxième contrôle…                             */

import fs from "node:fs";
import { execSync } from "node:child_process";

// ── 1. Le navigateur ─────────────────────────────────────────────────────────

export function findChrome() {
  // `CHROME_BIN` FAIT AUTORITÉ dès qu'il est posé : désigner un binaire absent, c'est
  // dire « pas de navigateur ». L'ancienne version retombait en silence sur le Chrome
  // système — ce silence a faussé la mesure de V2-75 (on croyait mesurer une suite sans
  // navigateur alors que Chrome tournait) et rendait la recette « sans Chrome »
  // impossible à jouer sur une machine de développement.
  if (process.env.CHROME_BIN) {
    return fs.existsSync(process.env.CHROME_BIN) ? process.env.CHROME_BIN : null;
  }
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  for (const name of ["google-chrome", "chromium", "chromium-browser"]) {
    try { return execSync(`command -v ${name}`, { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); }
    catch { /* absent */ }
  }
  return null;
}

const skipped = [];
let bannerArmed = false;

/** Bandeau final des harnais NON EXÉCUTÉS — sur stderr, hors du flot TAP `ok`,
 *  donc impossible à confondre avec une réussite même en lisant vite. */
function armBanner() {
  if (bannerArmed) return;
  bannerArmed = true;
  process.on("exit", () => {
    if (!skipped.length) return;
    const bar = "═".repeat(72);
    process.stderr.write(
      `\n${bar}\n⚠  ${skipped.length} HARNAIS NON EXÉCUTÉ(S) — AUCUN NAVIGATEUR\n` +
      `   La couverture front de cette exécution est CREUSE : les lignes « ok »\n` +
      `   ci-dessus ne prouvent rien pour ces harnais.\n` +
      [...new Set(skipped)].map((n) => {
        const k = skipped.filter((x) => x === n).length;
        return `     · ${n}${k > 1 ? ` (×${k})` : ""}\n`;
      }).join("") +
      `   (CASAGUIDE_ALLOW_NO_CHROME=1 est actif — retirez-le pour rendre la suite rouge.)\n${bar}\n`);
  });
}

/** Chemin du navigateur, ou LÈVE — l'absence de Chrome ne doit jamais produire
 *  une ligne verte. `name` sert au bandeau de l'échappatoire. */
export function requireChrome(name = "harnais") {
  const chrome = findChrome();
  if (chrome) return chrome;
  if (process.env.CASAGUIDE_ALLOW_NO_CHROME === "1") {
    skipped.push(name);
    armBanner();
    return null;                       // l'appelant sort — comptabilisé, jamais muet
  }
  throw new Error(
    "AUCUN NAVIGATEUR — ce harnais ne peut RIEN vérifier.\n" +
    "  Un test qui ne teste rien doit être rouge (V2-76, leçon V2-75 « mock ≠ réel » n°5).\n" +
    "  Installez Chrome/Chromium, ou désignez-le par CHROME_BIN=/chemin/vers/chrome.\n" +
    "  Pour sauter sciemment (le bandeau final dira lesquels) : CASAGUIDE_ALLOW_NO_CHROME=1.");
}

// ── 2. Le verdict ────────────────────────────────────────────────────────────

/** Relit le verdict d'un harnais, REFUSE un harnais muet, et imprime une ligne
 *  par contrôle réellement exécuté. Retourne le nombre de contrôles. */
export function reportVerdict(label, verdict) {
  if (!verdict) {
    throw new Error(
      `${label} : aucun verdict dans le DOM dumpé — harnais muet (script jamais\n` +
      "  arrivé au bout : exception au chargement, import cassé, `return` en tête\n" +
      "  de module — précédent V2-67). Rien n'a été vérifié.");
  }
  if (verdict.trim() === "PENDING") {
    throw new Error(`${label} : verdict resté PENDING — le harnais n'a pas conclu.`);
  }
  const [head, ...lines] = verdict.split("\n");
  const checks = lines.map((l) => l.trim()).filter(Boolean);
  if (head.trim() !== "PASS") {
    throw new Error(`${label} : harnais en ÉCHEC\n${checks.map((c) => "    " + c).join("\n")}`);
  }
  if (!checks.length) {
    throw new Error(
      `${label} : « PASS » sans AUCUN contrôle listé — harnais muet.\n` +
      "  Un harnais doit publier ce qu'il a vérifié (une ligne par `chk()`), sinon\n" +
      "  son vert ne prouve rien (V2-76).");
  }
  // Une ligne par assertion RÉELLE : c'est ce qui rend visible un harnais qui maigrit.
  for (const c of checks) console.log(`      ${c}`);
  return checks.length;
}
