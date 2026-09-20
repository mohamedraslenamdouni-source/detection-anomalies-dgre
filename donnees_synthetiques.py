"""
donnees_synthetiques.py — Générateur de données de DÉMONSTRATION (DGRE)
=======================================================================

⚠️  LES DONNÉES PRODUITES PAR CE SCRIPT SONT ENTIÈREMENT SIMULÉES.
    Elles ne proviennent d'AUCUNE mesure réelle du réseau DGRE et ne doivent
    jamais être utilisées pour une analyse hydrologique ou une décision
    opérationnelle. Leur seul rôle : permettre de démontrer et de tester le
    prototype (règles R1-R9, L1-L6, règles inter-stations) en attendant la
    mise à disposition des données réelles.

Ce que le script réutilise du projet RÉEL (pour que la simulation soit
plausible et cohérente avec le terrain) :
  • config_stations.csv  → les 102 vrais codes, noms, gouvernorats, types,
                           coordonnées et seuils min/max de chaque station
  • reseau_stations.csv  → le vrai graphe amont-aval issu de reseau.py, qui
                           sert à propager les crues d'amont en aval avec un
                           décalage temporel réaliste

Ce que le script SIMULE :
  • un champ de pluie régional (cellules orageuses se déplaçant sur le pays,
    saisonnalité tunisienne, gradient nord humide / sud aride)
  • la réponse hydrologique de chaque bassin (hydrogramme unitaire + tarissement)
  • la propagation amont→aval le long du graphe réel
  • la tension batterie (cycle solaire jour/nuit)
  • un jeu d'anomalies contrôlées, journalisées, pour que les règles de
    détection aient effectivement quelque chose à trouver

Sorties :
  historique_demo.db              base SQLite au format attendu par memoire.py
  DONNEES_SYNTHETIQUES_LISEZMOI.md  fiche de traçabilité (paramètres, anomalies)
  mis_demo/*.MIS                  (option --mis) fichiers à déposer dans le
                                  dashboard pour démontrer les règles R1-R9

Utilisation :
    py donnees_synthetiques.py                 # 1 an, base + fiche
    py donnees_synthetiques.py --jours 180     # période plus courte
    py donnees_synthetiques.py --mis           # + fichiers .MIS de démo
    py donnees_synthetiques.py --graine 7      # autre tirage aléatoire
"""

import argparse
import glob
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DOSSIER = Path(__file__).parent

F_CONFIG = DOSSIER / 'config_stations.csv'
F_RESEAU = DOSSIER / 'reseau_stations.csv'
F_SORTIE = DOSSIER / 'historique_demo.db'
F_FICHE = DOSSIER / 'DONNEES_SYNTHETIQUES_LISEZMOI.md'
D_MIS = DOSSIER / 'mis_demo'

# ─────────────────────────────────────────────
# PARAMÈTRES DE SIMULATION
# ─────────────────────────────────────────────
PAS_MIN = 60          # pas de temps des séries, en minutes
JOURS = 365           # durée simulée (>= 90 j pour que L4/L6 soient testables)
GRAINE = 42           # graine aléatoire (reproductibilité)

# Climat tunisien : la saison des pluies va de septembre à mai, l'été est sec.
# Poids mensuel de la probabilité d'orage (index 0 = janvier).
SAISON = np.array([1.15, 1.05, 1.00, 0.85, 0.55, 0.20,
                   0.06, 0.12, 0.60, 1.10, 1.25, 1.20])

# Cumuls annuels visés, d'après les normales climatiques tunisiennes :
# Nord (Bizerte, Béja, Ain Draham) 450-600 mm · Centre 250-350 mm ·
# Sud (Kébili, Tataouine) 100-150 mm. Le champ simulé est recalé sur ces
# valeurs après tirage, ce qui rend le résultat indépendant du nombre d'orages.
CUMUL_NORD_MM = 560   # cumul annuel visé pour les stations les plus arrosées
ARIDITE_SUD = 0.22    # part du cumul nord reçue par l'extrême sud
N_ORAGES_AN = 520     # cellules orageuses simulées sur l'ensemble du pays / an
INTENSITE_MOY = 5.0   # échelle de la loi exponentielle des intensités (mm/h)
INTENSITE_MAX = 38.0  # plafond d'intensité horaire au centre de la cellule
GAIN_DEBIT = 9.0      # conversion ruissellement → débit (m³/s par mm)
CELERITE_KMH = 5.0    # vitesse de propagation de l'onde de crue (km/h)
ATTENUATION = 0.55    # part du débit amont retrouvée à la station aval

# Batterie (cohérent avec BATTERY de mis_parser.py : nominal 12 V)
BAT_NUIT = 12.15
BAT_JOUR = 13.70
BAT_BRUIT = 0.05


