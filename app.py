"""
Interface web locale — Détection d'anomalies hydro-pluviométriques (DGRE)
=========================================================================
Fichiers requis dans le MÊME dossier :
  - mis_parser.py
  - config_stations.csv  (noms + seuils par station, éditable dans Excel)

Lancer avec :
    python -m streamlit run app.py
"""

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from mis_parser import (parse_mis, analyse_serie, SENSOR_DEFAULTS, BATTERY,
                        STATION_NAMES, STATION_INFO, CONFIG_CSV)
import memoire

SEVERITY_ICON = {'high': '🔴', 'medium': '🟡', 'low': '🔵'}
SEVERITY_BOX  = {'high': st.error, 'medium': st.warning, 'low': st.info}

con = memoire.connexion()   # ouvre (ou crée) historique.db


# ─────────────────────────────────────────────
# Mise en page générale
# ─────────────────────────────────────────────
st.set_page_config(page_title="Détection d'anomalies — Hydras 3",
                   page_icon="💧", layout="wide")

st.title("💧 Détection d'anomalies hydro-pluviométriques")
st.caption("Dépose un ou plusieurs fichiers .MIS : ils sont fusionnés puis analysés "
           "station par station, avec les seuils propres à chaque station.")

# ─────────────────────────────────────────────
# Barre latérale : état de la configuration
# ─────────────────────────────────────────────
st.sidebar.header("⚙️ Configuration")
if STATION_NAMES:
    st.sidebar.success(f"📋 {len(STATION_NAMES)} stations chargées depuis "
                       f"`{CONFIG_CSV.name}`")
    st.sidebar.caption("Pour modifier les seuils min/max d'une station : ouvre "
                       "`config_stations.csv` dans Excel, change les valeurs, "
                       "enregistre (séparateur ;) puis relance l'analyse.")
else:
    st.sidebar.warning("`config_stations.csv` introuvable — seuils par défaut "
                       "appliqués à toutes les stations.")

st.sidebar.subheader("🔋 Règles batterie (0002)")
st.sidebar.markdown(
    f"- Normal ≈ **{BATTERY['nominal']} V**\n"
    f"- Décharge (intervenir) : **≤ {BATTERY['alerte']} V**\n"
    f"- Point de non-retour : **≤ {BATTERY['non_retour']} V**\n"
    f"- Surcharge suspecte : **> {BATTERY['surcharge']} V**"
)

st.sidebar.subheader("🧠 Mémoire long terme")
_s = memoire.stats_memoire(con)
if _s['mesures']:
    st.sidebar.markdown(f"- **{_s['mesures']}** mesures accumulées\n"
                        f"- **{_s['stations']}** station(s)\n"
                        f"- du {_s['debut']} au {_s['fin']}")
else:
    st.sidebar.caption("Mémoire vide — elle se remplit à chaque dépôt de fichiers.")
st.sidebar.caption("Fichier : `historique.db` — le supprimer = repartir de zéro.")

st.sidebar.subheader("📡 Capteurs connus")
st.sidebar.markdown("\n".join(
    f"- `{code}` : {cfg['name']}" for code, cfg in sorted(SENSOR_DEFAULTS.items())
))

# ─────────────────────────────────────────────
# Zone de dépôt (plusieurs fichiers acceptés)
# ─────────────────────────────────────────────
files = st.file_uploader("Fichier(s) MIS à analyser",
                         type=["mis", "txt"], accept_multiple_files=True)

if not files:
    st.info("⬆️ Dépose un ou plusieurs fichiers .MIS pour lancer l'analyse.")
    st.stop()

# ─────────────────────────────────────────────
# 1) LECTURE de tous les fichiers → une seule base
# ─────────────────────────────────────────────
all_dfs, fichiers_vides = [], []
for up in files:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".MIS") as tmp:
        tmp.write(up.getvalue())
        tmp_path = tmp.name
    blocks = parse_mis(tmp_path)
    if blocks:
        all_dfs.extend(blocks)
    else:
        fichiers_vides.append(up.name)

if not all_dfs:
    st.error("Aucune donnée exploitable dans les fichiers déposés.")
    st.stop()

base = pd.concat(all_dfs, ignore_index=True)
avant = len(base)
base = (base.drop_duplicates(subset=['station', 'sensor', 'timestamp', 'value'])
            .sort_values(['station', 'sensor', 'timestamp'])
            .reset_index(drop=True))
retransmissions = avant - len(base)

# ─────────────────────────────────────────────
# 2) ANALYSE par station / capteur (séries fusionnées)
# ─────────────────────────────────────────────
resultats = []
for (station, sensor), g in base.groupby(['station', 'sensor']):
    g, anomalies = analyse_serie(g.reset_index(drop=True))
    resultats.append({'station': station, 'sensor': sensor,
                      'df': g, 'anomalies': anomalies})

