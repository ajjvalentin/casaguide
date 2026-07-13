/* Générateur de QR code autonome, sans dépendance (M-08, §3.2).

   Portée volontairement réduite au strict besoin du guide : mode octet (UTF-8),
   niveau de correction M, versions 1 à 6 (jusqu'à ~106 octets — largement
   suffisant pour une chaîne « WIFI:T:WPA;S:…;P:…;; »). Sélection automatique du
   masque par score de pénalité (règles standard ISO/IEC 18004).

   Algorithme fidèle à l'implémentation de référence de Project Nayuki (MIT) —
   encodage, Reed-Solomon (GF(256), 0x11d), placement en zigzag, patrons, info
   de format BCH(15,5). Vérifié par comparaison matricielle exacte avec segno
   (masque forcé) : voir backend/tests / scripts de vérification M-08.

   export qrMatrix(text) → matrice booléenne carrée (true = module noir), SANS
   zone de silence (l'appelant ajoute la marge au tracé). */

// Niveau M ; tables indexées par version (1..6). Index 0 inutilisé.
const ECC_PER_BLOCK = [0, 10, 16, 26, 18, 24, 16];
const NUM_BLOCKS = [0, 1, 1, 1, 2, 2, 4];
// Positions des patrons d'alignement (hors version 1).
const ALIGN = [[], [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34]];
const MAX_VERSION = 6;

function getBit(x, i) { return ((x >>> i) & 1) !== 0; }

// ── GF(256) : multiplication (polynôme primitif 0x11d) ───────────────────────
function mul(x, y) {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
}

function rsDivisor(degree) {
  const result = new Array(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = mul(result[j], root);
      if (j + 1 < result.length) result[j] ^= result[j + 1];
    }
    root = mul(root, 2);
  }
  return result;
}

function rsRemainder(data, divisor) {
  const result = new Array(divisor.length).fill(0);
  for (const b of data) {
    const factor = b ^ result.shift();
    result.push(0);
    for (let i = 0; i < result.length; i++) result[i] ^= mul(divisor[i], factor);
  }
  return result;
}

// ── Nombre de modules de données bruts (fonction de la version) ──────────────
function rawDataModules(ver) {
  let result = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const numAlign = Math.floor(ver / 7) + 2;
    result -= (25 * numAlign - 10) * numAlign - 55;
  }
  return result; // (versions ≥ 7 retrancheraient 36 pour l'info de version)
}

// ── Encodage des données (mode octet) + choix de version ─────────────────────
function encodeData(text) {
  const bytes = new TextEncoder().encode(text);
  for (let ver = 1; ver <= MAX_VERSION; ver++) {
    const rawCw = Math.floor(rawDataModules(ver) / 8);
    const dataCw = rawCw - NUM_BLOCKS[ver] * ECC_PER_BLOCK[ver];
    const capacity = dataCw * 8;
    const needed = 4 + 8 + 8 * bytes.length; // mode + compteur (8 bits, v≤9) + data
    if (needed <= capacity) return { ver, dataCw, bytes };
  }
  return null; // trop long pour la version 6 (cas irréaliste pour un wifi)
}

function buildCodewords(ver, dataCw, bytes) {
  const bits = [];
  const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
  push(0b0100, 4);            // indicateur de mode : octet
  push(bytes.length, 8);      // compteur de caractères (versions 1..9 → 8 bits)
  for (const b of bytes) push(b, 8);
  const cap = dataCw * 8;
  push(0, Math.min(4, cap - bits.length));          // terminateur
  while (bits.length % 8 !== 0) bits.push(0);        // alignement octet
  const pad = [0xec, 0x11];
  for (let i = 0; bits.length < cap; i++) push(pad[i % 2], 8);  // octets de remplissage

  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
    data.push(byte);
  }
  return data;
}

// ── Reed-Solomon + entrelacement des blocs ───────────────────────────────────
function addEcc(data, ver) {
  const numBlocks = NUM_BLOCKS[ver];
  const eccLen = ECC_PER_BLOCK[ver];
  const rawCw = Math.floor(rawDataModules(ver) / 8);
  const numShort = numBlocks - (rawCw % numBlocks);
  const shortLen = Math.floor(rawCw / numBlocks);
  const divisor = rsDivisor(eccLen);
  const blocks = [];
  for (let i = 0, k = 0; i < numBlocks; i++) {
    const datLen = shortLen - eccLen + (i < numShort ? 0 : 1);
    const dat = data.slice(k, k + datLen);
    k += dat.length;
    const ecc = rsRemainder(dat, divisor);
    if (i < numShort) dat.push(0); // égalise la longueur pour l'entrelacement
    blocks.push(dat.concat(ecc));
  }
  const result = [];
  for (let i = 0; i < blocks[0].length; i++) {
    for (let j = 0; j < blocks.length; j++) {
      // saute la case de remplissage des blocs courts
      if (i !== shortLen - eccLen || j >= numShort) result.push(blocks[j][i]);
    }
  }
  return result;
}