# ─────────────────────────────────────────────
# 1. LECTURE DU CONTEXTE RÉEL
# ─────────────────────────────────────────────
def charger_stations():
    """Les 102 stations réelles : code, type, position, seuils."""
    if not F_CONFIG.exists():
        sys.exit(f"Fichier introuvable : {F_CONFIG}")
    df = pd.read_csv(F_CONFIG, sep=';', encoding='utf-8-sig',
                     dtype={'code_station': str})
    manquantes = [c for c in ('x', 'y') if c not in df.columns]
    if manquantes:
        sys.exit(
            f"Colonne(s) {', '.join(manquantes)} absente(s) de "
            f"{F_CONFIG.name}.\n"
            "Ce script a besoin des coordonnées des stations (gradient de "
            "pluie nord-sud, distances entre cellules orageuses).\n"
            "Utilise la version du fichier qui contient les colonnes x et y "
            "— celle du Claude Project / celle utilisée par altitudes.py.")
    df['code_station'] = df['code_station'].str.strip()
    df['type_station'] = df['type_station'].astype(str).str.strip()
    df = df[df['x'].notna() & df['y'].notna()].reset_index(drop=True)

    # Indice d'humidité : 1 au nord (arrosé) → ARIDITE_SUD à l'extrême sud
    ymin, ymax = df['y'].min(), df['y'].max()
    nord = (df['y'] - ymin) / (ymax - ymin)
    df['humidite'] = ARIDITE_SUD + (1 - ARIDITE_SUD) * nord ** 1.25
    return df


def charger_reseau(codes_connus):
    """Relations amont→aval réelles, réduites aux liens DIRECTS.

    reseau_stations.csv contient la fermeture transitive (A→B, B→C et A→C).
    Pour propager une crue sans la compter deux fois, on ne garde que les
    liens directs : (a,b) est direct s'il n'existe aucun c tel que a→c→b.
    """
    if not F_RESEAU.exists():
        return pd.DataFrame(columns=['station_amont', 'station_aval',
                                     'distance_km', 'fiabilite'])
    r = pd.read_csv(F_RESEAU, sep=';', encoding='utf-8-sig',
                    dtype={'station_amont': str, 'station_aval': str})
    r['station_amont'] = r['station_amont'].str.strip()
    r['station_aval'] = r['station_aval'].str.strip()
    r = r[r['station_amont'].isin(codes_connus)
          & r['station_aval'].isin(codes_connus)]

    paires = set(zip(r['station_amont'], r['station_aval']))
    sortants = {}
    for a, b in paires:
        sortants.setdefault(a, set()).add(b)

    directs = []
    for a, b in paires:
        # existe-t-il une station intermédiaire c sur le chemin a → c → b ?
        indirect = any((c, b) in paires for c in sortants.get(a, ()) if c != b)
        if not indirect:
            directs.append((a, b))

    d = r.set_index(['station_amont', 'station_aval'])
    lignes = []
    for a, b in directs:
        info = d.loc[(a, b)]
        if isinstance(info, pd.DataFrame):
            info = info.iloc[0]
        lignes.append({'station_amont': a, 'station_aval': b,
                       'distance_km': float(info['distance_km']),
                       'fiabilite': str(info['fiabilite'])})
    return pd.DataFrame(lignes)


def ordre_topologique(codes, liens):
    """Ordonne les stations pour traiter l'amont avant l'aval."""
    entrants = {c: 0 for c in codes}
    enfants = {c: [] for c in codes}
    for _, l in liens.iterrows():
        enfants[l['station_amont']].append(l['station_aval'])
        entrants[l['station_aval']] += 1

    file = [c for c in codes if entrants[c] == 0]
    ordre = []
    while file:
        n = file.pop(0)
        ordre.append(n)
        for f in enfants[n]:
            entrants[f] -= 1
            if entrants[f] == 0:
                file.append(f)
    # sécurité : si un cycle subsiste (ne devrait pas), on ajoute le reste
    ordre += [c for c in codes if c not in set(ordre)]
    return ordre


