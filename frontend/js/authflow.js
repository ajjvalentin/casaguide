/* Enchaînement d'inscription / connexion (V2-16), extrait de la vue pour être
   testable hors navigateur (`frontend-tests/authflow.test.mjs`).

   Invariants du parcours (constats de production du 24/07) :
     - La session (jeton) est posée et VÉRIFIÉE (`me()`) AVANT tout appel
       authentifié — le Checkout Stripe compris.
     - Une offre payante choisie à l'inscription → redirection Checkout. Si le
       Checkout échoue (Stripe indispo, 402/503…), l'utilisateur reste CONNECTÉ
       et atterrit sur « Mon abonnement » avec un message — jamais éjecté.
     - Aucun 401 ne déclenche l'éjection « session expirée » pendant ce parcours
       (route publique) : `deps.suppress(true/false)` encadre les appels.

   `deps` (tous injectables) :
     register, login  : (body) → {access_token}
     me               : () → owner
     startCheckout    : (planId) → {url}
     setToken, setOwner, redirect, navigate, toast, suppress
*/

/* Résultat structuré (l'appelant DOM n'a rien d'autre à décider) :
     {outcome: "redirect"}                    → redirection Checkout en cours
     {outcome: "properties"}                  → entrée dans le back-office
     {outcome: "abonnement", message}         → connecté, Checkout à finaliser
     {outcome: "error", message}              → resté sur le formulaire
*/
export async function submitAuth({ mode, planId, credentials, deps }) {
  const {
    register, login, me, startCheckout,
    setToken, setOwner, redirect, navigate, toast, suppress,
  } = deps;

  suppress(true);
  try {
    let tokenResp;
    try {
      tokenResp = mode === "register"
        ? await register(credentials)
        : await login({ email: credentials.email, password: credentials.password });
      // Pose le jeton AVANT tout appel authentifié, puis vérifie qu'il ouvre bien
      // le profil : la session est établie et confirmée avant le Checkout.
      setToken(tokenResp.access_token);
      setOwner(await me());
    } catch (err) {
      // Échec de création/connexion OU de la vérification du profil : on reste
      // sur le formulaire avec un message lisible, jamais d'éjection.
      return { outcome: "error", message: errMessage(err) };
    }

    // Offre payante choisie à l'inscription → Checkout Stripe. Le compte reste
    // gratuit si le paiement est abandonné (seul le webhook change l'abonnement).
    if (mode === "register" && planId && planId !== "free") {
      try {
        const { url } = await startCheckout(planId);
        redirect(url);
        return { outcome: "redirect" };
      } catch (_) {
        const message = "Compte créé. Terminez votre souscription depuis "
          + "« Mon abonnement ».";
        if (toast) toast(message, "");
        navigate("#/abonnement");
        return { outcome: "abonnement", message };
      }
    }

    navigate("#/properties");
    return { outcome: "properties" };
  } finally {
    suppress(false);
  }
}

function errMessage(err) {
  return err && typeof err.message === "string" && err.message
    ? err.message
    : "Une erreur est survenue.";
}
