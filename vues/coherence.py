"""
vues/coherence.py — Cohérence des données : dépôt et analyse des fichiers .MIS
==============================================================================
Reprend le contenu de app.py, adapté au site multipage.

Quatre différences avec app.py, toutes imposées par le passage en multipage :

  1. Pas de st.set_page_config : site.py l'appelle déjà, et Streamlit n'en
     accepte qu'un seul par application.
  2. Le code est dans des fonctions : st.stop() couperait aussi la barre
     latérale, on utilise donc « return » pour sortir proprement.
  3. Les résultats sont rangés dans st.session_state : ils survivent ainsi
     à un aller-retour vers l'accueil.
  4. L'analyse long terme est mise en cache (5 min) : elle balaie tout
     l'historique et ne peut pas être relancée à chaque clic.

app.py reste utilisable tel quel, il n'a pas été modifié.
"""

import tempfile

import pandas as pd
import streamlit as st

import auth
import theme
import memoire
from mis_parser import (parse_mis, analyse_serie, SENSOR_DEFAULTS, BATTERY,
                        STATION_NAMES, STATION_INFO, CONFIG_CSV)

auth.barre_laterale()

SEVERITY_ICON = {'high': '🔴', 'medium': '🟡', 'low': '🔵'}
SEVERITY_BOX  = {'high': st.error, 'medium': st.warning, 'low': st.info}
ORDRE_GRAVITE = {'🔴': 0, '🟡': 1, '🔵': 2}

CLE_RESULTATS = 'coherence_resultats'   # analyses conservées entre les pages

con = memoire.connexion()


# ─────────────────────────────────────────────
# EN-TÊTE
# ─────────────────────────────────────────────
try:
    from theme import hero_banner
    hero_banner("COHÉRENCE", "DES DONNÉES",
                "Dépôt de fichiers .MIS — détection des valeurs aberrantes, "
                "ruptures et données manquantes",
                tag="DGRE // FLUX DE DONNÉES")
except ImportError:
    st.title("📊 Cohérence des données")
    st.caption("Dépose un ou plusieurs fichiers .MIS : ils sont fusionnés puis "
               "analysés station par station, avec les seuils propres à chacune.")


# ─────────────────────────────────────────────
# BARRE LATÉRALE : état de la configuration
# ─────────────────────────────────────────────
def panneau_lateral():
    st.sidebar.divider()
    st.sidebar.header("⚙️ Configuration")
    if STATION_NAMES:
        st.sidebar.success(f"📋 {len(STATION_NAMES)} stations chargées depuis "
                           f"`{CONFIG_CSV.name}`")
        st.sidebar.caption("Pour modifier les seuils min/max : ouvre "
                           "`config_stations.csv` dans Excel, change les valeurs, "
                           "enregistre (séparateur ;) puis relance l'analyse.")
    else:
        st.sidebar.warning("`config_stations.csv` introuvable — seuils par défaut.")

    st.sidebar.subheader("🔋 Règles batterie (0002)")
    st.sidebar.markdown(
        f"- Normal ≈ **{BATTERY['nominal']} V**\n"
        f"- Décharge (intervenir) : **≤ {BATTERY['alerte']} V**\n"
        f"- Point de non-retour : **≤ {BATTERY['non_retour']} V**\n"
        f"- Surcharge suspecte : **> {BATTERY['surcharge']} V**")

    st.sidebar.subheader("🧠 Mémoire long terme")
    s = memoire.stats_memoire(con)
    if s['mesures']:
        st.sidebar.markdown(f"- **{s['mesures']}** mesures accumulées\n"
                            f"- **{s['stations']}** station(s)\n"
                            f"- du {s['debut']} au {s['fin']}")
    else:
        st.sidebar.caption("Mémoire vide — elle se remplit à chaque dépôt.")

    st.sidebar.subheader("📡 Capteurs connus")
    st.sidebar.markdown("\n".join(
        f"- `{code}` : {cfg['name']}" for code, cfg in sorted(SENSOR_DEFAULTS.items())))


