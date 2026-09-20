"""
vues/fiche.py — Fiche détaillée d'une station
==============================================
Le niveau le plus fin du tableau de bord. Les autres pages répondent à
« comment va le réseau » et « lesquelles sont en panne » ; celle-ci répond à
« que se passe-t-il sur CETTE station ».

Quatre sections :

  1. État des capteurs   ce que l'agent constate, capteur par capteur, avec
                         la règle déclenchée
  2. Courbes             les 30 derniers jours de chaque capteur
  3. Observations        les règles L4 à L7 — visibles ici, mais SANS effet
                         sur l'état : une valeur inhabituelle peut être une
                         crue réelle, une rupture de moyenne peut venir d'un
                         barrage. On ne déclare pas une panne sur un peut-être.
  4. Historique          les demandes de vérification et leurs réponses

C'est le seul endroit où les règles long terme sont visibles PAR STATION ;
la page Cohérence les affiche pour tout le réseau, mélangées.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import theme

try:
    import propositions
    import etats_agent
except ImportError:
    propositions = etats_agent = None

try:
    import memoire
    from mis_parser import SENSOR_DEFAULTS
except ImportError:
    memoire = None
    SENSOR_DEFAULTS = {}

auth.barre_laterale()

SOURCE = Path(__file__).parent.parent / 'etats_stations.csv'
BASE   = Path(__file__).parent.parent / 'historique.db'

C = theme.couleurs()

ETATS = {
    'marche':   {'couleur': C['marche'],   'libelle': 'En marche', 'icone': '🟢'},
    'critique': {'couleur': C['critique'], 'libelle': 'Critique',  'icone': '🔴'},
    'arret':    {'couleur': C['arret'],    'libelle': 'En arrêt',  'icone': '🔵'},
}
ICONE_CAPTEUR = {'ok': '🟢', 'degrade': '🟡', 'muet': '⚫'}
JOURS_COURBE = 30


@st.cache_data
def charger_csv() -> pd.DataFrame:
    return pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig',
                       dtype={'code_station': str})


def _connexion():
    return propositions.connexion(BASE) if (propositions and BASE.exists()) else None


# ─────────────────────────────────────────────
# SECTIONS
# ─────────────────────────────────────────────
def entete(ligne, etat: str, en_attente: bool, valide: bool):
    e = ETATS.get(etat, {'couleur': C['muted'], 'libelle': '—', 'icone': '⚪'})
    st.markdown(f"""
    <div style="border:1px solid var(--border);border-left:4px solid {e['couleur']};
                border-radius:10px;padding:18px 22px;background:var(--panel);
                box-shadow:var(--ombre-carte);margin-bottom:18px;">
      <div style="font-family:'JetBrains Mono',monospace;font-size:1.3rem;
                  font-weight:800;color:var(--text);">{ligne['nom']}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:0.8rem;
                  color:var(--muted);margin-bottom:10px;">
        {ligne['code_station']} · {ligne['gouvernorat']} · {ligne['type_station']}
      </div>
      <span style="font-family:'JetBrains Mono',monospace;font-size:0.85rem;
                   color:{e['couleur']};font-weight:700;">
        {e['icone']} {e['libelle']}</span>
      {f'<span style="color:{C["ambre"]};margin-left:16px;font-size:0.8rem;">⏳ vérification en attente</span>' if en_attente else ''}
      {f'<span style="color:{C["muted"]};margin-left:16px;font-size:0.75rem;">état corrigé après vérification terrain</span>' if valide else ''}
    </div>""", unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Capteur déclaré", ligne.get('capteur', '—'))
    c2.metric("Transmission", ligne.get('mode_transmission', '—'))
    c3.metric("Panne déclarée", ligne.get('type_panne') or "aucune")


def etat_capteurs(con, station: str):
    st.markdown("### État des capteurs")
    if etats_agent is None or con is None:
        st.caption("Base de mesures indisponible.")
        return

    reference = etats_agent.reference_reseau(con)
    if reference is None:
        st.caption("Aucune mesure en base.")
        return

    series = pd.read_sql_query(
        "SELECT sensor, MAX(timestamp) derniere, COUNT(*) n FROM mesures "
        "WHERE station=? GROUP BY sensor", con, params=(station,),
        parse_dates=['derniere'])

    if series.empty:
        st.info("Cette station n'a jamais transmis de données à la base. "
                "Son état reste celui déclaré par la DGRE.")
        return

    for _, r in series.iterrows():
        c = etats_agent.etat_capteur(con, station, r['sensor'],
                                     r['derniere'], reference)
        regle = f" · règle **{c['regle']}**" if c['regle'] else ""
        st.markdown(f"{ICONE_CAPTEUR.get(c['etat'], '⚪')} {c['motif']}{regle}")
        st.caption(f"{int(r['n'])} mesures · dernière le {r['derniere']:%d/%m/%Y %H:%M}")


def courbes(con, station: str):
    st.markdown(f"### Les {JOURS_COURBE} derniers jours")
    if con is None:
        return

    fin = pd.read_sql_query("SELECT MAX(timestamp) f FROM mesures WHERE station=?",
                            con, params=(station,))['f'].iloc[0]
    if not fin:
        return
    depuis = (pd.Timestamp(fin) - pd.Timedelta(days=JOURS_COURBE)
              ).strftime('%Y-%m-%d %H:%M:%S')

    df = pd.read_sql_query(
        "SELECT sensor, timestamp, value FROM mesures "
        "WHERE station=? AND timestamp >= ? ORDER BY timestamp",
        con, params=(station, depuis), parse_dates=['timestamp'])

    if df.empty:
        st.caption("Aucune mesure sur la période.")
        return

    # Un graphique PAR capteur : mélanger des volts, des centimètres et des
    # millimètres sur un même axe rendrait la courbe illisible.
    for sensor, g in df.groupby('sensor'):
        base = SENSOR_DEFAULTS.get(sensor, {})
        titre = base.get('name', f"Capteur {sensor}")
        unite = base.get('unit', '')
        st.markdown(f"**{titre}** {f'({unite})' if unite else ''} — `{sensor}`")
        st.line_chart(g.set_index('timestamp')[['value']].rename(
            columns={'value': titre}), color=C['accent'], height=220)


@st.cache_data(ttl=300, show_spinner="Analyse de l'historique…")
def observations(station: str, signature: str) -> list:
    """Règles L1 à L7 pour cette station. `signature` sert de clé de cache."""
    if memoire is None:
        return []
    con = memoire.connexion()
    lignes = []
    for s, sensor in memoire.series_presentes(con):
        if s != station:
            continue
        for a in memoire.analyser_long_terme(station, sensor, con):
            lignes.append({'sensor': sensor, **a})
    return lignes


def bloc_observations(con, station: str):
    st.markdown("### Observations")
    st.caption("Signaux détectés par les règles long terme. Ils ne modifient "
               "pas l'état de la station : une valeur inhabituelle peut être "
               "une crue réelle, une rupture de moyenne peut venir d'un barrage.")

    if memoire is None or con is None:
        st.caption("Module mémoire indisponible.")
        return

    sig = etats_agent.signature_base(con) if etats_agent else station
    obs = observations(station, sig)

    if not obs:
        st.success("Aucune observation particulière sur cette station.")
        return

    icone = {'high': '🔴', 'medium': '🟡', 'low': '🔵'}
    for a in obs:
        nom = SENSOR_DEFAULTS.get(a['sensor'], {}).get('name', a['sensor'])
        st.markdown(f"{icone.get(a['severity'], '❔')} **{nom}** — {a['description']}")


def historique(con, station: str):
    st.markdown("### Historique des vérifications")
    if propositions is None or con is None:
        return

    journal = propositions.journal(con, station=station)
    if not journal:
        st.caption("Aucune demande de vérification n'a jamais été ouverte "
                   "pour cette station.")
        return

    st.dataframe(pd.DataFrame([{
        'Ouverte le': (p.get('cree_le') or '')[:16],
        'Proposé':    f"{p['etat_actuel']} → {p['etat_propose']}",
        'Règles':     p.get('regles') or '—',
        'Réponse':    p['libelle_statut'],
        'Par':        p.get('reponse_par') or '',
        'Note':       p.get('commentaire') or '',
    } for p in journal]), use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
# PAGE
# ─────────────────────────────────────────────
def main():
    if not SOURCE.exists():
        st.error(f"Fichier introuvable : `{SOURCE.name}`")
        return

    theme.hero_banner("FICHE", "STATION",
                      "Détail complet d'une station : capteurs, courbes, "
                      "observations et historique",
                      tag="DGRE // DÉTAIL STATION")

    df = charger_csv()
    con = _connexion()
    if con is not None:
        df = propositions.appliquer_aux_stations(con, df)
    else:
        df['etat'] = df['etat_dgre']
        df['proposition_en_attente'] = False
        df['etat_valide'] = False

    libelles = {f"{r['code_station']} — {r['nom']} ({r['gouvernorat']})":
                r['code_station'] for _, r in df.iterrows()}

    demandee = st.session_state.get('station_fiche')
    defaut = 0
    if demandee in df['code_station'].values:
        defaut = list(libelles.values()).index(demandee)

    choix = st.selectbox("Station", list(libelles), index=defaut)
    station = libelles[choix]
    st.session_state['station_fiche'] = station

    ligne = df[df['code_station'] == station].iloc[0]

    entete(ligne, ligne['etat'], bool(ligne['proposition_en_attente']),
           bool(ligne['etat_valide']))

    if ligne['proposition_en_attente'] and st.button("⏳ Traiter la demande"):
        st.switch_page("vues/verifications.py")

    st.divider()
    etat_capteurs(con, station)
    st.divider()
    courbes(con, station)
    st.divider()
    bloc_observations(con, station)
    st.divider()
    historique(con, station)

    st.divider()
    g, d = st.columns(2)
    with g:
        if st.button("← Inventaire", use_container_width=True):
            st.switch_page("vues/inventaire.py")
    with d:
        if st.button("Liste des stations →", use_container_width=True):
            st.switch_page("vues/liste.py")


main()
