"""Test de bout en bout contre l'API locale — crée un logement réel,
lance l'enrichissement, suit le job et affiche le guide obtenu.

Usage (le serveur uvicorn doit tourner) :
    python scripts/smoke_test.py "Calle Ejemplo 1" "03189" "Orihuela Costa" ES
    python scripts/smoke_test.py            # adresse de démonstration

Le script utilise un compte de démonstration dédié (demo@casaguide.local),
créé automatiquement au premier lancement.
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = "http://localhost:8000"
DEMO = {"email": "demo@casaguide-demo.com", "password": "demo1234!",
        "full_name": "Compte Démo", "locale": "fr"}


def die(msg: str) -> None:
    print(f"\n❌ {msg}")
    sys.exit(1)


def get_token(c: httpx.Client) -> str:
    r = c.post(f"{BASE}/api/auth/register", json=DEMO)
    if r.status_code == 409:  # compte déjà créé lors d'un lancement précédent
        r = c.post(f"{BASE}/api/auth/login",
                   json={"email": DEMO["email"], "password": DEMO["password"]})
    if r.status_code not in (200, 201):
        die(f"Auth impossible ({r.status_code}) : {r.text}")
    return r.json()["access_token"]


def main() -> None:
    args = sys.argv[1:]
    address = {
        "name": "Logement de test",
        "address_line1": args[0] if len(args) > 0 else "Avenida Alameda del Mar 1",
        "postal_code":   args[1] if len(args) > 1 else "03189",
        "city":          args[2] if len(args) > 2 else "Orihuela Costa",
        "country_code": (args[3] if len(args) > 3 else "ES").upper(),
    }

    with httpx.Client(timeout=30) as c:
        try:
            c.get(f"{BASE}/openapi.json")
        except httpx.ConnectError:
            die("Le serveur ne répond pas — lancer d'abord : uvicorn api.main:app --reload")

        c.headers["Authorization"] = f"Bearer {get_token(c)}"
        print(f"✅ Authentifié ({DEMO['email']})")

        props = c.get(f"{BASE}/api/properties").json()
        prop = next((p for p in props
                     if p.get("address_line1") == address["address_line1"]
                     and p.get("city") == address["city"]), None)
        if prop:
            print("ℹ️  Logement existant réutilisé (même adresse)")
        else:
            r = c.post(f"{BASE}/api/properties", json=address)
            if r.status_code == 402 and props:  # limite du plan -> dernier existant
                prop = props[-1]
                print(f"ℹ️  Limite du plan : réutilisation de"
                      f" « {prop.get('address_line1','?')}, {prop.get('city','?')} »")
            elif r.status_code not in (200, 201):
                die(f"Création du logement refusée ({r.status_code}) : {r.text}")
            else:
                prop = r.json()
        pid, token = prop["id"], prop.get("guide_token", "?")
        print(f"✅ Logement actif : {prop.get('address_line1','?')}, {prop.get('city','?')}  (id {pid[:8]}…)")

        r = c.post(f"{BASE}/api/properties/{pid}/enrich", json={"trigger": "initial"})
        if r.status_code == 429:
            die("Quota d'enrichissement du plan atteint pour ce logement ce mois-ci.")
        if r.status_code not in (200, 201, 202):
            die(f"Enrichissement refusé ({r.status_code}) : {r.text}")
        job_id = r.json().get("job_id") or r.json().get("id")
        print(f"⏳ Enrichissement lancé (job {str(job_id)[:8]}…) — comptez 30 à 90 s")

        deadline, job = time.time() + 600, {}
        while time.time() < deadline:
            time.sleep(5)
            for url in (f"{BASE}/api/properties/{pid}/jobs/{job_id}",
                        f"{BASE}/api/jobs/{job_id}"):
                r = c.get(url)
                if r.status_code == 200:
                    job = r.json()
                    break
            steps = job.get("steps") or {}
            done_steps = ",".join(k for k, v in steps.items() if isinstance(v, dict) and v.get("ok"))
            ov = steps.get("overpass") or {}
            prog = (f" | {ov.get('pois')} POI, catégorie en cours : {ov.get('in_progress')}"
                    if ov.get("in_progress") and not ov.get("ok") else "")
            print(f"   … statut {job.get('status','?'):<8} étapes ok : {done_steps or '—'}{prog}")
            if job.get("status") in ("done", "failed"):
                break
        if job.get("status") != "done":
            die(f"Job en échec ou trop long : {job.get('error') or job}")
        skipped = (job.get("steps") or {}).get("overpass", {}).get("failed") or {}
        if skipped:
            print(f"⚠️  Catégories sautées (serveurs OSM indisponibles) : {', '.join(skipped)}")

        r = c.get(f"{BASE}/g/{token}")
        if r.status_code != 200:
            die(f"Guide inaccessible ({r.status_code}) : {r.text}")
        guide = r.json()

        print("\n" + "=" * 62)
        print("🏠 GUIDE GÉNÉRÉ — synthèse")
        print("=" * 62)
        p = guide.get("property", guide)
        if p.get("geocode_accuracy"):
            print(f"Géocodage : précision « {p['geocode_accuracy']} »")

        pois = guide.get("pois", [])
        by_cat: dict[str, list] = {}
        for poi in pois:
            by_cat.setdefault(poi.get("category_code", "?"), []).append(poi)
        print(f"\nPOI suggérés/approuvés retournés : {len(pois)}")
        for cat in sorted(by_cat):
            best = min(by_cat[cat], key=lambda x: x.get("dist_walk_m") or 9e9)
            walk = best.get("walk_min")
            drive = best.get("drive_min")
            print(f"  {cat:<16} {len(by_cat[cat]):>2}  plus proche : {best.get('name','?')[:34]:<34}"
                  f" {'' if walk is None else str(walk)+' min à pied'}"
                  f"{'' if drive is None else ' / ' + str(drive) + ' min en voiture'}")

        facts = guide.get("area_facts") or {}
        if isinstance(facts, list):  # tolérance aux deux formats
            facts = {f.get("fact_type", "?"): f.get("content", {}) for f in facts}
        if facts:
            print(f"\nDonnées locales (area_facts) : {len(facts)}")
            for ft, content in facts.items():
                if ft == "emergency_numbers":
                    nums = ", ".join(f"{i.get('label')}: {i.get('number')}"
                                     for i in content.get("items", [])[:4])
                    print(f"  urgences : {nums}")
                else:
                    print(f"  {ft} : {str(content.get('summary',''))[:70]}")

        print(f"\n🔗 Guide complet : {BASE}/g/{token}")
        print("   (et interface Swagger : GET /g/{guide_token} dans /docs)")


if __name__ == "__main__":
    main()
