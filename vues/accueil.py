"""
vues/accueil.py — Page d'accueil : la couverture du tableau de bord
====================================================================
Composée comme la première slide d'une présentation : grand titre, illustration
de couverture, puis les deux portes d'entrée du système et quelques chiffres.

Aucune logique métier ici — la page ne lit qu'un CSV pour afficher des
compteurs. Elle sert d'orientation, pas de travail.

Note technique : une balise HTML ne peut pas déclencher un changement de page
dans Streamlit. Chaque case est donc un visuel suivi d'un bouton, stylés
ensemble pour ne former qu'un seul bloc.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import theme

auth.barre_laterale()

SOURCE = Path(__file__).parent.parent / 'etats_stations.csv'
C = theme.couleurs()


@st.cache_data
def chiffres() -> dict:
    """Quelques compteurs pour la bande de chiffres clés."""
    if not SOURCE.exists():
        return {}
    df = pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig',
                     dtype={'code_station': str})
    return {
        'stations':     len(df),
        'gouvernorats': df['gouvernorat'].nunique(),
        'en_marche':    int((df['etat_dgre'] == 'marche').sum()),
        'regles':       17,
    }


st.markdown(f"""
<style>
  /* ---------- CASES D'ENTRÉE ---------- */
  .porte {{
      position: relative; overflow: hidden;
      border: 1px solid var(--border); border-bottom: none;
      border-radius: var(--r-lg) var(--r-lg) 0 0;
      background: var(--panel); box-shadow: var(--ombre);
      padding: var(--esp-xl) var(--esp-lg) var(--esp-lg) var(--esp-lg);
      min-height: 260px;
      transition: box-shadow var(--transition);
  }}
  .porte:hover {{ box-shadow: var(--ombre-levee); }}
  /* Disque d'accent en haut à droite : la composition asymétrique du modèle */
  .porte::before {{
      content: ""; position: absolute; right: -60px; top: -70px;
      width: 200px; height: 200px; border-radius: 50%;
      background: radial-gradient(circle, {C['or']}22, transparent 70%);
  }}
  .porte .numero {{
      font-family: var(--f-titre); font-size: 3.4rem; font-weight: 300;
      color: var(--or); line-height: 1; opacity: 0.55;
  }}
  .porte .nom {{
      font-family: var(--f-titre); font-weight: 600; font-style: italic;
      font-size: var(--t-h2); color: var(--accent);
      margin: var(--esp-sm) 0 var(--esp-sm) 0; position: relative; z-index: 1;
  }}
  .porte .desc {{
      font-family: var(--f-texte); font-size: var(--t-petit);
      color: var(--text); opacity: 0.78; line-height: 1.7; max-width: 42ch;
  }}
  .porte .note {{
      font-family: var(--f-texte); font-size: var(--t-label);
      letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
      margin-top: var(--esp-md); border-top: 1px solid var(--border);
      padding-top: var(--esp-sm);
  }}

  /* Le bouton prolonge la case au lieu de flotter dessous */
  .porte + div .stButton button {{
      width: 100%; border-radius: 0 0 var(--r-lg) var(--r-lg) !important;
      border: 1px solid var(--border) !important; border-top: none !important;
      background: var(--panel2) !important; color: var(--accent) !important;
      padding: 1rem 0 !important; letter-spacing: 0.16em;
  }}
  .porte + div .stButton button:hover {{
      background: var(--accent) !important; color: var(--panel) !important;
      border-color: var(--accent) !important; transform: none !important;
  }}

  /* ---------- BANDE DE CHIFFRES ---------- */
  .chiffres {{
      display: flex; flex-wrap: wrap; gap: var(--esp-xl);
      justify-content: space-between;
      border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
      padding: var(--esp-lg) var(--esp-md); margin: var(--esp-xl) 0;
  }}
  .chiffres .bloc {{ flex: 1 1 140px; }}
  .chiffres .valeur {{
      font-family: var(--f-titre); font-size: clamp(2rem, 4vw, 3rem);
      font-weight: 300; color: var(--accent); line-height: 1;
  }}
  .chiffres .etiq {{
      font-family: var(--f-texte); font-size: var(--t-label);
      letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted);
      margin-top: var(--esp-sm);
  }}

  @media (max-width: 900px) {{
      .porte {{ min-height: auto; padding: var(--esp-lg) var(--esp-md); }}
      .porte::before {{ display: none; }}
      .chiffres {{ gap: var(--esp-lg); }}
  }}
</style>
""", unsafe_allow_html=True)


def porte(numero: str, nom: str, description: str, note: str,
          cle: str, cible: str):
    st.markdown(f"""
      <div class="porte">
        <div class="numero">{numero}</div>
        <div class="nom">{nom}</div>
        <div class="desc">{description}</div>
        <div class="note">{note}</div>
      </div>""", unsafe_allow_html=True)
    if st.button("Accéder", key=cle, use_container_width=True):
        st.switch_page(cible)


def main():
    n = chiffres()

    theme.hero_banner(
        "Surveillance", "du réseau national",
        "Détection automatique des anomalies hydrologiques et pluviométriques "
        "des stations de la Direction Générale des Ressources en Eau.",
        tag="DGRE · République Tunisienne", paysage=False)

    # Visuel de couverture — uniquement ici, comme la première slide
    # d'une présentation.
    st.markdown(theme.illustration_village(), unsafe_allow_html=True)

    if n:
        st.markdown(f"""
        <div class="chiffres">
          <div class="bloc"><div class="valeur">{n['stations']}</div>
            <div class="etiq">Stations suivies</div></div>
          <div class="bloc"><div class="valeur">{n['gouvernorats']}</div>
            <div class="etiq">Gouvernorats</div></div>
          <div class="bloc"><div class="valeur">{n['regles']}</div>
            <div class="etiq">Règles de détection</div></div>
          <div class="bloc"><div class="valeur">{n['en_marche']}</div>
            <div class="etiq">Stations en marche</div></div>
        </div>""", unsafe_allow_html=True)

    gauche, droite = st.columns(2, gap="large")

    with gauche:
        porte("01", "Inventaire des stations",
              "L'état de chaque station du réseau, sur carte. Répartition par "
              "état, listes détaillées, fiches individuelles et demandes de "
              "vérification en attente.",
              "Relevé DGRE · détections de l'agent",
              "btn_inventaire", "vues/inventaire.py")

    with droite:
        porte("02", "Cohérence des données",
              "Dépôt et analyse des fichiers .MIS : valeurs aberrantes, "
              "ruptures et données manquantes, station par station et sur "
              "l'historique complet.",
              "17 règles · instantanées, long terme, inter-stations",
              "btn_coherence", "vues/coherence.py")

    st.markdown(
        "<div style='text-align:center;margin-top:var(--esp-xxl);'>"
        "<span class='etiquette'>Prototype de stage · ENIT</span></div>",
        unsafe_allow_html=True)


main()
