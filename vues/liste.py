"""
vues/liste.py — Liste des stations d'un état donné
===================================================
Page d'atterrissage des compteurs de l'Inventaire. L'état à afficher est
transmis par st.session_state['etat_filtre'], posé au clic sur un compteur.

La colonne « type de panne » n'apparaît que pour les états qui en ont une :
l'afficher sur les stations en marche donnerait une colonne vide sur toute
la hauteur du tableau.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import theme

try:
    import propositions
except ImportError:
    propositions = None

auth.barre_laterale()

C = theme.couleurs()
BASE = Path(__file__).parent.parent / 'historique.db'

SOURCE = Path(__file__).parent.parent / 'etats_stations.csv'

ETATS = {
    'marche':   {'couleur': C['marche'],   'libelle': 'En marche', 'icone': '🟢'},
    'critique': {'couleur': C['critique'], 'libelle': 'Critique',  'icone': '🔴'},
    'arret':    {'couleur': C['arret'],    'libelle': 'En arrêt',  'icone': '🔵'},
}

COLONNES = {
    'code_station':      'Code',
    'nom':               'Nom',
    'gouvernorat':       'Gouvernorat',
    'type_station':      'Type',
    'capteur':           'Capteur',
    'mode_transmission': 'Transmission',
    'type_panne':        'Type de panne',
}


@st.cache_data
def charger_csv() -> pd.DataFrame:
    return pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig',
                       dtype={'code_station': str})


def charger() -> pd.DataFrame:
    """État DGRE, corrigé par les validations humaines — comme l'Inventaire.

    Les deux pages doivent afficher la même chose : une station validée
    « en arrêt » ne doit pas rester dans la liste des stations en marche.
    """
    df = charger_csv()
    if propositions is None or not BASE.exists():
        df['etat'] = df['etat_dgre']
        df['proposition_en_attente'] = False
        return df
    return propositions.appliquer_aux_stations(propositions.connexion(BASE), df)


def main():
    if not SOURCE.exists():
        st.error(f"Fichier introuvable : `{SOURCE.name}`")
        return

    df = charger()
    etat = st.session_state.get('etat_filtre', 'marche')
    if etat not in ETATS:
        etat = 'marche'

    # ── Bascule d'un état à l'autre sans repasser par l'Inventaire ──
    libelles = {f"{e['icone']} {e['libelle']} "
                f"({int((df['etat'] == k).sum())})": k
                for k, e in ETATS.items()}
    courant = [lab for lab, k in libelles.items() if k == etat][0]

    choix = st.radio("État", list(libelles), horizontal=True,
                     index=list(libelles).index(courant), label_visibility="collapsed")
    if libelles[choix] != etat:
        st.session_state['etat_filtre'] = libelles[choix]
        st.rerun()

    e = ETATS[etat]
    sous = df[df['etat'] == etat].copy()

    st.markdown(
        f"<h3 style='color:{e['couleur']};font-family:JetBrains Mono,monospace;'>"
        f"{e['icone']} {e['libelle']} — {len(sous)} stations</h3>",
        unsafe_allow_html=True)

    # ── Filtres ──
    f1, f2 = st.columns([1, 1])
    with f1:
        gouvs = ['Tous'] + sorted(sous['gouvernorat'].dropna().unique())
        gouv = st.selectbox("Gouvernorat", gouvs)
    with f2:
        recherche = st.text_input("Rechercher (nom ou code)", "")

    if gouv != 'Tous':
        sous = sous[sous['gouvernorat'] == gouv]
    if recherche.strip():
        m = recherche.strip().lower()
        sous = sous[sous['nom'].str.lower().str.contains(m, na=False)
                    | sous['code_station'].str.contains(m, na=False)]

    if sous.empty:
        st.info("Aucune station ne correspond à ces critères.")
        return

    # ── Tableau ──
    # La colonne « type de panne » n'a de sens que là où une panne existe.
    colonnes = list(COLONNES)
    if etat == 'marche' or sous['type_panne'].fillna('').eq('').all():
        colonnes.remove('type_panne')

    tableau = (sous[colonnes].rename(columns=COLONNES)
                             .sort_values(['Gouvernorat', 'Nom'])
                             .reset_index(drop=True))

    st.dataframe(tableau, use_container_width=True, hide_index=True)

    # ── Récapitulatif des pannes ──
    if 'type_panne' in colonnes:
        pannes = (sous['type_panne'].fillna('').str.split(' + ')
                       .explode().str.strip())
        pannes = pannes[pannes != '']
        if len(pannes):
            st.markdown("##### Pannes les plus fréquentes")
            compte = (pannes.value_counts().rename_axis('Panne')
                            .reset_index(name='Stations'))
            st.dataframe(compte, use_container_width=True, hide_index=True)

    # Passerelle vers le niveau de détail : la liste dit « lesquelles »,
    # la fiche dit « que se passe-t-il sur celle-ci ».
    st.markdown("##### Ouvrir une fiche")
    f1, f2 = st.columns([3, 1])
    with f1:
        cible = st.selectbox(
            "Station", [f"{c} — {n}" for c, n in
                        zip(sous['code_station'], sous['nom'])],
            label_visibility="collapsed")
    with f2:
        if st.button("Ouvrir →", use_container_width=True):
            st.session_state['station_fiche'] = cible.split(' — ')[0]
            st.switch_page("vues/fiche.py")

    st.divider()
    b1, b2 = st.columns([1, 3])
    with b1:
        if st.button("← Inventaire", use_container_width=True):
            st.switch_page("vues/inventaire.py")
    with b2:
        st.download_button(
            f"💾 Exporter cette liste ({len(tableau)} stations)",
            data=tableau.to_csv(index=False, sep=';').encode('utf-8-sig'),
            file_name=f"stations_{etat}.csv", mime="text/csv")


main()