# ─────────────────────────────────────────────
# 2. CHAMP DE PLUIE RÉGIONAL
# ─────────────────────────────────────────────
def champ_de_pluie(stations, dates, rng):
    """Pluie horaire (mm) par station : somme de cellules orageuses.

    Chaque orage a un centre, un rayon, une durée et une intensité. Les
    stations proches du centre reçoivent davantage : c'est ce qui crée la
    corrélation spatiale nécessaire pour tester la règle du « faux zéro »
    (une station à 0 mm quand toutes ses voisines mesurent de la pluie).
    """
    n_t, n_s = len(dates), len(stations)
    pluie = np.zeros((n_t, n_s))

    x = stations['x'].to_numpy() / 1000.0          # en km
    y = stations['y'].to_numpy() / 1000.0
    humid = stations['humidite'].to_numpy()

    annees = len(dates) * PAS_MIN / 60 / 24 / 365.25
    n_orages = int(N_ORAGES_AN * max(annees, 0.05))

    mois = np.array([d.month for d in dates])
    poids = SAISON[mois - 1]
    poids = poids / poids.sum()
    idx_depart = rng.choice(n_t, size=n_orages, p=poids)

    for i in range(n_orages):
        t0 = int(idx_depart[i])
        # centre de la cellule : tiré uniformément sur l'emprise du réseau.
        # Le gradient nord-sud est porté par `humidite`, pas par la position
        # des orages — sinon le sud devient totalement sec.
        cx = rng.uniform(x.min() - 40, x.max() + 40)
        cy = rng.uniform(y.min() - 40, y.max() + 40)
        rayon = rng.uniform(25, 130)                       # km
        duree = int(max(1, rng.gamma(2.2, 2.0)))           # pas de temps
        # intensité de pointe (mm/h) : loi exponentielle, plafonnée
        pointe = min(rng.exponential(INTENSITE_MOY) + 0.6, INTENSITE_MAX)

        d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        noyau = np.exp(-(d / rayon) ** 2) * humid          # atténuation spatiale
        if noyau.max() < 0.01:
            continue

        t1 = min(t0 + duree, n_t)
        # profil temporel : montée rapide, décrue plus lente
        prof = np.array([np.sin(np.pi * (k + 0.5) / duree) ** 1.5
                         for k in range(t1 - t0)])
        pluie[t0:t1] += pointe * np.outer(prof, noyau)

    # Recalage sur les normales climatiques : on ramène la station la plus
    # arrosée au cumul annuel visé, ce qui fixe l'échelle de tout le champ.
    cumul = pluie.sum(axis=0)
    ref = np.quantile(cumul, 0.95)
    if ref > 0:
        pluie *= (CUMUL_NORD_MM * max(annees, 1e-6)) / ref

    # bruit fin + arrondi au dixième (résolution auget basculeur : 0,1 mm)
    pluie *= rng.uniform(0.85, 1.15, size=pluie.shape)
    pluie[pluie < 0.1] = 0.0
    return np.round(pluie, 1)


# ─────────────────────────────────────────────
# 3. RÉPONSE HYDROLOGIQUE
# ─────────────────────────────────────────────
def hydrogramme_unitaire(n_pas, forme=2.4, echelle=3.0):
    """Noyau de réponse d'un bassin à une pluie unitaire (loi gamma)."""
    t = np.arange(1, n_pas + 1)
    uh = (t ** (forme - 1)) * np.exp(-t / echelle)
    return uh / uh.sum()


def serie_debit(pluie_bassin, taille_bassin, rng):
    """Convertit une chronique de pluie en débit (m³/s)."""
    # bassin plus grand = réponse plus lente et plus étalée
    echelle = 2.0 + 1.8 * np.log1p(taille_bassin)
    uh = hydrogramme_unitaire(int(12 + 6 * echelle), echelle=echelle)
    ruissellement = np.convolve(pluie_bassin, uh, mode='full')[:len(pluie_bassin)]

    gain = GAIN_DEBIT * (0.4 + 2.6 * taille_bassin)
    debit_base = 0.15 + 0.9 * taille_bassin
    q = debit_base + gain * ruissellement

    # tarissement : mémoire du bassin entre deux épisodes
    alpha = 0.965
    for t in range(1, len(q)):
        q[t] = max(q[t], alpha * q[t - 1] + (1 - alpha) * debit_base)

    # Étiage « vivant » : sans cela le tarissement converge vers une valeur
    # rigoureusement constante et la règle L1 (flatline) se déclenche sur
    # presque toutes les stations. Un cours d'eau réel fluctue toujours un peu
    # (évaporation, apports de nappe, résolution du capteur).
    n = len(q)
    lent = np.array(pd.Series(rng.normal(0, 1, n)).rolling(
        72, min_periods=1, center=True).mean().to_numpy(), dtype=float)
    lent /= (lent.std() + 1e-9)
    q *= (1.0 + 0.10 * lent)                    # fluctuation lente ±10 %
    q *= rng.normal(1.0, 0.015, n)              # bruit de mesure
    return np.maximum(q, 0.01) * rng.uniform(0.85, 1.15)


def debit_vers_cote(q, seuil_max_cm, rng):
    """Courbe de tarage inversée : h = a·Q^b (cm), bornée par les seuils."""
    h = 12.0 + 26.0 * np.power(np.maximum(q, 0.0), 0.42)
    h += rng.normal(0, 0.5, len(h))         # bruit capteur (~5 mm)
    return np.clip(h, 0.0, seuil_max_cm * 0.92)


# ─────────────────────────────────────────────
# 4. BATTERIE
# ─────────────────────────────────────────────
def serie_batterie(dates, rng):
    """Tension 12 V avec recharge solaire diurne et modulation saisonnière."""
    heures = np.array([d.hour + d.minute / 60 for d in dates])
    jours_an = np.array([d.timetuple().tm_yday for d in dates])

    # ensoleillement : nul la nuit, maximal vers 13 h, plus faible en hiver
    solaire = np.clip(np.sin(np.pi * (heures - 6.5) / 11.5), 0, None)
    saison = 0.80 + 0.20 * np.sin(2 * np.pi * (jours_an - 80) / 365.25)

    v = BAT_NUIT + (BAT_JOUR - BAT_NUIT) * solaire * saison
    v += rng.normal(0, BAT_BRUIT, size=len(dates))
    return np.round(v, 2)