# ─────────────────────────────────────────────
# ANALYSE LONG TERME — mise en cache
# ─────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner="Analyse de l'historique complet…")
def analyse_long_terme_cachee(signature: str) -> pd.DataFrame:
    """Règles L1–L7 sur toute la mémoire.

    L'argument `signature` n'entre pas dans le calcul : il sert uniquement de
    clé de cache. Quand la mémoire change (nouvelles mesures déposées), la
    signature change et le cache se renouvelle de lui-même.
    """
    c = memoire.connexion()
    lignes = []

    for station, sensor in memoire.series_presentes(c):
        for a in memoire.analyser_long_terme(station, sensor, c):
            lignes.append({
                'Station':     station,
                'Nom station': STATION_NAMES.get(station, '— hors liste —'),
                'Capteur':     f"{sensor} ({SENSOR_DEFAULTS.get(sensor, {}).get('name', '?')})",
                'Date':        pd.Timestamp(a['timestamp']).strftime('%Y-%m-%d %H:%M'),
                'Gravité':     SEVERITY_ICON.get(a['severity'], '❔'),
                'Type':        a['type'],
                'Description': a['description'],
            })

    for a in memoire.detecter_silences(c):
        lignes.append({
            'Station':     a['station'],
            'Nom station': STATION_NAMES.get(a['station'], '— hors liste —'),
            'Capteur':     f"{a['sensor']} ({SENSOR_DEFAULTS.get(a['sensor'], {}).get('name', '?')})",
            'Date':        pd.Timestamp(a['timestamp']).strftime('%Y-%m-%d %H:%M'),
            'Gravité':     SEVERITY_ICON.get(a['severity'], '❔'),
            'Type':        a['type'],
            'Description': a['description'],
        })

    if not lignes:
        return pd.DataFrame()
    return (pd.DataFrame(lignes)
            .sort_values(by=['Gravité', 'Station'],
                         key=lambda s: s.map(ORDRE_GRAVITE) if s.name == 'Gravité' else s)
            .reset_index(drop=True))


# ─────────────────────────────────────────────
# LECTURE + ANALYSE des fichiers déposés
# ─────────────────────────────────────────────
def analyser_fichiers(fichiers):
    """Retourne un dictionnaire de résultats, ou None si rien d'exploitable."""
    blocs, vides = [], []
    for up in fichiers:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".MIS") as tmp:
            tmp.write(up.getvalue())
            chemin = tmp.name
        lus = parse_mis(chemin)
        if lus:
            blocs.extend(lus)
        else:
            vides.append(up.name)

    if not blocs:
        return None

    base = pd.concat(blocs, ignore_index=True)
    avant = len(base)
    base = (base.drop_duplicates(subset=['station', 'sensor', 'timestamp', 'value'])
                .sort_values(['station', 'sensor', 'timestamp'])
                .reset_index(drop=True))

    resultats = []
    for (station, sensor), g in base.groupby(['station', 'sensor']):
        g, anomalies = analyse_serie(g.reset_index(drop=True))
        resultats.append({'station': station, 'sensor': sensor,
                          'df': g, 'anomalies': anomalies})

    nouvelles = sum(memoire.ajouter_mesures(r['df'], con) for r in resultats)

    return {'noms_fichiers':   [f.name for f in fichiers],
            'base':            base,
            'resultats':       resultats,
            'retransmissions': avant - len(base),
            'fichiers_vides':  vides,
            'nouvelles':       nouvelles}


def tableau_recapitulatif(resultats):
    def date_detection(df, a):
        idx = [i for i in (a.get('indices') or []) if 0 <= i < len(df)]
        return df['timestamp'].iloc[min(idx)] if idx else df['timestamp'].min()

    lignes = []
    for r in resultats:
        for a in r['anomalies']:
            lignes.append({
                'Station':        r['station'],
                'Nom station':    STATION_NAMES.get(r['station'], '— hors liste —'),
                'Capteur':        f"{r['sensor']} ({SENSOR_DEFAULTS.get(r['sensor'], {}).get('name', '?')})",
                'Date détection': date_detection(r['df'], a).strftime('%Y-%m-%d %H:%M'),
                'Gravité':        SEVERITY_ICON.get(a['severity'], '❔'),
                'Type':           a['type'],
                'Description':    a['description'],
            })
    recap = pd.DataFrame(lignes)
    if recap.empty:
        return recap
    return recap.sort_values(
        by=['Station', 'Date détection', 'Gravité'],
        key=lambda s: s.map(ORDRE_GRAVITE) if s.name == 'Gravité' else s
    ).reset_index(drop=True)


