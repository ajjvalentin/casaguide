/* Traduction d'un `detail` d'erreur d'API en message français lisible.

   Module pur (aucune dépendance DOM/session) → testable hors navigateur
   (`frontend-tests/apierrors.test.mjs`). Utilisé par `api.js`.

   Trois formes de `detail` sont possibles :
     - chaîne          : erreurs applicatives classiques (« Email déjà pris »…).
     - objet {code,message} : refus de quota 402 (V2-05a) → on garde le message.
     - liste [{loc,msg,type}] : erreurs de validation Pydantic (422). FastAPI
       renvoie une LISTE (pas une chaîne) → sans traitement, l'UI affichait
       « Erreur serveur (422) » au lieu du détail (V2-16, constat c). On mappe
       chaque champ vers un libellé FR. */

function fieldLabel(loc) {
  // `loc` = ["body", "<champ>"] (ou plus profond). On prend le dernier segment.
  const field = Array.isArray(loc) && loc.length ? loc[loc.length - 1] : "";
  switch (field) {
    case "email": return "Adresse email invalide.";
    case "password": return "Mot de passe invalide (8 caractères minimum).";
    case "full_name": return "Indiquez votre nom complet.";
    case "token": return "Lien invalide ou expiré.";
    default: return "Un champ du formulaire est invalide.";
  }
}

/* Erreurs de validation 422 → un message FR (dédoublonné, ordre stable). */
export function messageFromValidation(errors) {
  const seen = new Set();
  const msgs = [];
  for (const e of errors) {
    const m = fieldLabel(e && e.loc);
    if (!seen.has(m)) { seen.add(m); msgs.push(m); }
  }
  return msgs.join(" ");
}

/* Message FR lisible à partir du `status` HTTP et du `detail` renvoyé. */
export function messageFromDetail(status, detail) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const m = messageFromValidation(detail);
    if (m) return m;
  } else if (detail && typeof detail === "object" && detail.message) {
    return detail.message;
  }
  return `Erreur serveur (${status}).`;
}