# ─────────────────────────────────────────────
# 5. CONSTRUCTION DE TOUTES LES SÉRIES
# ─────────────────────────────────────────────
def construire(stations, liens, dates, rng):
    """Retourne {(code, capteur): np.array} pour toutes les stations."""
    codes = stations['code_station'].tolist()
    idx = {c: i for i, c in enumerate(codes)}

    print("  simulation du champ de pluie régional…")
    pluie = champ_de_pluie(stations, dates, rng)

    # Taille relative du bassin : nombre de stations situées en amont dans le
    # graphe réel. Un tronçon aval draine plus de surface → débits plus forts.
    amont_de = {c: 0 for c in codes}
    for _, l in liens.iterrows():
        amont_de[l['station_aval']] += 1
    n_max = max(max(amont_de.values()), 1)
    taille = {c: 0.25 + 1.75 * (amont_de[c] / n_max) for c in codes}

    print("  réponse hydrologique + propagation amont→aval…")
    debits = {}
    for code in ordre_topologique(codes, liens):
        i = idx[code]
        q = serie_debit(pluie[:, i], taille[code], rng)

        # apport des stations amont, décalé du temps de parcours
        entrants = liens[liens['station_aval'] == code]
        for _, l in entrants.iterrows():
            q_am = debits.get(l['station_amont'])
            if q_am is None:
                continue
            retard = int(round(l['distance_km'] / CELERITE_KMH * 60 / PAS_MIN))
            retard = max(retard, 1)
            decale = np.concatenate([np.full(retard, q_am[0]), q_am])[:len(q)]
            # un barrage casse la continuité : l'onde de crue est laminée
            part = ATTENUATION * (0.30 if l['fiabilite'] != 'haute' else 1.0)
            q = q + part * decale
        debits[code] = q

    print("  mise en forme par capteur…")
    series = {}
    for _, st in stations.iterrows():
        code, typ = st['code_station'], st['type_station']
        pluvio = 'luvio' in typ            # Pluviométrique / Hydro-pluviométrique
        hydro = 'ydro' in typ              # Hydrométrique / Hydro-pluviométrique

        series[(code, '0002')] = serie_batterie(dates, rng)
        if pluvio:
            series[(code, '0006')] = pluie[:, idx[code]]
        if hydro:
            q = debits[code]
            q = np.clip(q, 0, float(st.get('debit_max') or 3000) * 0.9)
            series[(code, '0007')] = np.round(q, 2)
            series[(code, '0001')] = np.round(
                debit_vers_cote(q, float(st.get('cote_max') or 2000), rng), 1)
    return series