# ─────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────
def vue_ensemble(res, recap):
    st.divider()
    st.header("📊 Vue d'ensemble")

    n_crit = int((recap['Gravité'] == '🔴').sum()) if not recap.empty else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Fichiers déposés",       len(res['noms_fichiers']))
    c2.metric("Mesures fusionnées",     len(res['base']))
    c3.metric("Séries station-capteur", len(res['resultats']))
    c4.metric("Anomalies",              len(recap))
    c5.metric("Dont critiques 🔴",      n_crit)

    if res['retransmissions']:
        st.caption(f"ℹ️ {res['retransmissions']} retransmission(s) identique(s) fusionnée(s).")
    st.caption(f"🧠 {res['nouvelles']} nouvelle(s) mesure(s) ajoutée(s) à la mémoire "
               f"— total accumulé : {memoire.stats_memoire(con)['mesures']} mesures.")
    if res['fichiers_vides']:
        st.warning("Fichier(s) sans données exploitables : " + ", ".join(res['fichiers_vides']))

    hors_liste = sorted({r['station'] for r in res['resultats']
                         if r['station'] not in STATION_NAMES})
    if STATION_NAMES and hors_liste:
        st.warning("⚠️ Station(s) absente(s) de config_stations.csv (seuils par "
                   "défaut appliqués) : " + ", ".join(hors_liste))

    if recap.empty:
        st.success("✅ Aucune anomalie détectée sur l'ensemble des données.")
        return

    st.download_button("💾 Rapport global d'anomalies (CSV)",
                       data=recap.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name="rapport_anomalies.csv", mime="text/csv",
                       key="dl_rapport_global")

    st.subheader("Anomalies classées par station")
    for station, sub in recap.groupby('Station', sort=True):
        nom  = STATION_NAMES.get(station, 'hors liste')
        gouv = STATION_INFO.get(station, {}).get('gouvernorat', '')
        nc   = int((sub['Gravité'] == '🔴').sum())
        titre = f"#### 🏢 {station} — {nom}"
        if isinstance(gouv, str) and gouv.strip():
            titre += f" ({gouv})"
        st.markdown(titre + f" · {len(sub)} anomalie(s), dont {nc} critique(s) 🔴")
        st.dataframe(sub.drop(columns=['Station', 'Nom station']),
                     use_container_width=True, hide_index=True)


def vue_long_terme():
    st.divider()
    st.header("🧠 Anomalies long terme (mémoire)")
    st.caption("Règles appliquées à l'HISTORIQUE COMPLET de chaque station — toutes "
               "les données jamais déposées, pas seulement les fichiers du jour : "
               "flatline · dérive batterie (avec date prévue du point de non-retour) "
               "· station muette · valeur inhabituelle · rupture de moyenne.")

    s = memoire.stats_memoire(con)
    if not s['mesures']:
        st.info("La mémoire est vide — dépose des fichiers pour la remplir.")
        return

    signature = f"{s['mesures']}|{s['fin']}"

    _, droite = st.columns([4, 1])
    with droite:
        if st.button("🔄 Recalculer", use_container_width=True):
            analyse_long_terme_cachee.clear()
            st.rerun()

    lt = analyse_long_terme_cachee(signature)

    if lt.empty:
        st.success("✅ Aucune anomalie long terme — l'historique est peut-être "
                   "encore trop court.")
        return

    st.dataframe(lt, use_container_width=True, hide_index=True)
    st.download_button("💾 Rapport long terme (CSV)",
                       data=lt.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name="rapport_long_terme.csv", mime="text/csv",
                       key="dl_rapport_lt")