# ─────────────────────────────────────────────
# 2bis) MÉMOIRE : ajout des nouvelles mesures à l'historique
#       (les doublons sont ignorés automatiquement)
# ─────────────────────────────────────────────
nouvelles_mesures = sum(memoire.ajouter_mesures(r['df'], con) for r in resultats)

# ─────────────────────────────────────────────
# 3) VUE D'ENSEMBLE consolidée
# ─────────────────────────────────────────────
st.divider()
st.header("📊 Vue d'ensemble")

def _date_detection(df, a):
    idx = [i for i in (a.get('indices') or []) if 0 <= i < len(df)]
    return df['timestamp'].iloc[min(idx)] if idx else df['timestamp'].min()

lignes = []
for r in resultats:
    for a in r['anomalies']:
        lignes.append({
            'Station':        r['station'],
            'Nom station':    STATION_NAMES.get(r['station'], '— hors liste —'),
            'Capteur':        f"{r['sensor']} ({SENSOR_DEFAULTS.get(r['sensor'], {}).get('name', '?')})",
            'Date détection': _date_detection(r['df'], a).strftime('%Y-%m-%d %H:%M'),
            'Gravité':        SEVERITY_ICON.get(a['severity'], '❔'),
            'Type':           a['type'],
            'Description':    a['description'],
        })

ordre_gravite = {'🔴': 0, '🟡': 1, '🔵': 2}
recap = pd.DataFrame(lignes)
if not recap.empty:
    recap = recap.sort_values(
        by=['Station', 'Date détection', 'Gravité'],
        key=lambda s: s.map(ordre_gravite) if s.name == 'Gravité' else s
    ).reset_index(drop=True)

n_critiques = int((recap['Gravité'] == '🔴').sum()) if not recap.empty else 0
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Fichiers déposés", len(files))
c2.metric("Mesures fusionnées", len(base))
c3.metric("Séries station-capteur", len(resultats))
c4.metric("Anomalies", len(recap))
c5.metric("Dont critiques 🔴", n_critiques)

if retransmissions:
    st.caption(f"ℹ️ {retransmissions} retransmission(s) identique(s) fusionnée(s).")
st.caption(f"🧠 {nouvelles_mesures} nouvelle(s) mesure(s) ajoutée(s) à la mémoire "
           f"— total accumulé : {memoire.stats_memoire(con)['mesures']} mesures.")
if fichiers_vides:
    st.warning("Fichier(s) sans données exploitables : " + ", ".join(fichiers_vides))

stations_hors_liste = sorted({r['station'] for r in resultats
                              if r['station'] not in STATION_NAMES})
if STATION_NAMES and stations_hors_liste:
    st.warning("⚠️ Station(s) absente(s) de config_stations.csv (seuils par défaut "
               "appliqués) : " + ", ".join(stations_hors_liste))

if recap.empty:
    st.success("✅ Aucune anomalie détectée sur l'ensemble des données.")
else:
    st.download_button("💾 Télécharger le rapport global d'anomalies (CSV)",
                       data=recap.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name="rapport_anomalies.csv",
                       mime="text/csv",
                       key="dl_rapport_global")

    # ── Un tableau PAR STATION ──
    st.subheader("Anomalies classées par station")
    for station, sub in recap.groupby('Station', sort=True):
        nom    = STATION_NAMES.get(station, 'hors liste')
        gouv   = STATION_INFO.get(station, {}).get('gouvernorat', '')
        n_crit = int((sub['Gravité'] == '🔴').sum())
        titre  = f"#### 🏢 {station} — {nom}"
        if isinstance(gouv, str) and gouv.strip():
            titre += f" ({gouv})"
        st.markdown(titre + f" · {len(sub)} anomalie(s), dont {n_crit} critique(s) 🔴")
        st.dataframe(sub.drop(columns=['Station', 'Nom station']),
                     use_container_width=True, hide_index=True)

# Téléchargement unique : toutes les données corrigées consolidées
donnees_corrigees = pd.concat([r['df'] for r in resultats], ignore_index=True)
st.download_button("💾 Télécharger toutes les données corrigées (CSV)",
                   data=donnees_corrigees.to_csv(index=False, sep=';').encode('utf-8-sig'),
                   file_name="donnees_corrigees.csv",
                   mime="text/csv",
                   key="dl_donnees_globales")

# ─────────────────────────────────────────────
# 3bis) ANOMALIES LONG TERME (sur toute la mémoire)
# ─────────────────────────────────────────────
st.divider()
st.header("🧠 Anomalies long terme (mémoire)")
st.caption("Règles appliquées à l'HISTORIQUE COMPLET de chaque station — toutes les "
           "données jamais déposées, pas seulement les fichiers du jour : "
           "flatline ≥ 24 h · dérive batterie (avec date prévue du point de non-retour) "
           "· station muette · valeur inhabituelle vs historique · "
           "pluie sans réponse de la rivière (croisement des deux capteurs d'une "
           "même station).")