# ─────────────────────────────────────────────
# 6. INJECTION D'ANOMALIES CONTRÔLÉES
# ─────────────────────────────────────────────
def injecter_anomalies(series, stations, dates, rng):
    """Introduit des pannes plausibles et retourne le journal de ce qui a été
    injecté — c'est la « vérité terrain » qui permet de vérifier que les
    règles L1-L6 détectent bien ce qu'elles doivent détecter."""
    journal = []
    n_t = len(dates)
    pas_par_jour = int(24 * 60 / PAS_MIN)

    # Une station ne reçoit qu'une seule anomalie : sinon une panne peut en
    # masquer une autre (ex. une station muette efface le pic censé déclencher
    # L4) et la vérité terrain devient fausse.
    deja = set()

    def choisir(capteur, n):
        cles = [k for k in series if k[1] == capteur and k[0] not in deja]
        rng.shuffle(cles)
        retenues = cles[:n]
        deja.update(k[0] for k in retenues)
        return retenues

    # ── L2 : dérive de la batterie (décharge progressive) ──
    for cle in choisir('0002', 3):
        v = series[cle]
        d = 30 * pas_par_jour
        pente = rng.uniform(0.055, 0.11)                   # V/jour
        chute = np.linspace(0, pente * 30, min(d, n_t))
        v[-len(chute):] -= chute
        series[cle] = np.round(np.clip(v, 6.0, None), 2)
        journal.append((cle[0], cle[1], 'L2 dérive batterie',
                        f"décharge {pente:.2f} V/j sur les 30 derniers jours"))

    # ── L2b : plus de recharge solaire (panneau ou régulateur HS) ──
    for cle in choisir('0002', 2):
        v = series[cle]
        d = min(12 * pas_par_jour, n_t)
        v[-d:] = np.minimum(v[-d:], 12.0 + rng.normal(0, 0.04, size=d))
        series[cle] = np.round(v, 2)
        journal.append((cle[0], cle[1], 'L2b absence de charge',
                        "tension plafonnée à 12 V sur les 12 derniers jours"))

    # ── L1 : flatline longue (capteur bloqué) ──
    for cle in choisir('0001', 2):
        v = series[cle]
        d = min(5 * pas_par_jour, n_t)
        v[-d:] = v[-d]
        series[cle] = v
        journal.append((cle[0], cle[1], 'L1 flatline longue',
                        f"cote figée à {v[-1]} sur les 5 derniers jours"))

    # ── L5 : dérive continue du capteur sur 7 jours ──
    for cle in choisir('0001', 2):
        v = series[cle]
        d = min(7 * pas_par_jour, n_t)
        v[-d:] = v[-d:] + np.linspace(0, rng.uniform(60, 120), d)
        series[cle] = np.round(v, 1)
        journal.append((cle[0], cle[1], 'L5 tendance continue',
                        "dérive régulière ajoutée sur les 7 derniers jours"))

    # ── L6 : rupture de moyenne (recalibration / déplacement du capteur) ──
    for cle in choisir('0001', 2):
        v = series[cle]
        t = n_t // 2
        v[t:] += rng.uniform(45, 90)
        series[cle] = np.round(v, 1)
        journal.append((cle[0], cle[1], 'L6 rupture de moyenne',
                        f"décalage brutal du zéro à mi-période "
                        f"({dates[t]:%d/%m/%Y})"))

    # ── L4 : valeur récente très inhabituelle ──
    for cle in choisir('0007', 2):
        v = series[cle]
        p = n_t - rng.integers(2, pas_par_jour)
        v[p] = float(np.median(v)) * rng.uniform(18, 30) + 50
        series[cle] = np.round(v, 2)
        journal.append((cle[0], cle[1], 'L4 valeur inhabituelle',
                        f"pic isolé injecté le {dates[p]:%d/%m %H:%M}"))

    # ── L3 : station muette (plus rien ne remonte) ──
    libres = [c for c in stations['code_station'].to_numpy() if c not in deja]
    muettes = list(rng.choice(libres, min(3, len(libres)), replace=False))
    for code in muettes:
        deja.add(code)
        d = int(rng.integers(2, 6)) * pas_par_jour
        for cle in [k for k in series if k[0] == code]:
            v = series[cle].astype(float)
            v[-d:] = np.nan                     # lignes non écrites en base
            series[cle] = v
        journal.append((code, 'tous', 'L3 station muette',
                        f"transmission interrompue {d // pas_par_jour} j "
                        "avant la fin"))

    # ── R7 : trous de transmission ponctuels (GPRS) ──
    n_trous = 0
    for cle in list(series):
        if rng.random() < 0.22:
            for _ in range(int(rng.integers(1, 4))):
                debut = int(rng.integers(0, max(n_t - 48, 1)))
                fin = debut + int(rng.integers(3, 30))
                v = series[cle].astype(float)
                v[debut:fin] = np.nan
                series[cle] = v
                n_trous += 1
    journal.append(('—', '—', 'R7 trous de transmission',
                    f"{n_trous} coupure(s) GPRS réparties sur le réseau"))
    return journal


def charger_mis_reels(motif):
    """Lit les vrais fichiers .MIS et les prépare pour la base.

    Retourne (mesures, stations, fichiers, fin_reelle) où `mesures` est une
    liste de tuples prêts pour la table `mesures`. Les données passent par
    analyse_serie() — exactement la même chaîne que le dashboard — pour que
    les corrections (R8 pluie) et les colonnes value_raw/corrected soient
    cohérentes avec une ingestion normale.

    `motif` accepte `**` pour descendre dans les sous-dossiers, ex. :
        "hydras/**/*.MIS"
    Un fichier illisible (corrompu, tronqué) est ignoré avec un avertissement
    plutôt que de faire échouer tout le chargement.
    """
    try:
        from mis_parser import parse_mis, analyse_serie
    except ImportError:
        sys.exit("mis_parser.py doit être dans le même dossier que ce script.")

    fichiers = sorted(glob.glob(motif, recursive=True))
    if not fichiers:
        sys.exit(f"Aucun fichier .MIS trouvé pour le motif : {motif}")

    blocs, ignores = [], []
    for f in fichiers:
        try:
            blocs.extend(parse_mis(f))
        except Exception as e:
            ignores.append((f, f"{type(e).__name__}: {e}"))
    if ignores:
        print(f"  ⚠ {len(ignores)} fichier(s) .MIS illisible(s), ignoré(s) :")
        for f, err in ignores[:10]:
            print(f"      ✗ {f} — {err}")
        if len(ignores) > 10:
            print(f"      … et {len(ignores) - 10} autre(s).")
    if not blocs:
        sys.exit("Les fichiers .MIS ne contiennent aucune donnée exploitable.")

    base = pd.concat(blocs, ignore_index=True)
    base = (base.drop_duplicates(subset=['station', 'sensor', 'timestamp', 'value'])
                .sort_values(['station', 'sensor', 'timestamp'])
                .reset_index(drop=True))

    mesures, stations = [], set()
    for (st, se), g in base.groupby(['station', 'sensor']):
        g, _ = analyse_serie(g.reset_index(drop=True))
        g = g.dropna(subset=['value'])
        stations.add(str(st))
        for r in g.itertuples():
            mesures.append((str(st), str(se),
                            r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                            float(r.value), float(r.value_raw),
                            int(r.corrected)))

    fin_reelle = pd.Timestamp(base['timestamp'].max()).to_pydatetime()
    return mesures, stations, fichiers, fin_reelle


