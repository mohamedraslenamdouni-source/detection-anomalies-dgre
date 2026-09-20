"""
regles_inter_stations.py — Règles de cohérence ENTRE stations (phase 2)
=======================================================================
La phase 1 (mis_parser.py, memoire.py) regarde chaque station isolément.
Ces règles-ci regardent les stations LES UNES PAR RAPPORT AUX AUTRES :
c'est la seule façon de trancher des questions qu'une station seule ne
peut pas trancher.

Règle implémentée dans cette version :

  I1 — FAUX ZÉRO PLUVIOMÉTRIQUE
       Un pluviomètre qui mesure 0,0 mm pendant qu'au moins MIN_VOISINS
       stations voisines (≤ RAYON_KM) mesurent chacune une vraie pluie
       est probablement bouché (entonnoir obstrué, augets bloqués).
       C'est la réponse à la question que la règle R4 de la phase 1 pose
       sans pouvoir y répondre : « tous zéros — saison sèche ou capteur
       bouché ? ». Seuls les voisins peuvent trancher.

Garde-fous :
  • une station SILENCIEUSE (aucune donnée le jour considéré) n'est pas
    jugée : l'absence de données est le travail de la règle L3, pas d'I1 ;
  • les voisins sont choisis par DISTANCE GÉOGRAPHIQUE (x/y de
    config_stations.csv) : la pluie est un phénomène spatial, un orage ne
    suit pas le réseau hydrographique ;
  • PROVENANCE : une station réelle n'est comparée qu'à des voisines
    réelles, une simulée qu'à des simulées (table provenance_station).
    Comparer du réel à du simulé fabriquerait des anomalies fictives ;
  • toutes les lectures passent par donnees_propres.charger_serie_propre()
    (valeurs impossibles exclues — plan A).

Utilisation :
    py regles_inter_stations.py                    # analyse historique_demo.db
    py regles_inter_stations.py autre_base.db
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from donnees_propres import charger_serie_propre

# ─────────────────────────────────────────────
# PARAMÈTRES DE LA RÈGLE I1
# ─────────────────────────────────────────────
# Voisinage : les K stations PLUVIOMÉTRIQUES les plus proches (≤ RAYON_MAX_KM).
# Un rayon fixe ne suffit pas : autour d'une station donnée, beaucoup de
# voisines géographiques sont hydro-only (pas de capteur 0006) — testé sur la
# base : 7 voisines à 50 km dont 2 seulement mesurent la pluie, rendant la
# condition « ≥ 3 voisines pluvieuses » mathématiquement impossible.
K_VOISINES = 8           # nb de voisines pluviométriques considérées
RAYON_MAX_KM = 80.0      # au-delà, une station n'est plus un « voisin » crédible
DELTA_ALTITUDE_M = 300.0 # écart d'altitude maximal entre une station et ses
#                          témoins. Effet orographique : le relief force l'air
#                          à monter, ce qui déclenche la pluie. Une station de
#                          montagne peut donc être arrosée pendant qu'une
#                          station de plaine voisine reste sèche — sans aucune
#                          panne. Comparer deux altitudes très différentes
#                          reviendrait à accuser un capteur sain.
#                          Nécessite la colonne altitude_m (remplie par
#                          altitudes.py). Si elle est vide, le filtre est
#                          ignoré et un avertissement est affiché.
MIN_VOISINS = 3          # nb minimal de voisines pluvieuses pour accuser
SEUIL_PLUIE_MM = 3.0     # cumul journalier minimal pour dire « il a plu »
SEUIL_ATTENDU_MM = 4.0   # pluie ATTENDUE à la station (interpolation inverse
#                          distance des voisines) en dessous de laquelle on
#                          n'accuse pas : si les voisines PROCHES sont sèches
#                          et que seule la pluie LOINTAINE existe, le zéro est
#                          un simple bord d'orage, pas un capteur bouché
SEUIL_GRAVE_MM = 15.0    # pluie attendue au-delà → gravité haute
MIN_MESURES_JOUR = 24    # nb minimal de mesures pour juger une journée
#                          (en dessous, la station est traitée comme muette)

# ─────────────────────────────────────────────
# PARAMÈTRES DE LA RÈGLE I2 (cohérence de crue amont → aval)
# ─────────────────────────────────────────────
# Hypothèse fixe assumée : l'onde de crue se propage entre 2 et 10 km/h.
# La FOURCHETTE est volontairement large : elle absorbe l'incertitude sur la
# vitesse réelle (qui dépend de la pente, du débit, de la géométrie du lit)
# sans prétendre à une précision que les données ne permettent pas.
V_ONDE_MIN_KMH = 2.0     # onde lente  → borne TARDIVE de la fenêtre d'arrivée
V_ONDE_MAX_KMH = 10.0    # onde rapide → borne PRÉCOCE de la fenêtre d'arrivée
MARGE_FENETRE_H = 2.0    # marge ajoutée de part et d'autre de la fenêtre

# Détection de crue à l'AMONT : montée nette au-dessus de la ligne de base.
# Même outil statistique que L4/L6 (médiane + k·MAD) pour rester cohérent.
K_CRUE_AMONT = 6.0       # nb de MAD au-dessus de la médiane pour dire « crue »
MIN_MONTEE_AMONT_CM = 10.0   # montée minimale absolue (évite les séries plates
#                              où le MAD est quasi nul et tout devient « crue »)

# Réponse attendue à l'AVAL : montée au-dessus de son niveau LOCAL — la
# médiane des 24 h précédant la fenêtre d'arrivée. Un seuil global (médiane +
# k·MAD de toute la série) ne convient pas : sur une station aval riche en
# crues, le MAD global est énorme et le seuil devient inatteignable (constaté
# au test : seuil 268 cm pour des pics réels à 150 cm → fausses alertes).
MIN_MONTEE_AVAL_CM = 5.0     # montée minimale absolue au-dessus du niveau local
PART_MONTEE_AMONT = 0.10     # ou au moins 10 % de la montée amont (le plus
#                              exigeant des deux) : une grosse crue amont doit
#                              laisser une trace proportionnée

# Abstention : si l'aval est DÉJÀ en crue (ou en décrue d'un épisode
# précédent) quand l'onde arrive, on ne peut pas juger. Constaté au test :
# l'onde amont arrive dans une eau encore haute qui continue de baisser — le
# niveau ne monte pas, il descend simplement moins vite. Accuser le capteur
# dans ce cas est une FAUSSE ALERTE (67 cas sur la base saine avant ce garde).
SEUIL_AVAL_DEJA_HAUT = 1.5   # niveau local > médiane + 1.5 × (médiane - min) → déjà en crue
PENTE_DECRUE_CM_H = -0.5     # décroissance moyenne avant la fenêtre → en décrue

MIN_POINTS_SERIE = 100   # en dessous, la série est trop courte pour juger
COUVERTURE_FENETRE = 0.5 # part minimale de la fenêtre d'arrivée couverte par
#                          des mesures aval pour rendre un verdict
DISTANCE_MAX_KM = 100.0  # au-delà, l'onde s'amortit et se mélange aux apports
#                          intermédiaires : mesuré sur la base saine, le taux
#                          d'échec passe de 0,8 % (< 25 km) à 10 % (> 100 km),
#                          ce qui traduit l'atténuation, pas une panne. Au-delà
#                          de cette distance la règle s'abstient.

CONFIG = Path(__file__).parent / 'config_stations.csv'


# ─────────────────────────────────────────────
# CONTEXTE : positions, noms, provenance
# ─────────────────────────────────────────────

def charger_contexte(con):
    """Positions, altitudes et noms depuis config_stations.csv,
    provenance depuis la base."""
    cfg = pd.read_csv(CONFIG, sep=';', encoding='utf-8-sig',
                      dtype={'code_station': str})
    cfg['code_station'] = cfg['code_station'].str.strip()
    cfg = cfg[cfg['x'].notna() & cfg['y'].notna()]

    positions = {r['code_station']: (float(r['x']), float(r['y']))
                 for _, r in cfg.iterrows()}
    noms = {r['code_station']: str(r['nom']).strip() for _, r in cfg.iterrows()}

    altitudes = {}
    if 'altitude_m' in cfg.columns:
        alt = pd.to_numeric(cfg['altitude_m'], errors='coerce')
        altitudes = {c: float(a) for c, a in zip(cfg['code_station'], alt)
                     if pd.notna(a)}

    try:
        provenance = dict(con.execute(
            "SELECT station, origine FROM provenance_station").fetchall())
    except sqlite3.OperationalError:
        provenance = {}          # base 100 % réelle : pas de table, pas de garde
    return positions, noms, provenance, altitudes


def voisines_pluvio(station, positions, provenance, stations_pluvio,
                    altitudes=None, k=K_VOISINES, rayon_max=RAYON_MAX_KM,
                    delta_alt=DELTA_ALTITUDE_M):
    """Les k stations PLUVIOMÉTRIQUES les plus proches (≤ rayon_max km),
    de MÊME PROVENANCE et d'ALTITUDE COMPARABLE (± delta_alt m).

    Le filtre d'altitude n'est appliqué que si les deux stations ont une
    altitude connue : sans donnée, on ne prétend pas savoir.
    """
    if station not in positions:
        return []
    x0, y0 = positions[station]
    mienne = provenance.get(station)
    alt0 = (altitudes or {}).get(station)
    res = []
    for code in stations_pluvio:
        if code == station or code not in positions:
            continue
        if provenance and provenance.get(code) != mienne:
            continue             # jamais réel ↔ simulé
        alt = (altitudes or {}).get(code)
        if alt0 is not None and alt is not None and abs(alt - alt0) > delta_alt:
            continue             # relief différent → pluie non comparable
        x, y = positions[code]
        d = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 / 1000.0
        if d <= rayon_max:
            res.append((code, round(d, 1)))
    return sorted(res, key=lambda t: t[1])[:k]


# ─────────────────────────────────────────────
# RÈGLE I1 — FAUX ZÉRO PLUVIOMÉTRIQUE
# ─────────────────────────────────────────────

def cumuls_journaliers_pluie(con, stations, debut=None, fin=None):
    """{station: Series cumul_mm par jour, n_mesures par jour} via lecture propre."""
    cumuls, comptes = {}, {}
    for st in stations:
        df, _ = charger_serie_propre(st, '0006', con, debut=debut, fin=fin)
        if df.empty:
            continue
        s = df.set_index('timestamp')['value']
        cumuls[st] = s.resample('D').sum()
        comptes[st] = s.resample('D').count()
    return cumuls, comptes


def regle_I1_faux_zero(con, verbose=False, debut=None, fin=None):
    """Retourne la liste des anomalies « faux zéro pluviométrique ».

    Pour chaque jour et chaque station pluviométrique :
      station = 0,0 mm (en ayant VRAIMENT mesuré : ≥ MIN_MESURES_JOUR relevés)
      ET ≥ MIN_VOISINS voisines (≤ RAYON_KM, même provenance) ont chacune
         cumulé ≥ SEUIL_PLUIE_MM ce jour-là
      → pluviomètre probablement bouché ce jour-là.

    Les jours consécutifs en faute sont regroupés en un seul épisode.
    """
    positions, noms, provenance, altitudes = charger_contexte(con)

    stations_pluie = [s for (s,) in con.execute(
        "SELECT DISTINCT station FROM mesures WHERE sensor='0006'")]
    cumuls, comptes = cumuls_journaliers_pluie(con, stations_pluie,
                                               debut=debut, fin=fin)
    avec_donnees = set(cumuls)   # seules celles-ci peuvent témoigner

    if verbose:
        print(f"  {len(cumuls)} station(s) pluviométrique(s) avec données")
        if altitudes:
            print(f"  filtre d'altitude actif : témoins à ±"
                  f"{DELTA_ALTITUDE_M:.0f} m ({len(altitudes)} altitudes connues)")
        else:
            print("  ⚠ colonne altitude_m vide — filtre orographique INACTIF "
                  "(lancer altitudes.py pour l'activer)")

    suspects = []                # (station, jour, med_voisines, detail)
    for st in cumuls:
        vois = voisines_pluvio(st, positions, provenance, avec_donnees,
                               altitudes)
        codes_vois = [c for c, _ in vois]
        if len(codes_vois) < MIN_VOISINS:
            continue             # trop isolée pour être jugée par ses voisines

        for jour, cumul in cumuls[st].items():
            if cumul != 0.0:
                continue
            if comptes[st].get(jour, 0) < MIN_MESURES_JOUR:
                continue         # journée quasi muette → travail de L3, pas d'I1

            # pluie des voisines ce jour-là, avec leur distance
            releves = []
            for cv, d in vois:
                c = cumuls.get(cv)
                if c is not None and jour in c.index:
                    releves.append((cv, d, float(c[jour])))
            pluvieuses = [(cv, d, v) for cv, d, v in releves
                          if v >= SEUIL_PLUIE_MM]
            if len(pluvieuses) < MIN_VOISINS:
                continue         # pas de pluie avérée dans le secteur

            # Pluie ATTENDUE à la station : interpolation inverse-distance
            # (poids 1/d²) de TOUTES les voisines, sèches comprises. Si les
            # voisines proches sont sèches, l'attendu reste faible et le zéro
            # est un bord d'orage — pas une accusation.
            w = [(1.0 / max(d, 1.0) ** 2, v) for _, d, v in releves]
            attendu = sum(wi * vi for wi, vi in w) / sum(wi for wi, _ in w)
            if attendu < SEUIL_ATTENDU_MM:
                continue

            suspects.append((st, jour, attendu,
                             [(cv, v) for cv, _, v in pluvieuses]))

    # regrouper les jours consécutifs d'une même station en épisodes
    anomalies = []
    par_station = {}
    for st, jour, med, detail in suspects:
        par_station.setdefault(st, []).append((jour, med, detail))

    for st, jours in par_station.items():
        jours.sort()
        episode = [jours[0]]
        for j in jours[1:]:
            if (j[0] - episode[-1][0]).days == 1:
                episode.append(j)
            else:
                anomalies.append(_episode_vers_anomalie(st, episode, noms))
                episode = [j]
        anomalies.append(_episode_vers_anomalie(st, episode, noms))

    return anomalies


def _episode_vers_anomalie(station, episode, noms):
    jours = [e[0] for e in episode]
    attendu_max = max(e[1] for e in episode)
    pire = max(episode, key=lambda e: e[1])
    exemples = ', '.join(f"{noms.get(c, c)} {v:.0f} mm"
                         for c, v in pire[2][:3])
    n_j = len(jours)
    return {
        'type': 'I1_faux_zero_pluie',
        'station': station,
        'severity': 'high' if attendu_max >= SEUIL_GRAVE_MM else 'medium',
        'timestamp': jours[0],
        'description': (
            f"🌧️ 0,0 mm pendant {n_j} jour(s) "
            f"({jours[0]:%d/%m} → {jours[-1]:%d/%m}) alors que la pluie "
            f"attendue à cet endroit (interpolée des voisines) atteint "
            f"{attendu_max:.0f} mm/j — ex. {exemples}. "
            f"Pluviomètre probablement bouché : "
            f"vérification terrain recommandée."),
        'jours': jours,
    }


# ─────────────────────────────────────────────
# RÈGLE I2 — COHÉRENCE DE CRUE AMONT → AVAL
# ─────────────────────────────────────────────

RESEAU = Path(__file__).parent / 'reseau_stations.csv'


def _evenements_de_crue(df, k=K_CRUE_AMONT, min_montee=MIN_MONTEE_AMONT_CM):
    """Épisodes où la cote dépasse nettement sa ligne de base.

    Retourne [(t_debut, t_pic, valeur_pic, montee), ...].
    Seuil = médiane + max(k · 1.4826 · MAD, min_montee) : le même outil
    robuste que L4/L6, plus un plancher absolu pour les séries très plates.
    """
    v = df['value']
    med = float(v.median())
    mad = float((v - med).abs().median())
    seuil = med + max(k * 1.4826 * mad, min_montee)

    au_dessus = v > seuil
    if not au_dessus.any():
        return []

    evenements = []
    groupe = (au_dessus != au_dessus.shift()).cumsum()
    for _, bloc in df[au_dessus].groupby(groupe[au_dessus]):
        i_pic = bloc['value'].idxmax()
        evenements.append((bloc['timestamp'].iloc[0],
                           df.loc[i_pic, 'timestamp'],
                           float(df.loc[i_pic, 'value']),
                           float(df.loc[i_pic, 'value'] - med)))
    return evenements


def regle_I2_coherence_crue(con, verbose=False, debut=None, fin=None):
    """Pour chaque paire amont→aval du graphe réel, vérifie que les crues
    amont se retrouvent à l'aval dans la fenêtre de propagation.

    Retourne (anomalies, confirmations, declins) :
      anomalies      crue amont SANS réponse aval dans la fenêtre → 🔴
      confirmations  crue amont AVEC réponse aval → ✅ (réseau sain)
      declins        paires non jugées, avec la raison → ⚪
    """
    positions, noms, provenance, _ = charger_contexte(con)

    if not RESEAU.exists():
        raise SystemExit(f"Graphe introuvable : {RESEAU}")
    paires = pd.read_csv(RESEAU, sep=';', encoding='utf-8-sig',
                         dtype={'station_amont': str, 'station_aval': str})
    for c in ('station_amont', 'station_aval'):
        paires[c] = paires[c].str.strip()

    stations_base = {s for (s,) in con.execute(
        "SELECT DISTINCT station FROM mesures WHERE sensor='0001'")}

    anomalies, confirmations, declins = [], [], []
    cache = {}

    def serie(st):
        if st not in cache:
            df, _ = charger_serie_propre(st, '0001', con,
                                         debut=debut, fin=fin)
            cache[st] = df
        return cache[st]

    for _, p in paires.iterrows():
        am, av = p['station_amont'], p['station_aval']
        nom_am = noms.get(am, am)
        nom_av = noms.get(av, av)
        etiquette = f"{nom_am} → {nom_av} ({p['distance_km']} km)"

        # ── Garde 1 : le barrage rompt le signal — on ne juge pas ──
        if str(p['fiabilite']) != 'haute':
            declins.append((etiquette, f"fiabilité « {p['fiabilite']} » : "
                            f"obstacle ({p['franchit']}) entre les deux — "
                            "le débit aval dépend de la gestion, pas de la pluie"))
            continue

        # ── Garde 2 : distance — au-delà, l'onde n'est plus identifiable ──
        if float(p['distance_km']) > DISTANCE_MAX_KM:
            declins.append((etiquette, f"distance > {DISTANCE_MAX_KM:.0f} km : "
                            "onde trop amortie et mêlée aux apports "
                            "intermédiaires pour être suivie"))
            continue

        # ── Garde 3 : provenance — jamais réel ↔ simulé ──
        if provenance and provenance.get(am) != provenance.get(av):
            declins.append((etiquette, "provenances différentes "
                            f"({provenance.get(am)} / {provenance.get(av)}) : "
                            "comparaison sans signification"))
            continue

        # ── Garde 4 : données de cote suffisantes des deux côtés ──
        if am not in stations_base or av not in stations_base:
            declins.append((etiquette, "cote absente d'un des deux côtés"))
            continue
        s_am, s_av = serie(am), serie(av)
        if len(s_am) < MIN_POINTS_SERIE or len(s_av) < MIN_POINTS_SERIE:
            declins.append((etiquette, "série de cote trop courte"))
            continue

        # ── Crues à l'amont ──
        crues = _evenements_de_crue(s_am)
        if not crues:
            declins.append((etiquette, "aucune crue amont sur la période "
                            "(rien à propager, rien à vérifier)"))
            continue

        # fenêtre d'arrivée : distance / vitesse, bornes larges + marge
        d_km = float(p['distance_km'])
        tot_min = pd.Timedelta(hours=d_km / V_ONDE_MAX_KMH - MARGE_FENETRE_H)
        tot_max = pd.Timedelta(hours=d_km / V_ONDE_MIN_KMH + MARGE_FENETRE_H)
        tot_min = max(tot_min, pd.Timedelta(0))

        v_av = s_av['value']
        med_av = float(v_av.median())
        pas_av = s_av['timestamp'].diff().median()

        for t_debut, t_pic, v_pic, montee in crues:
            f0, f1 = t_debut + tot_min, t_pic + tot_max
            fenetre = s_av[(s_av['timestamp'] >= f0) & (s_av['timestamp'] <= f1)]

            # niveau LOCAL de l'aval : médiane des 24 h précédant la fenêtre
            avant = s_av[(s_av['timestamp'] >= f0 - pd.Timedelta(hours=24))
                         & (s_av['timestamp'] < f0)]
            base_loc = float(avant['value'].median()) if len(avant) >= 10 \
                else med_av
            seuil_av = base_loc + max(MIN_MONTEE_AVAL_CM,
                                      PART_MONTEE_AMONT * montee)

            # ── Garde 5 : l'aval est-il DÉJÀ en crue / en décrue ? ──
            # Dans ce cas l'onde amont arrive dans une eau déjà haute : le
            # niveau ne monte pas, il baisse moins vite. Impossible de
            # conclure → on s'abstient au lieu d'accuser à tort.
            if len(avant) >= 10:
                deja_haut = base_loc > med_av + SEUIL_AVAL_DEJA_HAUT * (
                    med_av - float(v_av.min()))
                dt_h = ((avant['timestamp'].iloc[-1]
                         - avant['timestamp'].iloc[0]).total_seconds() / 3600)
                pente = ((float(avant['value'].iloc[-1])
                          - float(avant['value'].iloc[0])) / dt_h
                         if dt_h > 0 else 0.0)
                if deja_haut or pente <= PENTE_DECRUE_CM_H:
                    etat = "déjà en crue" if deja_haut else "en décrue"
                    declins.append((etiquette,
                                    f"crue amont du {t_pic:%d/%m %H:%M} : "
                                    f"aval {etat} à l'arrivée de l'onde "
                                    f"(niveau {base_loc:.0f} cm, pente "
                                    f"{pente:+.1f} cm/h) — verdict impossible"))
                    continue

            # la fenêtre doit être suffisamment couverte de mesures aval
            if pas_av is not None and pas_av > pd.Timedelta(0):
                attendu = (f1 - f0) / pas_av
                if attendu > 0 and len(fenetre) / attendu < COUVERTURE_FENETRE:
                    declins.append((etiquette,
                                    f"crue amont du {t_pic:%d/%m %H:%M} : "
                                    "aval trop peu mesuré dans la fenêtre "
                                    "d'arrivée — verdict impossible"))
                    continue

            if fenetre.empty:
                declins.append((etiquette,
                                f"crue amont du {t_pic:%d/%m %H:%M} : "
                                "aucune mesure aval dans la fenêtre"))
                continue

            reponse = float(fenetre['value'].max())
            if reponse >= seuil_av:
                t_rep = fenetre.loc[fenetre['value'].idxmax(), 'timestamp']
                lag_h = (t_rep - t_pic).total_seconds() / 3600
                confirmations.append({
                    'paire': etiquette,
                    'description': (
                        f"✅ crue amont du {t_pic:%d/%m %H:%M} "
                        f"(pic {v_pic:.0f} cm, +{montee:.0f} cm) retrouvée à "
                        f"l'aval {lag_h:+.1f} h plus tard "
                        f"({reponse:.0f} cm, niveau avant {base_loc:.0f} cm)"),
                })
            else:
                anomalies.append({
                    'type': 'I2_crue_non_propagee',
                    'station': av,
                    'severity': 'high',
                    'timestamp': t_pic,
                    'paire': etiquette,
                    'description': (
                        f"🌊 Crue à l'amont {nom_am} le {t_pic:%d/%m %H:%M} "
                        f"(pic {v_pic:.0f} cm, montée +{montee:.0f} cm) mais "
                        f"AUCUNE réponse à l'aval {nom_av} dans la fenêtre "
                        f"d'arrivée [{f0:%d/%m %H:%M} → {f1:%d/%m %H:%M}] "
                        f"(maximum aval {reponse:.0f} cm, niveau avant la fenêtre "
                        f"{base_loc:.0f} cm, montée exigée "
                        f"≥ {seuil_av - base_loc:.0f} cm). "
                        f"Écoulement direct sans obstacle "
                        f"déclaré : capteur aval à vérifier — une crue a pu "
                        f"passer sans être mesurée."),
                })

    if verbose:
        print(f"  {len(paires)} paire(s) au total · "
              f"{len(confirmations)} propagation(s) confirmée(s) · "
              f"{len(anomalies)} incohérence(s) · "
              f"{len(declins)} refus de juger")
    return anomalies, confirmations, declins



if __name__ == '__main__':
    base = Path(sys.argv[1] if len(sys.argv) > 1 else 'historique_demo.db')
    if not base.exists():
        raise SystemExit(f"Base introuvable : {base}")
    con = sqlite3.connect(base)
    _, noms, provenance, _alt = charger_contexte(con)

    print(f"Base : {base.name}")
    print(f"Règle I1 — faux zéro pluviométrique "
          f"({K_VOISINES} voisines pluvio les plus proches ≤ {RAYON_MAX_KM:.0f} km, "
          f"≥ {MIN_VOISINS} à ≥ {SEUIL_PLUIE_MM:.0f} mm/j)\n")

    anomalies = regle_I1_faux_zero(con, verbose=True)

    if not anomalies:
        print("\n✅ Aucun faux zéro détecté.")
        print("   (Normal en saison sèche : s'il ne pleut nulle part, aucune")
        print("    station ne peut être prise en défaut par ses voisines.)")
    else:
        print(f"\n{len(anomalies)} épisode(s) suspect(s) :\n")
        for a in sorted(anomalies, key=lambda a: (a['severity'] != 'high',
                                                  a['station'])):
            icone = '🔴' if a['severity'] == 'high' else '🟡'
            orig = provenance.get(a['station'], '?')
            print(f"{icone} {a['station']} {noms.get(a['station'], '')} "
                  f"[{orig}]")
            print(f"   {a['description']}\n")

    # ── I2 ──
    print("\n" + "=" * 66)
    print(f"Règle I2 — cohérence de crue amont → aval")
    print(f"(onde {V_ONDE_MIN_KMH:.0f}-{V_ONDE_MAX_KMH:.0f} km/h, "
          f"jugée UNIQUEMENT sur les paires « haute » — les paires avec "
          f"barrage sont refusées)")
    print("=" * 66 + "\n")

    anomalies2, confirmations, declins = regle_I2_coherence_crue(
        con, verbose=True)

    if confirmations:
        print(f"\n✅ {len(confirmations)} propagation(s) confirmée(s) "
              "(le réseau se comporte normalement) :")
        for c in confirmations[:8]:
            print(f"   {c['paire']}")
            print(f"      {c['description']}")
        if len(confirmations) > 8:
            print(f"   … et {len(confirmations) - 8} autre(s)")

    if anomalies2:
        print(f"\n🔴 {len(anomalies2)} incohérence(s) amont/aval :\n")
        for a in anomalies2:
            print(f"   {a['paire']}")
            print(f"   {a['description']}\n")
    else:
        print("\n✅ Aucune incohérence amont/aval sur les paires jugeables.")

    if declins:
        # regrouper les refus par raison pour rester lisible
        raisons = {}
        for et, r in declins:
            cle = r.split(' du ')[0].split(' : ')[0]
            raisons.setdefault(cle, []).append(et)
        print(f"\n⚪ {len(declins)} refus de juger (comportement voulu) :")
        for r, ets in sorted(raisons.items(), key=lambda kv: -len(kv[1])):
            print(f"   {len(ets):3d} × {r}")
            for e in ets[:2]:
                print(f"         ex. {e}")
    con.close()