lt_lignes = []
for _station, _sensor in memoire.series_presentes(con):
    for a in memoire.analyser_long_terme(_station, _sensor, con):
        lt_lignes.append({
            'Station':     _station,
            'Nom station': STATION_NAMES.get(_station, '— hors liste —'),
            'Capteur':     f"{_sensor} ({SENSOR_DEFAULTS.get(_sensor, {}).get('name', '?')})",
            'Date':        pd.Timestamp(a['timestamp']).strftime('%Y-%m-%d %H:%M'),
            'Gravité':     SEVERITY_ICON.get(a['severity'], '❔'),
            'Type':        a['type'],
            'Description': a['description'],
        })
for a in memoire.detecter_silences(con):
    lt_lignes.append({
        'Station':     a['station'],
        'Nom station': STATION_NAMES.get(a['station'], '— hors liste —'),
        'Capteur':     f"{a['sensor']} ({SENSOR_DEFAULTS.get(a['sensor'], {}).get('name', '?')})",
        'Date':        pd.Timestamp(a['timestamp']).strftime('%Y-%m-%d %H:%M'),
        'Gravité':     SEVERITY_ICON.get(a['severity'], '❔'),
        'Type':        a['type'],
        'Description': a['description'],
    })

# L7 : croisement des DEUX capteurs d'une même station (pluie × cote)
for _station in sorted({s for s, _ in memoire.series_presentes(con)}):
    for a in memoire.analyser_croisement_pluie_cote(_station, con):
        lt_lignes.append({
            'Station':     _station,
            'Nom station': STATION_NAMES.get(_station, '— hors liste —'),
            'Capteur':     '0006 × 0001 (pluie × cote)',
            'Date':        pd.Timestamp(a['timestamp']).strftime('%Y-%m-%d %H:%M'),
            'Gravité':     SEVERITY_ICON.get(a['severity'], '❔'),
            'Type':        a['type'],
            'Description': a['description'],
        })

if lt_lignes:
    lt = pd.DataFrame(lt_lignes).sort_values(
        by=['Gravité', 'Station'],
        key=lambda s: s.map(ordre_gravite) if s.name == 'Gravité' else s
    ).reset_index(drop=True)
    st.dataframe(lt, use_container_width=True, hide_index=True)
    st.download_button("💾 Télécharger le rapport long terme (CSV)",
                       data=lt.to_csv(index=False, sep=';').encode('utf-8-sig'),
                       file_name="rapport_long_terme.csv",
                       mime="text/csv",
                       key="dl_rapport_lt")
else:
    st.success("✅ Aucune anomalie long terme — l'historique est peut-être encore trop court.")

# ─────────────────────────────────────────────
# 3ter) RÈGLES INTER-STATIONS (I1, I2)
#       Comparent les stations ENTRE ELLES : elles répondent à des questions
#       qu'une station seule ne peut pas trancher.
# ─────────────────────────────────────────────
st.divider()
st.header("🔗 Anomalies inter-stations")
st.caption("Ces règles comparent les stations les unes aux autres. "
           "**I1 — faux zéro pluviométrique** : une station à 0 mm pendant que "
           "ses voisines proches, de même altitude, mesurent de la pluie. "
           "**I2 — cohérence de crue amont→aval** : une crue vue à l'amont "
           "doit se retrouver à l'aval après le temps de propagation. "
           "Les paires séparées par un barrage ne sont PAS jugées : le débit "
           "aval y dépend des lâchers, pas de la pluie.")

_fin_reseau = memoire.stats_memoire(con)['fin']
if not _fin_reseau:
    st.info("Mémoire vide — les règles inter-stations n'ont rien à comparer.")