# ─────────────────────────────────────────────
# 7. ÉCRITURE DE LA BASE
# ─────────────────────────────────────────────
def ecrire_base(series, dates, chemin, meta, mesures_reelles=None,
                stations_reelles=None):
    if chemin.exists():
        chemin.unlink()
    con = sqlite3.connect(chemin)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mesures (
            station   TEXT,
            sensor    TEXT,
            timestamp TEXT,
            value     REAL,
            value_raw REAL,
            corrected INTEGER,
            PRIMARY KEY (station, sensor, timestamp)
        )""")
    # Traçabilité : ces tables n'existent PAS dans une base purement réelle.
    # Leur présence signale que la base contient des données simulées.
    con.execute("""
        CREATE TABLE IF NOT EXISTS provenance_synthetique (
            cle TEXT PRIMARY KEY, valeur TEXT)""")
    con.executemany("INSERT OR REPLACE INTO provenance_synthetique VALUES (?,?)",
                    list(meta.items()))

    # Provenance PAR STATION. Règle absolue : une station est soit entièrement
    # réelle, soit entièrement simulée — jamais un mélange des deux. Sans cette
    # table, plus personne ne saurait dans six mois quelle station croire.
    con.execute("""
        CREATE TABLE IF NOT EXISTS provenance_station (
            station TEXT PRIMARY KEY,
            origine TEXT NOT NULL CHECK (origine IN ('réel', 'simulé')))""")

    horodatages = [d.strftime('%Y-%m-%d %H:%M:%S') for d in dates]
    total_sim = 0
    stations_sim = set()
    for (station, capteur), valeurs in series.items():
        v = np.asarray(valeurs, dtype=float)
        ok = ~np.isnan(v)
        lignes = [(station, capteur, horodatages[i], float(v[i]),
                   float(v[i]), 0) for i in np.nonzero(ok)[0]]
        con.executemany("INSERT OR IGNORE INTO mesures VALUES (?,?,?,?,?,?)",
                        lignes)
        total_sim += len(lignes)
        stations_sim.add(station)

    total_reel = 0
    if mesures_reelles:
        con.executemany("INSERT OR IGNORE INTO mesures VALUES (?,?,?,?,?,?)",
                        mesures_reelles)
        total_reel = len(mesures_reelles)

    con.executemany("INSERT OR REPLACE INTO provenance_station VALUES (?,?)",
                    [(s, 'simulé') for s in sorted(stations_sim)]
                    + [(s, 'réel') for s in sorted(stations_reelles or ())])
    con.commit()
    con.execute("VACUUM")
    con.close()
    return total_sim, total_reel


# ─────────────────────────────────────────────
# 8. EXPORT .MIS (démonstration des règles instantanées)
# ─────────────────────────────────────────────
def exporter_mis(series, dates, stations, rng, n_stations=6, jours=3):
    """Écrit quelques fichiers .MIS au format Hydras 3, avec des anomalies
    INSTANTANÉES (R1-R9) que la base ne peut pas contenir : valeurs hors
    plage, horodatages dupliqués, codes d'état '---/[10]'."""
    D_MIS.mkdir(exist_ok=True)
    for ancien in D_MIS.glob('*.MIS'):        # sinon deux exécutions se mélangent
        ancien.unlink()
    pas_par_jour = int(24 * 60 / PAS_MIN)
    debut = max(len(dates) - jours * pas_par_jour, 0)

    codes = list(rng.choice(stations['code_station'].to_numpy(),
                            min(n_stations, len(stations)), replace=False))
    fichiers = []
    for code in codes:
        cles = sorted([k for k in series if k[0] == code], key=lambda k: k[1])
        if not cles:
            continue
        lignes = []
        for (_, capteur) in cles:
            lignes.append(f"<STATION>{code}</STATION><SENSOR>{capteur}</SENSOR>")
            v = np.asarray(series[(code, capteur)], dtype=float)
            for i in range(debut, len(dates)):
                if np.isnan(v[i]):
                    continue
                val = v[i]
                d = dates[i]
                r = rng.random()
                if r < 0.004:                       # R9 : capteur muet ponctuel
                    txt = '---/[10]'
                elif r < 0.007 and capteur == '0006':   # R2 : pluie hors plage
                    txt = f"{rng.uniform(26, 45):.1f}"
                elif r < 0.009 and capteur == '0001':   # R1 : valeur négative
                    txt = f"{-abs(rng.uniform(1, 20)):.1f}"
                else:
                    txt = f"{val:.2f}"
                lignes.append(f"{d:%Y%m%d};{d:%H:%M:%S};{txt}")
                if rng.random() < 0.002:            # R5 : horodatage dupliqué
                    lignes.append(f"{d:%Y%m%d};{d:%H:%M:%S};{txt}")

        f = D_MIS / f"DEMO_{code}_{dates[-1]:%Y%m%d}.MIS"
        f.write_text("\n".join(lignes), encoding='utf-8')
        fichiers.append(f)
    return fichiers