// ── Construction de la matrice ───────────────────────────────────────────────
function qrMatrix(text, opts = {}) {
  const enc = encodeData(text);
  if (!enc) return null;
  const { ver, dataCw, bytes } = enc;
  const codewords = addEcc(buildCodewords(ver, dataCw, bytes), ver);

  const size = ver * 4 + 17;
  const modules = Array.from({ length: size }, () => new Array(size).fill(false));
  const isFn = Array.from({ length: size }, () => new Array(size).fill(false));
  const set = (x, y, dark) => { modules[y][x] = dark; isFn[y][x] = true; };

  // Patrons de recherche (finders) + séparateurs
  const finder = (x, y) => {
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const xx = x + dx, yy = y + dy;
      if (xx < 0 || xx >= size || yy < 0 || yy >= size) continue;
      const dist = Math.max(Math.abs(dx), Math.abs(dy));
      set(xx, yy, dist !== 2 && dist !== 4);
    }
  };
  finder(3, 3); finder(size - 4, 3); finder(3, size - 4);

  // Motifs de synchronisation (timing)
  for (let i = 0; i < size; i++) {
    if (!isFn[6][i]) set(i, 6, i % 2 === 0);
    if (!isFn[i][6]) set(6, i, i % 2 === 0);
  }

  // Patrons d'alignement
  const pos = ALIGN[ver];
  for (let i = 0; i < pos.length; i++) for (let j = 0; j < pos.length; j++) {
    if ((i === 0 && j === 0) || (i === 0 && j === pos.length - 1) ||
        (i === pos.length - 1 && j === 0)) continue; // coïncident avec les finders
    const cx = pos[i], cy = pos[j];
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++)
      set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }

  // Réserve les zones d'info de format (remplies après le choix du masque)
  const reserveFormat = () => {
    for (let i = 0; i <= 8; i++) { if (!isFn[i][8]) set(8, i, false); if (!isFn[8][i]) set(i, 8, false); }
    for (let i = 0; i < 8; i++) { set(size - 1 - i, 8, false); set(8, size - 1 - i, false); }
    set(8, size - 8, true); // module toujours noir
  };
  reserveFormat();

  // Placement des mots de code en zigzag
  let bit = 0;
  const totalBits = codewords.length * 8;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5; // saute la colonne de timing
    for (let vert = 0; vert < size; vert++) {
      for (let k = 0; k < 2; k++) {
        const x = right - k;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (!isFn[y][x] && bit < totalBits) {
          modules[y][x] = getBit(codewords[bit >>> 3], 7 - (bit & 7));
          bit++;
        }
      }
    }
  }

  const maskFn = [
    (x, y) => (x + y) % 2 === 0,
    (x, y) => y % 2 === 0,
    (x, y) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
    (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
    (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
    (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
  ];

  const applyMask = (m) => {
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++)
      if (!isFn[y][x] && maskFn[m](x, y)) modules[y][x] = !modules[y][x];
  };

  const drawFormat = (m) => {
    const data = (0 << 3) | m; // niveau M = 0b00
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    for (let i = 0; i <= 5; i++) set(8, i, getBit(bits, i));
    set(8, 7, getBit(bits, 6));
    set(8, 8, getBit(bits, 7));
    set(7, 8, getBit(bits, 8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, getBit(bits, i));
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, getBit(bits, i));
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, getBit(bits, i));
    set(8, size - 8, true);
  };

  // Choix du masque : score de pénalité minimal (règles standard)
  let chosen = opts.forceMask != null ? opts.forceMask : 0;
  if (opts.forceMask == null) {
    let best = Infinity;
    for (let m = 0; m < 8; m++) {
      drawFormat(m); applyMask(m);
      const score = penalty(modules, size);
      applyMask(m); // annule
      if (score < best) { best = score; chosen = m; }
    }
  }
  drawFormat(chosen); applyMask(chosen);
  return modules;
}

// ── Score de pénalité (ISO/IEC 18004, fidèle à Nayuki) ───────────────────────
function penalty(modules, size) {
  let result = 0;
  const addHistory = (run, hist) => { if (hist[0] === 0) run += size; hist.pop(); hist.unshift(run); };
  const countPatterns = (h) => {
    const n = h[1];
    const core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;
    return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0);
  };
  const terminate = (color, run, hist) => {
    if (color) { addHistory(run, hist); run = 0; }
    run += size; addHistory(run, hist);
    return countPatterns(hist);
  };
  // Règles 1 & 3 — lignes
  for (let y = 0; y < size; y++) {
    let color = false, run = 0; const hist = [0, 0, 0, 0, 0, 0, 0];
    for (let x = 0; x < size; x++) {
      if (modules[y][x] === color) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
      else { addHistory(run, hist); if (!color) result += countPatterns(hist) * 40; color = modules[y][x]; run = 1; }
    }
    result += terminate(color, run, hist) * 40;
  }
  // Règles 1 & 3 — colonnes
  for (let x = 0; x < size; x++) {
    let color = false, run = 0; const hist = [0, 0, 0, 0, 0, 0, 0];
    for (let y = 0; y < size; y++) {
      if (modules[y][x] === color) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
      else { addHistory(run, hist); if (!color) result += countPatterns(hist) * 40; color = modules[y][x]; run = 1; }
    }
    result += terminate(color, run, hist) * 40;
  }
  // Règle 2 — blocs 2×2
  for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) {
    const c = modules[y][x];
    if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) result += 3;
  }
  // Règle 4 — proportion de modules noirs
  let dark = 0;
  for (const row of modules) for (const c of row) if (c) dark++;
  const total = size * size;
  const k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
  return result + k * 10;
}

export { qrMatrix };
