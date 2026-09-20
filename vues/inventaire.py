"""
vues/inventaire.py — Inventaire des stations : carte, compteurs, répartition
=============================================================================
Affiche l'état de chaque station, en combinant deux sources :

  · etats_stations.csv   l'état DÉCLARÉ par la DGRE — la référence, jamais
                         modifiée par le programme
  · historique.db        les changements VALIDÉS par un humain après
                         vérification sur le terrain

L'état affiché est le premier, remplacé par le second là où une validation
existe. Un halo ambré entoure les stations dont une question est en attente.

L'agent se relance quand les DONNÉES ont changé, pas à intervalle fixe : le
site compare une empreinte de la base (nombre de mesures + dernière date) à
celle du calcul précédent. Même mécanisme pour un dépôt manuel et pour un
futur dépôt par FTP.

Dépendances :  py -m pip install folium streamlit-folium
"""

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import auth
import theme

try:
    import folium
    from streamlit_folium import st_folium
except ImportError:
    folium = None

try:
    import propositions
    import etats_agent
except ImportError:
    propositions = etats_agent = None

auth.barre_laterale()

SOURCE = Path(__file__).parent.parent / 'etats_stations.csv'
BASE   = Path(__file__).parent.parent / 'historique.db'

C = theme.couleurs()

ETATS = {
    'marche':   {'couleur': C['marche'],   'libelle': 'En marche', 'icone': '🟢',
                 'aide': 'Transmet, aucune panne déclarée'},
    'critique': {'couleur': C['critique'], 'libelle': 'Critique',  'icone': '🔴',
                 'aide': 'Transmet, mais une panne est déclarée'},
    'arret':    {'couleur': C['arret'],    'libelle': 'En arrêt',  'icone': '🔵',
                 'aide': 'Ne transmet plus'},
}
AMBRE = C['ambre']
CENTRE, ZOOM = (34.0, 9.6), 6

FONDS = {
    'Fond de carte': (C['tuiles'], theme.ATTRIBUTION_TUILES),
    'OpenStreetMap': theme.TUILES_SECOURS,
}


# ─────────────────────────────────────────────
# DONNÉES
# ─────────────────────────────────────────────
@st.cache_data
def charger_csv() -> pd.DataFrame:
    return pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig',
                       dtype={'code_station': str})


def preparer():
    """CSV DGRE + validations humaines + demandes en attente.

    Retourne (df, bilan) — bilan vaut None si l'agent n'a pas tourné.
    """
    df = charger_csv()

    if propositions is None or not BASE.exists():
        df['etat'] = df['etat_dgre']
        df['proposition_en_attente'] = False
        df['etat_valide'] = False
        return df, None

    con = propositions.connexion(BASE)
    bilan = None

    # Recalcul UNIQUEMENT si de nouvelles données sont arrivées.
    if etats_agent is not None:
        try:
            signature = etats_agent.signature_base(con)
        except Exception:
            signature = None
        if signature and st.session_state.get('signature_agent') != signature:
            with st.spinner("Nouvelles données détectées — analyse en cours…"):
                bilan = etats_agent.synchroniser(con)
            st.session_state['signature_agent'] = signature
            st.session_state['bilan_agent'] = bilan
        else:
            bilan = st.session_state.get('bilan_agent')

    return propositions.appliquer_aux_stations(con, df), bilan


# ─────────────────────────────────────────────
# STYLE
# ─────────────────────────────────────────────
st.markdown(f"""
<style>
  .carte-etat {{
      border:1px solid var(--border); border-left-width:4px;
      border-radius:10px 10px 0 0; border-bottom:none;
      background:var(--panel); padding:14px 18px 12px 18px;
      box-shadow: var(--ombre-carte);
  }}
  .carte-etat .chiffre {{
      font-family:'JetBrains Mono',monospace; font-weight:800;
      font-size:2.1rem; line-height:1; margin-bottom:2px;
  }}
  .carte-etat .libelle {{
      font-family:'JetBrains Mono',monospace; font-size:0.8rem;
      text-transform:uppercase; letter-spacing:0.08em; color:var(--text);
  }}
  .carte-etat .aide {{
      font-family:'Space Grotesk',sans-serif; font-size:0.72rem;
      color:var(--muted); margin-top:4px;
  }}
  .carte-etat.attente {{ background: linear-gradient(180deg, {AMBRE}14, var(--panel)); }}
</style>
""", unsafe_allow_html=True)