# ─────────────────────────────────────────────
# 9. FICHE DE TRAÇABILITÉ
# ─────────────────────────────────────────────
def ecrire_fiche(meta, journal, stats, chemin=F_FICHE):
    lignes = [
        "# ⚠️ Données synthétiques — fiche de traçabilité",
        "",
        "**Les mesures contenues dans `historique_demo.db` sont entièrement "
        "SIMULÉES.** Elles ne proviennent d'aucun capteur du réseau DGRE. "
        "Elles servent uniquement à démontrer et tester le prototype de "
        "détection d'anomalies en l'absence de données réelles.",
        "",
        "Ne jamais utiliser cette base pour une analyse hydrologique, un "
        "bilan de ressource ou une décision d'exploitation.",
        "",
        "## Paramètres de génération",
        "",
        "| Paramètre | Valeur |",
        "| --- | --- |",
    ]
    for k, v in meta.items():
        lignes.append(f"| {k} | {v} |")

    lignes += [
        "",
        "## Ce qui est réel dans cette simulation",
        "",
        "- Les **codes, noms, gouvernorats, types et coordonnées** des stations "
        "proviennent de `config_stations.csv` (réseau DGRE réel).",
        "- Le **graphe amont-aval** (propagation des crues) provient de "
        "`reseau_stations.csv`, construit par `reseau.py` sur les couches SIG "
        "réelles.",
        "- Les **seuils min/max** par station sont ceux du fichier de "
        "configuration.",
        "- Si des fichiers `.MIS` réels ont été fournis (option `--reels`), "
        "les **mesures des stations concernées sont authentiques**.",
        "",
        "### Règle de séparation (essentielle)",
        "",
        "Une station est **soit entièrement réelle, soit entièrement simulée** "
        "— jamais un mélange. Une station réelle n'est jamais « complétée » "
        "par de la simulation, même sur les périodes où elle n'a pas transmis. "
        "La table `provenance_station` de la base donne l'origine de chacune :",
        "",
        "```sql",
        "SELECT origine, COUNT(*) FROM provenance_station GROUP BY origine;",
        "SELECT station FROM provenance_station WHERE origine = 'réel';",
        "```",
        "",
        "Tout le reste — pluies, cotes, débits, tensions des stations simulées "
        "— est produit par les modèles ci-dessous.",
        "",
        "## Modèles utilisés",
        "",
        "| Grandeur | Modèle |",
        "| --- | --- |",
        "| Pluie (0006) | Cellules orageuses (centre, rayon, durée, intensité "
        "exponentielle), saisonnalité tunisienne, gradient nord-sud |",
        "| Débit (0007) | Hydrogramme unitaire (loi gamma) + tarissement "
        "exponentiel + propagation amont→aval décalée |",
        "| Cote (0001) | Courbe de tarage inversée h = a·Q^b, bornée aux seuils |",
        "| Batterie (0002) | Cycle solaire diurne + modulation saisonnière + bruit |",
        "",
        "## Anomalies injectées volontairement",
        "",
        "Ce tableau est la **vérité terrain** de la simulation : chaque ligne "
        "correspond à une anomalie que les règles du prototype doivent "
        "retrouver. Il permet de mesurer le taux de détection.",
        "",
        "| Station | Capteur | Anomalie visée | Détail |",
        "| --- | --- | --- | --- |",
    ]
    for st, capt, typ, det in journal:
        lignes.append(f"| {st} | {capt} | {typ} | {det} |")

    lignes += [
        "",
        "## Volumétrie produite",
        "",
        "| Indicateur | Valeur |",
        "| --- | --- |",
    ]
    for k, v in stats.items():
        lignes.append(f"| {k} | {v} |")

    lignes += [
        "",
        "## Limite à mentionner dans le rapport",
        "",
        "Les règles long terme (L1-L6) et inter-stations n'ont été validées "
        "que sur ces données simulées. Leur calibrage (seuils, fenêtres) devra "
        "être repris dès que l'historique réel de la DGRE sera disponible : "
        "une simulation ne reproduit ni les défauts de capteur propres à "
        "chaque site, ni les artefacts de transmission observés en "
        "exploitation.",
        "",
    ]
    chemin.write_text("\n".join(lignes), encoding='utf-8')


