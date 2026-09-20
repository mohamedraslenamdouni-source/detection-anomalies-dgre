"""
site.py — Point d'entrée du tableau de bord DGRE
=================================================
Lancer :
    py -m streamlit run site.py

Pourquoi ce fichier plutôt que le dossier pages/ natif de Streamlit :
  Le dossier pages/ affiche AUTOMATIQUEMENT toutes les pages dans la barre
  latérale, y compris avant la connexion. N'importe qui verrait la structure
  du site sans être identifié, et pourrait accéder aux pages directement.

  st.navigation() construit le menu DYNAMIQUEMENT : tant que la connexion
  n'est pas faite, aucune page n'existe pour Streamlit. C'est la seule façon
  propre de protéger un site multipage.

Arborescence attendue :
    site.py
    auth.py
    theme.py
    vues/accueil.py
    vues/inventaire.py
    vues/coherence.py
    etats_stations.csv
    .streamlit/secrets.toml     ← créé par creer_identifiants.py
"""

import streamlit as st

import auth

st.set_page_config(page_title="HYDRAS Agent — DGRE",
                   page_icon="💧", layout="wide",
                   initial_sidebar_state="collapsed")

# Thème visuel : palette clair/sombre, injectée avant tout affichage
import theme
theme.inject_theme()

# ── Barrière d'accès ──────────────────────────
# Tant que l'utilisateur n'est pas identifié, page_connexion() appelle
# st.stop() : rien de ce qui suit n'est jamais exécuté ni envoyé au navigateur.
if not auth.est_connecte():
    auth.page_connexion()

# ── Routage (uniquement après connexion) ──────
accueil = st.Page("vues/accueil.py",    title="Accueil",
                  icon="🏠", url_path="accueil", default=True)
inventaire = st.Page("vues/inventaire.py", title="Inventaire des stations",
                     icon="🗺️", url_path="inventaire")
liste = st.Page("vues/liste.py", title="Liste des stations",
                icon="📋", url_path="liste")
verifications = st.Page("vues/verifications.py", title="Vérifications en attente",
                        icon="⏳", url_path="verifications")
fiche = st.Page("vues/fiche.py", title="Fiche station",
                icon="🔍", url_path="fiche")
coherence = st.Page("vues/coherence.py",  title="Cohérence des données",
                    icon="📊", url_path="coherence")

# position="hidden" : la navigation se fait par les cases de la page d'accueil,
# pas par une liste dans la barre latérale.
st.navigation([accueil, inventaire, liste, fiche, verifications, coherence],
              position="hidden").run()
