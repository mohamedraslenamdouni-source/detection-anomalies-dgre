"""
Mémoire long terme — base SQLite qui accumule l'historique de chaque station
============================================================================
Principe :
  1. Chaque fichier MIS analysé est AJOUTÉ à la base historique.db
     (les doublons sont ignorés automatiquement grâce à la clé primaire)
  2. Les règles "long terme" tournent sur l'HISTORIQUE COMPLET de chaque
     station — elles voient ce qu'un fichier seul ne peut pas montrer.

Règles long terme :
  L1  Flatline longue   : valeur bloquée depuis ≥ 24 h (cote, débit ≠ 0)
  L2  Dérive batterie   : pente de décharge sur 7 j + date prévue du 8 V
  L3  Station muette    : plus de données depuis ≥ 6 h (vs reste du réseau)
  L4  Valeur inhabituelle : mesure récente très éloignée de l'historique
  L7  Pluie sans réponse : croisement des DEUX capteurs d'une même station
      (pluviomètre × cote) — pluie significative sans réaction de la rivière

Utilisation en ligne de commande :
  python memoire.py fichier1.MIS fichier2.MIS   (ou *.MIS)
"""

import glob
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from mis_parser import (SENSOR_DEFAULTS, BATTERY, STATION_NAMES,
                        station_label, parse_mis, analyse_serie)

# Base par défaut : historique.db (données réelles).
# La variable d'environnement DGRE_DB permet de basculer sur une autre base —
# typiquement historique_demo.db — sans jamais mélanger données réelles et
# données simulées :
#     PowerShell :  $env:DGRE_DB = "historique_demo.db"
DB_PATH = Path(os.environ.get('DGRE_DB') or (Path(__file__).parent / 'historique.db'))

# Seuils des règles long terme (modifiables)
LT = {
    # Flatline : calibré sur 1 an de données réelles Mellegue K13, où des paliers
    # NATURELS de 42 h existent en étiage (résolution 1 cm) → seuil 72 h pour
    # cote/débit afin d'éviter les fausses alertes. 24 h pour le reste.
    'flatline_h':      {'0001': 72, '0007': 72, 'defaut': 24},
    'silence_h':        6,     # h sans données → station muette
    'derive_v_jour':  0.05,    # V/jour de décharge batterie → alerte
    'derive_fenetre_j':  7,    # fenêtre d'analyse de la dérive (jours)
    'outlier_k':         6,    # sensibilité de la règle "valeur inhabituelle"
    'outlier_min_pts': 100,    # historique minimal pour la règle L4
    'tendance_r2':     0.7,    # qualité d'ajustement minimale pour la règle L5
    'rupture_p':      0.01,
    # Fenêtre de lecture des règles bornées (L1, L2, L2b, L4, L5). 120 j >
    # au plus large de leurs besoins (~30 j), avec une marge confortable.
    # None = relire tout l'historique (ancien comportement).
    'fenetre_j':       120,    # seuil de significativité du test de Pettitt (L6)

    # ── L7 : pluie sans réponse de la rivière (croisement 0006 × 0001) ──
    # Règle MONOSTATION mais CROISÉE : elle compare deux capteurs d'une MÊME
    # station (pluviomètre et capteur de cote), pas deux stations.
    'l7_pluie_mm':      10.0,  # cumul sur la fenêtre de pluie à partir duquel
    #                            une réaction de la rivière est attendue.
    #                            ⚠ à valider avec la DGRE : dépend du bassin,
    #                            de la pente et de l'état du sol.
    'l7_fenetre_pluie_h': 24,  # durée sur laquelle on cumule la pluie
    'l7_reponse_h':       36,  # délai max accordé à la rivière pour réagir
    'l7_montee_min_cm':  3.0,  # montée de cote considérée comme une réaction
    'l7_secheresse_j':    20,  # si la station est sèche depuis plus longtemps,
    #                            le sol absorbe tout : on s'abstient
    'l7_deja_haut':      1.5,  # cote déjà > médiane + 1.5×(médiane-min) →
    #                            rivière déjà en crue, verdict impossible
}