# ─────────────────────────────────────────────
# 10. PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────
def main():
    global PAS_MIN
    ap = argparse.ArgumentParser(
        description="Génère une base de démonstration pour le prototype DGRE.")
    ap.add_argument('--jours', type=int, default=JOURS,
                    help=f"durée simulée en jours (défaut {JOURS})")
    ap.add_argument('--pas', type=int, default=PAS_MIN,
                    help=f"pas de temps en minutes (défaut {PAS_MIN})")
    ap.add_argument('--graine', type=int, default=GRAINE,
                    help=f"graine aléatoire (défaut {GRAINE})")
    ap.add_argument('--sortie', type=Path, default=F_SORTIE,
                    help="chemin de la base à écrire")
    ap.add_argument('--mis', action='store_true',
                    help="exporte aussi des fichiers .MIS de démonstration")
    ap.add_argument('--reels', type=str, default=None,
                    help="motif des VRAIS fichiers .MIS à intégrer, "
                         "ex. \"mis_reels/*.MIS\". Les stations qu'ils "
                         "couvrent ne sont PAS simulées.")
    args = ap.parse_args()

    PAS_MIN = args.pas
    rng = np.random.default_rng(args.graine)

    print("⚠️  GÉNÉRATION DE DONNÉES SIMULÉES — usage démonstration uniquement\n")

    stations = charger_stations()
    liens = charger_reseau(set(stations['code_station']))
    print(f"  {len(stations)} stations réelles · {len(liens)} liens amont-aval directs")

    # ── Données réelles éventuelles ──
    mesures_reelles, stations_reelles, fichiers_reels = [], set(), []
    fin = datetime.now().replace(minute=0, second=0, microsecond=0)
    if args.reels:
        mesures_reelles, stations_reelles, fichiers_reels, fin_reelle = \
            charger_mis_reels(args.reels)
        # La période simulée se termine en même temps que les données réelles.
        # Sinon la règle L3 déclarerait « muettes » les stations réelles, dont
        # la dernière mesure serait vieille de plusieurs mois face au reste
        # du réseau simulé.
        fin = fin_reelle.replace(minute=0, second=0, microsecond=0)
        print(f"  {len(fichiers_reels)} fichier(s) .MIS réel(s) · "
              f"{len(stations_reelles)} station(s) réelle(s) : "
              f"{', '.join(sorted(stations_reelles))}")
        print(f"  {len(mesures_reelles)} mesure(s) réelle(s), "
              f"dernière le {fin_reelle:%d/%m/%Y %H:%M}")

    n_pas = int(args.jours * 24 * 60 / PAS_MIN)
    dates = [fin - timedelta(minutes=PAS_MIN * (n_pas - 1 - i))
             for i in range(n_pas)]
    print(f"  période simulée : {dates[0]:%d/%m/%Y} → {dates[-1]:%d/%m/%Y} "
          f"({n_pas} pas de {PAS_MIN} min)\n")

    series = construire(stations, liens, dates, rng)

    # Une station réelle n'est JAMAIS complétée par de la simulation : on
    # retire toutes ses séries simulées. Elle reste utilisée en interne pour
    # la propagation amont-aval, mais rien de simulé n'est écrit pour elle.
    if stations_reelles:
        for cle in [k for k in series if k[0] in stations_reelles]:
            del series[cle]

    print("  injection des anomalies de référence…")
    journal = injecter_anomalies(series, stations, dates, rng)

    meta = {
        'nature': ('BASE MIXTE — stations réelles + stations simulées'
                   if stations_reelles else
                   'DONNÉES SIMULÉES — aucune mesure réelle'),
        'genere_le': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'script': 'donnees_synthetiques.py',
        'graine': str(args.graine),
        'periode_simulee': f"{dates[0]:%Y-%m-%d} → {dates[-1]:%Y-%m-%d}",
        'pas_de_temps_min': str(PAS_MIN),
        'stations_simulees': str(len(stations) - len(stations_reelles)),
        'stations_reelles': (', '.join(sorted(stations_reelles))
                             if stations_reelles else 'aucune'),
        'fichiers_mis_reels': str(len(fichiers_reels)),
        'liens_amont_aval': str(len(liens)),
    }

    print("  écriture de la base…")
    total_sim, total_reel = ecrire_base(series, dates, args.sortie, meta,
                                        mesures_reelles, stations_reelles)
    taille_mo = args.sortie.stat().st_size / 1024 / 1024

    stats = {
        'mesures simulées': f"{total_sim:,}".replace(',', ' '),
        'mesures réelles': f"{total_reel:,}".replace(',', ' '),
        'séries station-capteur simulées': str(len(series)),
        'taille du fichier': f"{taille_mo:.1f} Mo",
    }

    fichiers_mis = []
    if args.mis:
        print("  export des fichiers .MIS de démonstration…")
        fichiers_mis = exporter_mis(series, dates, stations, rng)
        stats['fichiers .MIS'] = str(len(fichiers_mis))

    ecrire_fiche(meta, journal, stats)

    total = total_sim + total_reel
    print(f"\n💾 {args.sortie.name} — {total:,} mesures, {taille_mo:.1f} Mo"
          .replace(',', ' '))
    if stations_reelles:
        print(f"   dont {total_reel:,} RÉELLES ({len(stations_reelles)} station(s)) "
              f"et {total_sim:,} simulées".replace(',', ' '))
        print("   → table provenance_station : quelle station croire")
    print(f"💾 {F_FICHE.name} — fiche de traçabilité + vérité terrain")
    if fichiers_mis:
        print(f"💾 {D_MIS.name}/ — {len(fichiers_mis)} fichier(s) .MIS à "
              "déposer dans le dashboard")
    print(f"\n{len(journal)} anomalie(s) de référence injectée(s) — "
          "voir la fiche pour la liste complète.")


if __name__ == '__main__':
    main()