def vue_detail(resultats):
    st.divider()
    st.header("🔍 Détail par station et capteur")

    for r in resultats:
        df, anomalies = r['df'], r['anomalies']
        nom  = STATION_NAMES.get(r['station'], 'hors liste')
        name = SENSOR_DEFAULTS.get(r['sensor'], {}).get('name', 'capteur inconnu')
        titre = (f"🏢 {r['station']} — {nom} · Capteur {r['sensor']} ({name}) "
                 f"· {len(anomalies)} anomalie(s)")

        with st.expander(titre, expanded=(len(resultats) <= 2)):
            diffs = df['timestamp'].diff().dropna().dt.total_seconds()
            step  = f"{int(diffs.mode()[0])} s" if len(diffs) else "—"
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Mesures", len(df))
            c2.metric("Début", df['timestamp'].min().strftime('%d/%m %H:%M'))
            c3.metric("Fin",   df['timestamp'].max().strftime('%d/%m %H:%M'))
            c4.metric("Pas de temps", step)

            if anomalies:
                for a in anomalies:
                    icon = SEVERITY_ICON.get(a['severity'], '❔')
                    show = SEVERITY_BOX.get(a['severity'], st.info)
                    texte = f"{icon} **{a['type']}** — {a['description']}"
                    details = a.get('details', [])
                    if details:
                        texte += "\n\n" + "\n".join(f"- {d}" for d in details[:10])
                    show(texte)
            else:
                st.success("✅ Aucune anomalie détectée")

            # Couleurs imposées : les teintes par défaut de Streamlit sont
            # trop pâles sur le fond crème du thème.
            pal_g = theme.couleurs()
            st.line_chart(df.set_index('timestamp')[['value_raw', 'value']]
                            .rename(columns={'value_raw': 'Valeur brute',
                                             'value': 'Valeur corrigée'}),
                          color=[pal_g['muted'], pal_g['accent']], height=260)

            cols = ['timestamp', 'value_raw', 'value', 'corrected']
            if 'code' in df.columns and df['code'].notna().any():
                cols.append('code')
            affiche = df[cols].rename(columns={'timestamp': 'Horodatage',
                                               'value_raw': 'Valeur brute',
                                               'value':     'Valeur corrigée',
                                               'corrected': 'Corrigée ?',
                                               'code':      'Code capteur'})

            # Le surlignage des valeurs corrigées suit le mode d'affichage :
            # rose pâle sur fond clair, rouge sombre sur fond sombre.
            pal = theme.couleurs()
            style_corr = (f"background-color: {pal['surlignage_fond']}; "
                          f"color: {pal['surlignage_texte']};")

            def surligner(row):
                return [style_corr if row['Corrigée ?'] else ''] * len(row)

            st.dataframe(affiche.style.apply(surligner, axis=1),
                         use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
# PROGRAMME DE LA PAGE
# ─────────────────────────────────────────────
def main():
    panneau_lateral()

    fichiers = st.file_uploader("Fichier(s) MIS à analyser",
                                type=["mis", "txt"], accept_multiple_files=True)

    if fichiers:
        res = analyser_fichiers(fichiers)
        if res is None:
            st.error("Aucune donnée exploitable dans les fichiers déposés.")
            return
        st.session_state[CLE_RESULTATS] = res

    res = st.session_state.get(CLE_RESULTATS)

    if res is None:
        st.info("⬆️ Dépose un ou plusieurs fichiers .MIS pour lancer l'analyse.")
        # La mémoire existe déjà : ses règles long terme sont consultables
        # sans rien déposer.
        vue_long_terme()
        return

    if not fichiers:
        st.caption(f"📎 Analyse conservée : {', '.join(res['noms_fichiers'])} "
                   "— redépose des fichiers pour la remplacer.")

    recap = tableau_recapitulatif(res['resultats'])
    vue_ensemble(res, recap)

    donnees = pd.concat([r['df'] for r in res['resultats']], ignore_index=True)
    st.download_button("💾 Toutes les données corrigées (CSV)",
                       data=donnees.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name="donnees_corrigees.csv", mime="text/csv",
                       key="dl_donnees_globales")

    vue_long_terme()
    vue_detail(res['resultats'])


main()