else:
    _jours = st.slider("Fenêtre d'analyse (jours avant la dernière donnée reçue)",
                       min_value=7, max_value=180, value=30, step=7,
                       help="Les règles inter-stations ne sont appliquées que "
                            "sur cette période récente : rejouer tout "
                            "l'historique à chaque dépôt serait inutilement long.")
    _debut = (pd.Timestamp(_fin_reseau)
              - pd.Timedelta(days=_jours)).strftime('%Y-%m-%d %H:%M:%S')
    st.caption(f"Période analysée : du {_debut[:16]} au {_fin_reseau[:16]}")

    try:
        import regles_inter_stations as inter

        with st.spinner("Comparaison des stations en cours…"):
            i1 = inter.regle_I1_faux_zero(con, debut=_debut, fin=_fin_reseau)
            i2_anom, i2_ok, i2_declins = inter.regle_I2_coherence_crue(
                con, debut=_debut, fin=_fin_reseau)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Faux zéros (I1)", len(i1))
        c2.metric("Crues non propagées (I2)", len(i2_anom))
        c3.metric("Propagations confirmées", len(i2_ok))
        c4.metric("Paires non jugées", len(i2_declins))

        # ── I1 ──
        st.subheader("I1 — Faux zéro pluviométrique")
        if not i1:
            st.success("✅ Aucun faux zéro sur la période — s'il n'a pas plu, "
                       "aucune station ne peut être prise en défaut.")
        else:
            for a in sorted(i1, key=lambda x: x['severity'] != 'high'):
                box = SEVERITY_BOX.get(a['severity'], st.info)
                nom = STATION_NAMES.get(a['station'], '— hors liste —')
                box(f"**{a['station']} — {nom}**\n\n{a['description']}")

        # ── I2 ──
        st.subheader("I2 — Cohérence de crue amont → aval")
        if not i2_anom:
            st.success("✅ Toutes les crues amont se retrouvent à l'aval "
                       "(sur les paires jugeables).")
        else:
            for a in i2_anom:
                st.error(f"**{a['paire']}**\n\n{a['description']}")

        if i2_ok:
            with st.expander(f"✅ {len(i2_ok)} propagation(s) confirmée(s) — "
                             "le réseau se comporte normalement"):
                st.dataframe(
                    pd.DataFrame([{'Paire': c['paire'],
                                   'Constat': c['description']}
                                  for c in i2_ok]),
                    use_container_width=True, hide_index=True)

        if i2_declins:
            with st.expander(f"⚪ {len(i2_declins)} paire(s) non jugée(s) — "
                             "pourquoi la règle s'abstient"):
                st.caption("S'abstenir n'est pas un échec : une règle qui ne "
                           "juge que lorsqu'elle est sûre reste crédible.")
                _res = {}
                for _et, _r in i2_declins:
                    _cle = _r.split(' du ')[0].split(' : ')[0]
                    _res.setdefault(_cle, []).append(_et)
                st.dataframe(
                    pd.DataFrame([{'Raison': k, 'Nombre': len(v),
                                   'Exemple': v[0]}
                                  for k, v in sorted(_res.items(),
                                                     key=lambda kv: -len(kv[1]))]),
                    use_container_width=True, hide_index=True)

        # ── provenance : quelles stations sont réelles ? ──
        _orig = memoire.origine_stations(con)
        if _orig:
            _n_reel = sum(1 for v in _orig.values() if v == 'réel')
            st.warning(
                f"⚠️ Cette base contient des données SIMULÉES : "
                f"{_n_reel} station(s) réelle(s) sur {len(_orig)}. "
                "Les règles inter-stations refusent automatiquement toute "
                "comparaison entre une station réelle et une station simulée.")

    except ImportError:
        st.info("`regles_inter_stations.py` introuvable — placez-le dans le "
                "même dossier pour activer les règles inter-stations.")
    except Exception as e:
        st.error(f"Règles inter-stations indisponibles : {type(e).__name__} — {e}")


# ─────────────────────────────────────────────
# 4) DÉTAIL par station / capteur (sections dépliables)
# ─────────────────────────────────────────────
st.divider()
st.header("🔍 Détail par station et capteur")

for i, r in enumerate(resultats):
    df, anomalies = r['df'], r['anomalies']
    nom_station = STATION_NAMES.get(r['station'], 'hors liste')
    name  = SENSOR_DEFAULTS.get(r['sensor'], {}).get('name', 'capteur inconnu')
    titre = (f"🏢 {r['station']} — {nom_station} · Capteur {r['sensor']} ({name}) "
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
                text = f"{icon} **{a['type']}** — {a['description']}"
                details = a.get('details', [])
                if details:
                    text += "\n\n" + "\n".join(f"- {d}" for d in details[:10])
                show(text)
        else:
            st.success("✅ Aucune anomalie détectée")

        chart_df = (df.set_index('timestamp')[['value_raw', 'value']]
                      .rename(columns={'value_raw': 'Valeur brute',
                                       'value': 'Valeur corrigée'}))
        st.line_chart(chart_df)

        cols = ['timestamp', 'value_raw', 'value', 'corrected']
        if 'code' in df.columns and df['code'].notna().any():
            cols.append('code')
        show_df = df[cols].rename(
            columns={'timestamp': 'Horodatage',
                     'value_raw': 'Valeur brute',
                     'value':     'Valeur corrigée',
                     'corrected': 'Corrigée ?',
                     'code':      'Code capteur'})

        def _highlight(row):
            color = 'background-color: #ffd6d6; color: #7a1010;' if row['Corrigée ?'] else ''
            return [color] * len(row)

        st.dataframe(show_df.style.apply(_highlight, axis=1),
                     use_container_width=True, hide_index=True)