def carte_compteur(etat: str, n: int, total: int):
    e = ETATS[etat]
    part = (100 * n / total) if total else 0
    st.markdown(f"""
      <div class="carte-etat" style="border-left-color:{e['couleur']};">
        <div class="chiffre" style="color:{e['couleur']};">{n}</div>
        <div class="libelle">{e['icone']} {e['libelle']} · {part:.0f} %</div>
        <div class="aide">{e['aide']}</div>
      </div>""", unsafe_allow_html=True)
    if st.button(f"Voir les {n} stations →", key=f"btn_{etat}",
                 use_container_width=True):
        st.session_state['etat_filtre'] = etat
        st.switch_page("vues/liste.py")


def carte_attente(n: int):
    """4ᵉ case : les questions que l'agent a posées et qui attendent réponse.

    C'est la seule case qui montre le travail de l'agent. Les trois autres ne
    font que refléter le relevé de la DGRE.
    """
    st.markdown(f"""
      <div class="carte-etat attente" style="border-left-color:{AMBRE};">
        <div class="chiffre" style="color:{AMBRE};">{n}</div>
        <div class="libelle">⏳ À vérifier</div>
        <div class="aide">Changements d'état proposés par l'agent, en attente
        d'une vérification sur le terrain</div>
      </div>""", unsafe_allow_html=True)
    if st.button(f"Traiter les {n} demandes →", key="btn_attente",
                 use_container_width=True, disabled=(n == 0)):
        st.switch_page("vues/verifications.py")


# ─────────────────────────────────────────────
# CAMEMBERT
# ─────────────────────────────────────────────
def camembert(df: pd.DataFrame):
    data = pd.DataFrame([
        {'Etat': ETATS[e]['libelle'], 'Nombre': int((df['etat'] == e).sum()),
         'Couleur': ETATS[e]['couleur']} for e in ETATS])
    graphe = (alt.Chart(data)
              .mark_arc(innerRadius=52, outerRadius=95, stroke=C['bg'], strokeWidth=2)
              .encode(theta=alt.Theta('Nombre:Q', stack=True),
                      color=alt.Color('Etat:N',
                                      scale=alt.Scale(domain=list(data['Etat']),
                                                      range=list(data['Couleur'])),
                                      legend=alt.Legend(title=None, orient='bottom',
                                                        labelColor=C['text'])),
                      tooltip=['Etat:N', 'Nombre:Q'])
              .properties(height=250, background='transparent'))
    st.altair_chart(graphe, use_container_width=True)


# ─────────────────────────────────────────────
# CARTE
# ─────────────────────────────────────────────
def bulle(r) -> str:
    e = ETATS.get(r['etat'], {'couleur': '#888', 'libelle': '—', 'icone': '⚪'})
    panne = r['type_panne'] if isinstance(r.get('type_panne'), str) and r['type_panne'] else '—'
    attente = ('<div style="background:#ffb34722;border-left:3px solid #d97706;'
               'padding:4px 8px;margin-top:7px;">⏳ Vérification demandée</div>'
               if r.get('proposition_en_attente') else '')
    valide = ('<div style="color:#7a8a86;margin-top:6px;font-size:11px;">'
              'État corrigé après vérification terrain</div>'
              if r.get('etat_valide') else '')
    return f"""
    <div style="font-family:monospace;font-size:12px;min-width:225px;color:#0e1214;">
      <div style="font-weight:700;font-size:13px;">{r['nom']}</div>
      <div style="color:#7a8a86;margin-bottom:7px;">{r['code_station']}</div>
      <div style="background:{e['couleur']}22;border-left:3px solid {e['couleur']};
                  padding:4px 8px;margin-bottom:7px;font-weight:600;">
        {e['icone']} {e['libelle']}
      </div>
      <table>
        <tr><td style="color:#7a8a86;padding-right:10px;">Gouvernorat</td><td>{r['gouvernorat']}</td></tr>
        <tr><td style="color:#7a8a86;padding-right:10px;">Type</td><td>{r['type_station']}</td></tr>
        <tr><td style="color:#7a8a86;padding-right:10px;">Capteur</td><td>{r['capteur']}</td></tr>
        <tr><td style="color:#7a8a86;padding-right:10px;">Panne</td><td>{panne}</td></tr>
      </table>{attente}{valide}
    </div>"""


