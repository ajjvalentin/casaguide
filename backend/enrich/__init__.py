"""Pipeline d'enrichissement CasaGuide.

Le chargement de `backend/.env` est déclenché ici (au premier import du paquet)
afin que `enrich/settings.py` et toute la configuration voient les variables du
fichier — que le pipeline soit lancé en ligne de commande ou via l'API.
"""
from .envfile import load_env

load_env()