# ─────────────────────────────────────────────
# 1. LA BASE DE DONNÉES
# ─────────────────────────────────────────────

def connexion(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Ouvre (et crée si besoin) la base historique."""
    con = sqlite3.connect(db_path, check_same_thread=False)
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
    con.commit()
    return con


def ajouter_mesures(df: pd.DataFrame, con: sqlite3.Connection) -> int:
    """Ajoute les mesures à la mémoire. Les doublons (même station, capteur,
    horodatage) sont ignorés. Retourne le nombre de lignes réellement ajoutées."""
    df = df.dropna(subset=['value'])   # les lignes "code d'état" (---/[10])
    #                                      ne sont pas des mesures → pas stockées
    rows = [(r.station, r.sensor, r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
             float(r.value), float(r.value_raw), int(r.corrected))
            for r in df.itertuples()]
    cur = con.executemany(
        "INSERT OR IGNORE INTO mesures VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    return cur.rowcount


def charger_serie(station: str, sensor: str, con,
                  fenetre_j: int = None) -> pd.DataFrame:
    """Recharge l'historique d'une série station-capteur, trié dans le temps.

    `fenetre_j` limite la lecture aux N derniers jours de la série (comptés
    depuis SA dernière mesure, pas depuis la date du jour : une station muette
    depuis un mois doit rester analysable). Sans fenêtre, tout est chargé.

    Pourquoi : les règles L1, L2, L2b, L4 et L5 raisonnent sur des fenêtres
    bornées par construction (7 à 30 jours). Leur faire relire plusieurs années
    d'historique à chaque appel rend le temps de calcul proportionnel à l'âge
    de la base, alors que le résultat, lui, ne change pas.
    """
    if not fenetre_j:
        return pd.read_sql_query(
            "SELECT * FROM mesures WHERE station=? AND sensor=? ORDER BY timestamp",
            con, params=(station, sensor), parse_dates=['timestamp'])
    return pd.read_sql_query(
        """SELECT * FROM mesures
           WHERE station = ? AND sensor = ?
             AND timestamp >= datetime(
                   (SELECT MAX(timestamp) FROM mesures WHERE station=? AND sensor=?),
                   ?)
           ORDER BY timestamp""",
        con, params=(station, sensor, station, sensor, f'-{int(fenetre_j)} days'),
        parse_dates=['timestamp'])


# Médiane journalière exacte calculée par SQLite : pour un nombre pair de
# valeurs dans la journée, moyenne des deux valeurs centrales — exactement la
# convention de pandas .median(). Vérifié identique au dixième de millième.
_SQL_MEDIANES_JOUR = """
SELECT jour, AVG(value) AS mediane FROM (
  SELECT date(timestamp) AS jour, value,
         ROW_NUMBER() OVER (PARTITION BY date(timestamp) ORDER BY value) AS rn,
         COUNT(*)     OVER (PARTITION BY date(timestamp))               AS cnt
  FROM mesures WHERE station = ? AND sensor = ?
) WHERE rn IN ((cnt+1)/2, (cnt+2)/2)
GROUP BY jour ORDER BY jour"""


def medianes_journalieres(station: str, sensor: str, con) -> pd.Series:
    """Médianes journalières sur TOUT l'historique, agrégées côté SQLite.

    La règle L6 (test de Pettitt) est définie sur la série entière : la
    tronquer changerait le point de rupture trouvé, donc le verdict. Mais elle
    n'a besoin que d'une valeur par jour. Agréger dans SQLite renvoie ~365
    lignes par an au lieu de plusieurs milliers, sans rien changer au résultat.
    """
    d = pd.read_sql_query(_SQL_MEDIANES_JOUR, con, params=(station, sensor),
                          parse_dates=['jour'])
    if d.empty:
        return pd.Series(dtype=float)
    return d.set_index('jour')['mediane'].dropna()


def series_presentes(con) -> list[tuple]:
    return con.execute(
        "SELECT DISTINCT station, sensor FROM mesures ORDER BY station, sensor"
    ).fetchall()


def stats_memoire(con) -> dict:
    row = con.execute("""SELECT COUNT(*), COUNT(DISTINCT station),
                                MIN(timestamp), MAX(timestamp) FROM mesures""").fetchone()
    return {'mesures': row[0], 'stations': row[1], 'debut': row[2], 'fin': row[3]}


# ─────────────────────────────────────────────
# 2. RÈGLES LONG TERME
# ─────────────────────────────────────────────

def _flatline_en_cours(df):
    """Durée (en h) du palier de valeur constante à la FIN de la série."""
    v = df['value'].values
    i = len(v) - 1
    while i > 0 and v[i - 1] == v[i]:
        i -= 1
    debut = df['timestamp'].iloc[i]
    duree_h = (df['timestamp'].iloc[-1] - debut).total_seconds() / 3600
    return duree_h, v[-1], debut


def _pettitt(x):
    """Test de Pettitt : trouve le point de rupture le plus probable d'une série.
    Retourne (index_rupture, statistique_K, p_value_approx)."""
    n = len(x)
    r = pd.Series(x).rank().values
    cs = np.cumsum(r)
    t = np.arange(1, n)
    U = 2 * cs[:-1] - t * (n + 1)
    K = float(np.max(np.abs(U)))
    idx = int(np.argmax(np.abs(U)))
    p = 2.0 * np.exp(-6.0 * K ** 2 / (n ** 3 + n ** 2))
    return idx, K, min(p, 1.0)


def analyser_long_terme(station: str, sensor: str, con) -> list[dict]:
    """Applique les règles L1, L2, L4 sur l'historique complet d'une série."""
    df = charger_serie(station, sensor, con, fenetre_j=LT.get('fenetre_j'))
    anomalies = []
    if len(df) < 3:
        return anomalies

    base = SENSOR_DEFAULTS.get(sensor, {})

    # ── L1 : flatline longue (cote toujours, débit seulement si ≠ 0) ──
    if sensor in ('0001', '0007'):
        seuil_h = LT['flatline_h'].get(sensor, LT['flatline_h']['defaut'])
        duree_h, val, debut = _flatline_en_cours(df)
        if duree_h >= seuil_h and not (sensor == '0007' and val == 0):
            anomalies.append({
                'type': 'flatline_longue', 'severity': 'high',
                'description': (f"Valeur bloquée à {val} depuis "
                                f"{duree_h:.0f} h (depuis le {debut:%d/%m %H:%M}) "
                                f"— capteur probablement en panne"),
                'timestamp': debut,
            })

    # ── L2 : dérive de la batterie + prévision ──
    if base.get('battery'):
        fin = df['timestamp'].max()
        fen = df[df['timestamp'] >= fin - pd.Timedelta(days=LT['derive_fenetre_j'])]
        assez_long = (fen['timestamp'].max() - fen['timestamp'].min()
                      ) >= pd.Timedelta(days=2)
        if len(fen) >= 10 and assez_long:
            t = (fen['timestamp'] - fen['timestamp'].min()).dt.total_seconds() / 86400
            pente = float(np.polyfit(t, fen['value'], 1)[0])   # V / jour
            if pente <= -LT['derive_v_jour']:
                v_now = float(fen['value'].iloc[-1])
                j8  = (v_now - BATTERY['non_retour']) / (-pente)
                msg = (f"Décharge progressive : {pente:.2f} V/jour sur "
                       f"{LT['derive_fenetre_j']} j (actuellement {v_now} V). ")
                if v_now > BATTERY['alerte']:
                    j11 = (v_now - BATTERY['alerte']) / (-pente)
                    msg += f"Seuil d'alerte ({BATTERY['alerte']} V) dans ~{j11:.0f} j. "
                msg += (f"Point de non-retour ({BATTERY['non_retour']} V) "
                        f"dans ~{j8:.0f} jour(s) si rien n'est fait.")
                anomalies.append({
                    'type': 'batterie_derive', 
                    'severity': 'high' if j8 <= 14 else 'medium',
                    'description': "📉 " + msg,
                    'timestamp': fin,
                })

    # ── L2b : batterie qui ne se RECHARGE plus (panneau solaire/régulateur) ──
    # Une batterie saine oscille : ~12 V la nuit, 13-15 V en journée (charge solaire).
    # Si elle reste bloquée à ≤ 12 V pendant des jours, la charge ne se fait plus —
    # la décharge finale n'est qu'une question de temps.
    if base.get('battery'):
        fin = df['timestamp'].max()
        fen  = df[df['timestamp'] >= fin - pd.Timedelta(days=7)]
        hist = df[df['timestamp'] <  fin - pd.Timedelta(days=7)]
        seuil_charge = BATTERY['nominal'] + 0.5
        if (len(fen) >= 10 and len(hist) >= 50
                and fen['value'].max() < seuil_charge
                and (hist['value'] >= seuil_charge).mean() > 0.10):
            depuis = df[df['value'] >= seuil_charge]['timestamp'].max()
            anomalies.append({
                'type': 'batterie_charge_absente', 'severity': 'medium',
                'description': (f"🔌 La batterie ne dépasse plus {fen['value'].max()} V "
                                f"depuis 7 jours (dernière charge ≥ {seuil_charge} V : "
                                f"{depuis:%d/%m %H:%M}) — le panneau solaire ou le "
                                f"régulateur ne recharge probablement plus"),
                'timestamp': fin,
            })

    # ── L5 : tendance continue sur 7 jours (dérive capteur OU évolution réelle) ──
    if sensor in ('0001', '0007'):
        fin  = df['timestamp'].max()
        fen  = df[df['timestamp'] >= fin - pd.Timedelta(days=LT['derive_fenetre_j'])]
        hist = df[df['timestamp'] <  fin - pd.Timedelta(days=LT['derive_fenetre_j'])]
        span_j = (fen['timestamp'].max() - fen['timestamp'].min()).total_seconds() / 86400
        if len(fen) >= 30 and span_j >= 4 and len(hist) >= 200:
            t = (fen['timestamp'] - fen['timestamp'].min()).dt.total_seconds() / 86400
            pente, b0 = np.polyfit(t, fen['value'], 1)
            pred   = pente * t + b0
            ss_res = float(((fen['value'] - pred) ** 2).sum())
            ss_tot = float(((fen['value'] - fen['value'].mean()) ** 2).sum())
            r2     = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
            delta  = pente * span_j
            med_h  = hist['value'].median()
            mad    = 1.4826 * (hist['value'] - med_h).abs().median()
            etendue = float(hist['value'].max() - hist['value'].min())
            if r2 >= LT['tendance_r2'] and abs(delta) > max(5 * mad, 0.1 * etendue):
                anomalies.append({
                    'type': 'tendance_continue', 'severity': 'medium',
                    'description': (f"📈 Évolution continue sur {LT['derive_fenetre_j']} j : "
                                    f"{delta:+.1f} au total ({pente:+.2f}/jour, R²={r2:.2f}) "
                                    f"— dérive du capteur possible OU évolution "
                                    f"hydrologique réelle (décrue, remplissage) : à vérifier"),
                    'timestamp': fin,
                })

    # ── L6 : rupture de moyenne (test de Pettitt sur les médianes journalières) ──
    if sensor in ('0001', '0007'):
        # NB : volontairement sur TOUT l'historique, pas sur la fenêtre de
        # `df` — un test de rupture tronqué désignerait une autre rupture.
        daily = medianes_journalieres(station, sensor, con)
        if len(daily) >= 60:
            idx, K, p = _pettitt(daily.values)
            if p < LT['rupture_p'] and 15 <= idx <= len(daily) - 15:
                m1 = float(daily.iloc[:idx + 1].median())
                m2 = float(daily.iloc[idx + 1:].median())
                # La dispersion doit être mesurée À L'INTÉRIEUR de chaque
                # segment. Un MAD calculé sur toute la série est gonflé par la
                # rupture elle-même : plus la rupture est nette, plus le seuil
                # monte, et la règle finit par ne jamais se déclencher.
                mad1 = 1.4826 * (daily.iloc[:idx + 1] - m1).abs().median()
                mad2 = 1.4826 * (daily.iloc[idx + 1:] - m2).abs().median()
                mad_d = (mad1 + mad2) / 2
                if abs(m2 - m1) > max(2 * mad_d, 0.05 * float(daily.max() - daily.min())):
                    date_r = daily.index[idx]
                    anomalies.append({
                        'type': 'rupture_moyenne', 'severity': 'medium',
                        'description': (f"📊 Changement durable du niveau moyen autour du "
                                        f"{date_r:%d/%m/%Y} : médiane {m1:.1f} avant → "
                                        f"{m2:.1f} après (test de Pettitt, p={p:.4f}) — "
                                        f"recalibration/déplacement du capteur possible OU "
                                        f"changement de régime réel (saison, barrage)"),
                        'timestamp': date_r,
                    })

    # ── L4 : valeur récente inhabituelle par rapport à l'historique ──
    if sensor in ('0001', '0007'):
        fin = df['timestamp'].max()
        recent = df[df['timestamp'] >  fin - pd.Timedelta(hours=24)]
        hist   = df[df['timestamp'] <= fin - pd.Timedelta(hours=24)]
        if len(hist) >= LT['outlier_min_pts'] and len(recent):
            med = hist['value'].median()
            mad = (hist['value'] - med).abs().median()
            if mad > 0:
                k = LT['outlier_k'] * 1.4826 * mad
                out = recent[(recent['value'] > med + k) | (recent['value'] < med - k)]
                if len(out):
                    vmax = out['value'].iloc[out['value'].sub(med).abs().argmax()]
                    anomalies.append({
                        'type': 'valeur_inhabituelle', 'severity': 'medium',
                        'description': (f"{len(out)} mesure(s) récente(s) très "
                                        f"éloignée(s) de l'historique (ex : {vmax} "
                                        f"vs médiane {med:.1f}) — possible événement "
                                        f"réel (crue) OU erreur capteur : à vérifier"),
                        'timestamp': out['timestamp'].iloc[0],
                    })
    return anomalies


def origine_stations(con) -> dict:
    """{code_station: 'réel'|'simulé'} si la base contient des données simulées.

    Retourne un dictionnaire VIDE pour une base 100 % réelle (la table
    provenance_station n'existe alors pas). Permet au dashboard d'afficher
    clairement quelles stations sont authentiques.
    """
    try:
        return dict(con.execute(
            "SELECT station, origine FROM provenance_station").fetchall())
    except sqlite3.OperationalError:
        return {}


def analyser_croisement_pluie_cote(station: str, con) -> list[dict]:
    """L7 — Pluie sans réponse de la rivière (croisement 0006 × 0001).

    Règle MONOSTATION CROISÉE : elle confronte les DEUX capteurs d'une même
    station, sans faire intervenir aucune autre station.

    Principe physique : quand il tombe assez de pluie sur le bassin, l'eau
    ruisselle et la cote monte quelques heures plus tard. Si une pluie
    significative est enregistrée et que la rivière ne bouge pas du tout,
    l'un des deux capteurs ment :
      • soit le capteur de cote est bloqué (la crue a eu lieu, il ne l'a
        pas vue) ;
      • soit le pluviomètre invente de la pluie (augets qui basculent à
        vide, toile d'araignée, insecte).
    La règle ne dit PAS lequel des deux est en cause — elle signale
    l'incohérence, le technicien tranche sur place.

    Abstentions (aucun verdict rendu) :
      • pluie insuffisante pour espérer une réaction ;
      • sol sec depuis longtemps : la première pluie s'infiltre sans
        ruisseler, surtout en climat aride ;
      • rivière DÉJÀ en crue : une nouvelle montée y est indétectable ;
      • cote absente ou trop lacunaire sur la fenêtre de réponse.

    Retourne la liste des incohérences détectées.
    """
    pluie = charger_serie(station, '0006', con)
    cote = charger_serie(station, '0001', con)
    if len(pluie) < 10 or len(cote) < 50:
        return []

    p = pluie.set_index('timestamp')['value'].sort_index()
    c = cote.set_index('timestamp')['value'].sort_index()

    # cumuls de pluie glissants sur la fenêtre choisie
    fen = f"{LT['l7_fenetre_pluie_h']}h"
    cumul = p.rolling(fen).sum()

    med_c = float(c.median())
    min_c = float(c.min())
    seuil_deja_haut = med_c + LT['l7_deja_haut'] * (med_c - min_c)

    anomalies = []
    dernier_signale = None

    for t, mm in cumul.items():
        if pd.isna(mm) or mm < LT['l7_pluie_mm']:
            continue
        # un seul signalement par épisode : on saute ce qui suit de près
        if dernier_signale is not None and \
                (t - dernier_signale) < pd.Timedelta(hours=LT['l7_reponse_h']):
            continue

        debut_pluie = t - pd.Timedelta(hours=LT['l7_fenetre_pluie_h'])

        # ── abstention 1 : sol sec depuis très longtemps ──
        avant = p[p.index < debut_pluie]
        if len(avant):
            derniere_pluie = avant[avant > 0]
            if len(derniere_pluie):
                jours_sec = (debut_pluie
                             - derniere_pluie.index[-1]).total_seconds() / 86400
                if jours_sec > LT['l7_secheresse_j']:
                    continue          # sol desséché : tout s'infiltre

        # ── niveau de référence : cote AVANT la pluie ──
        ref = c[(c.index >= debut_pluie - pd.Timedelta(hours=12))
                & (c.index <= debut_pluie)]
        if len(ref) < 3:
            continue                  # pas de référence fiable
        base = float(ref.median())

        # ── abstention 2 : rivière déjà en crue ──
        if base > seuil_deja_haut:
            continue

        # ── réponse de la rivière après la pluie ──
        rep = c[(c.index > debut_pluie)
                & (c.index <= t + pd.Timedelta(hours=LT['l7_reponse_h']))]
        if len(rep) < 5:
            continue                  # cote trop lacunaire pour juger

        montee = float(rep.max()) - base
        if montee >= LT['l7_montee_min_cm']:
            continue                  # la rivière a réagi : cohérent

        anomalies.append({
            'type': 'pluie_sans_reponse',
            'severity': 'high' if mm >= 2 * LT['l7_pluie_mm'] else 'medium',
            'description': (
                f"🌧️↔📏 {mm:.1f} mm de pluie cumulés en "
                f"{LT['l7_fenetre_pluie_h']} h (jusqu'au {t:%d/%m %H:%M}) "
                f"mais la cote n'a pas bougé : {base:.1f} cm avant, "
                f"maximum {float(rep.max()):.1f} cm dans les "
                f"{LT['l7_reponse_h']} h suivantes (montée {montee:+.1f} cm, "
                f"seuil {LT['l7_montee_min_cm']:.0f} cm). "
                f"Incohérence entre les deux capteurs de la station : "
                f"soit le capteur de cote est bloqué, soit le pluviomètre "
                f"enregistre une pluie qui n'a pas eu lieu."),
            'timestamp': t,
        })
        dernier_signale = t

    return anomalies


def detecter_silences(con, seuil_h: float = None) -> list[dict]:
    """L3 : séries dont la dernière donnée est bien plus vieille que le reste du réseau."""
    seuil_h = seuil_h or LT['silence_h']
    stats = stats_memoire(con)
    if not stats['fin']:
        return []
    ref = pd.Timestamp(stats['fin'])   # dernière donnée reçue, tout le réseau
    silences = []
    for station, sensor in series_presentes(con):
        last = pd.Timestamp(con.execute(
            "SELECT MAX(timestamp) FROM mesures WHERE station=? AND sensor=?",
            (station, sensor)).fetchone()[0])
        retard_h = (ref - last).total_seconds() / 3600
        if retard_h >= seuil_h:
            silences.append({
                'station': station, 'sensor': sensor,
                'type': 'station_muette', 'severity': 'high',
                'description': (f"Plus aucune donnée depuis {retard_h:.0f} h "
                                f"(dernière mesure : {last:%d/%m %H:%M}, alors que "
                                f"le réseau a transmis jusqu'au {ref:%d/%m %H:%M})"),
                'timestamp': last,
            })
    return silences


# ─────────────────────────────────────────────
# 3. INGESTION D'UN FICHIER (analyse instantanée + ajout mémoire)
# ─────────────────────────────────────────────

def ingester_fichier(filepath: str, con):
    """Analyse un fichier MIS (règles instantanées) puis l'ajoute à la mémoire.
    Retourne (résultats_par_série, nb_nouvelles_mesures)."""
    resultats, nouvelles = [], 0
    for df in parse_mis(filepath):
        df, anomalies = analyse_serie(df)
        nouvelles += ajouter_mesures(df, con)
        resultats.append({'station': df['station'].iloc[0],
                          'sensor':  df['sensor'].iloc[0],
                          'df': df, 'anomalies': anomalies})
    return resultats, nouvelles


# ─────────────────────────────────────────────
# 4. LIGNE DE COMMANDE
# ─────────────────────────────────────────────

if __name__ == '__main__':
    # Supporte les jokers même sous Windows PowerShell : python memoire.py *.MIS
    fichiers = []
    for a in sys.argv[1:]:
        fichiers += glob.glob(a) if ('*' in a or '?' in a) else [a]

    con = connexion()

    for fp in fichiers:
        resultats, nouvelles = ingester_fichier(fp, con)
        print(f"\n📄 {Path(fp).name} → {nouvelles} nouvelle(s) mesure(s) en mémoire")
        for r in resultats:
            for a in r['anomalies']:
                icon = {'high': '🔴', 'medium': '🟡', 'low': '🔵'}.get(a['severity'], '❔')
                print(f"   {icon} {station_label(r['station'])} · {r['sensor']} : {a['description']}")

    s = stats_memoire(con)
    print(f"\n🧠 MÉMOIRE : {s['mesures']} mesures | {s['stations']} stations | "
          f"du {s['debut']} au {s['fin']}")

    print(f"\n{'='*62}\nANALYSE LONG TERME (sur tout l'historique)\n{'='*62}")
    rien = True
    for station, sensor in series_presentes(con):
        for a in analyser_long_terme(station, sensor, con):
            icon = {'high': '🔴', 'medium': '🟡'}.get(a['severity'], '🔵')
            name = SENSOR_DEFAULTS.get(sensor, {}).get('name', sensor)
            print(f"{icon} {station_label(station)} · {name}\n   {a['description']}")
            rien = False

    # L7 : croisement pluie × cote, une seule fois par station
    for station in sorted({s for s, _ in series_presentes(con)}):
        for a in analyser_croisement_pluie_cote(station, con):
            icon = {'high': '🔴', 'medium': '🟡'}.get(a['severity'], '🔵')
            print(f"{icon} {station_label(station)} · croisement pluie/cote"
                  f"\n   {a['description']}")
            rien = False

    for a in detecter_silences(con):
        name = SENSOR_DEFAULTS.get(a['sensor'], {}).get('name', a['sensor'])
        print(f"🔴 {station_label(a['station'])} · {name}\n   {a['description']}")
        rien = False
    if rien:
        print("✅ Aucune anomalie long terme détectée (l'historique est peut-être encore court)")