def dessiner_carte(df: pd.DataFrame):
    if folium is None:
        st.error("Dépendances manquantes pour la carte.\n\n"
                 "`py -m pip install folium streamlit-folium`")
        return

    carte = folium.Map(location=CENTRE, zoom_start=ZOOM, tiles=None, control_scale=True)
    for nom, (url, attribution) in FONDS.items():
        folium.TileLayer(url, attr=attribution, name=nom,
                         overlay=False, control=True).add_to(carte)

    for etat, e in ETATS.items():
        sous = df[df['etat'] == etat]
        groupe = folium.FeatureGroup(name=f"{e['icone']} {e['libelle']} ({len(sous)})")
        for _, r in sous.iterrows():
            if pd.isna(r['lat']) or pd.isna(r['lon']):
                continue
            # Halo ambré : la couleur garde l'état officiel, le halo signale
            # qu'une question est en attente. Les deux se combinent librement.
            if r.get('proposition_en_attente'):
                folium.CircleMarker([r['lat'], r['lon']], radius=11, color=AMBRE,
                                    weight=3, opacity=0.9, fill=False).add_to(groupe)
            folium.CircleMarker(
                [r['lat'], r['lon']], radius=6, color=e['couleur'], weight=2,
                fill=True, fill_color=e['couleur'], fill_opacity=0.75,
                tooltip=(f"{r['nom']} — {e['libelle']}"
                         + (" · ⏳ à vérifier" if r.get('proposition_en_attente') else "")),
                popup=folium.Popup(bulle(r), max_width=310)).add_to(groupe)
        groupe.add_to(carte)

    folium.LayerControl(collapsed=False).add_to(carte)
    st_folium(carte, use_container_width=True, height=640, returned_objects=[])


# ─────────────────────────────────────────────
# PAGE
# ─────────────────────────────────────────────
def main():
    if not SOURCE.exists():
        st.error(f"Fichier introuvable : `{SOURCE.name}`\n\n"
                 "Lance d'abord :  `py preparer_etats.py`")
        return

    df, bilan = preparer()
    total = len(df)
    n_attente = int(df['proposition_en_attente'].sum())

    theme.hero_banner("INVENTAIRE", "DES STATIONS",
                      f"{total} stations du réseau national — état déclaré par la "
                      f"DGRE, corrigé par les vérifications terrain",
                      tag="DGRE // ÉTAT DU RÉSEAU")

    if bilan and bilan.get('closes'):
        st.info(f"♻️ {len(bilan['closes'])} demande(s) devenue(s) sans objet — "
                "le problème a disparu avant qu'on ait eu à répondre.")

    gauche, droite = st.columns([1, 1.9], gap="medium")

    with gauche:
        for etat in ETATS:
            carte_compteur(etat, int((df['etat'] == etat).sum()), total)
            st.write("")
        carte_attente(n_attente)
        st.write("")
        st.markdown("##### Répartition")
        camembert(df)

    with droite:
        dessiner_carte(df)
        st.caption("Clique sur un point pour ouvrir sa fiche. Un anneau ambré "
                   "signale une vérification en attente.")

        if etats_agent is not None and BASE.exists():
            _, bouton = st.columns([3, 1])
            with bouton:
                if st.button("🔄 Relancer l'agent", use_container_width=True):
                    st.session_state.pop('signature_agent', None)
                    st.rerun()

    st.divider()
    n_valides = int(df['etat_valide'].sum()) if 'etat_valide' in df else 0
    st.caption(
        f"Source : relevé DGRE ({total} stations)"
        + (f" · {n_valides} état(s) corrigé(s) après vérification terrain" if n_valides else "")
        + f" · {n_attente} vérification(s) en attente. "
        "L'agent ne modifie jamais un état lui-même : il propose, un humain tranche.")


main()
