"""
Parseur + validation monostation — fichiers MIS Hydras 3 (OTT HydroMet)
=======================================================================
Capteurs du réseau DGRE :
  0001 = Cote (hauteur d'eau)   0002 = Batterie
  0006 = Pluviométrie           0007 = Débit

La configuration PAR STATION (noms + seuils min/max) est lue depuis
config_stations.csv, placé dans le même dossier que ce script.
→ Ouvre-le dans Excel pour remplacer les seuils par défaut par les
  vraies valeurs de la DGRE (garde le séparateur point-virgule).

Utilisation :
  python mis_parser.py mon_fichier.MIS
"""

import re
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────
# 0a. CAPTEURS : valeurs par défaut (si la station n'a pas ses propres seuils)
# ─────────────────────────────────────────────
SENSOR_DEFAULTS = {
    '0001': {'name': "Cote (hauteur d'eau)", 'unit': 'cm',
             'min_valid': 0.0, 'max_valid': 2000.0, 'correct': False},
    '0002': {'name': 'Batterie', 'unit': 'V', 'battery': True},
    '0006': {'name': 'Pluviométrie', 'unit': 'mm/pas',
             'min_valid': 0.0, 'max_valid': 25.0, 'correct': True},
    '0007': {'name': 'Débit', 'unit': 'm³/s',
             'min_valid': 0.0, 'max_valid': 3000.0, 'correct': False},
}

# ─────────────────────────────────────────────
# 0b. BATTERIE : règles d'alerte (globales, valables pour toutes les stations)
#     État normal ≈ 12 V. Décharge à surveiller dès ≤ 11 V.
#     Point de non-retour : 8 V (batterie irrécupérable, station HS).
# ─────────────────────────────────────────────
BATTERY = {
    'nominal':    12.0,
    'alerte':     11.0,   # ≤ 11 V → décharge, il faut intervenir
    'non_retour':  8.0,   # ≤ 8 V → point de non-retour
    'surcharge':  15.0,   # > 15 V → régulateur de charge suspect
}

# ─────────────────────────────────────────────
# 0c. CONFIGURATION PAR STATION (config_stations.csv)
# ─────────────────────────────────────────────
CONFIG_CSV = Path(__file__).parent / 'config_stations.csv'

# correspondance capteur → colonnes du CSV
_CSV_COLS = {'0006': ('pluie_min', 'pluie_max'),
             '0001': ('cote_min',  'cote_max'),
             '0007': ('debit_min', 'debit_max')}

STATION_NAMES  = {}   # code → nom
STATION_INFO   = {}   # code → {'gouvernorat':…, 'type':…}
STATION_LIMITS = {}   # code → {capteur: (min, max)}


def load_station_config(path: Path = CONFIG_CSV):
    """Charge config_stations.csv s'il existe. Sinon, seuils par défaut."""
    names, info, limits = {}, {}, {}
    if path.exists():
        t = pd.read_csv(path, sep=';', encoding='utf-8-sig',
                        dtype={'code_station': str})
        for _, row in t.iterrows():
            code = str(row['code_station']).strip()
            names[code] = str(row.get('nom', code)).strip()
            info[code]  = {'gouvernorat': row.get('gouvernorat', ''),
                           'type': row.get('type_station', '')}
            lim = {}
            for sensor, (c_min, c_max) in _CSV_COLS.items():
                mn, mx = row.get(c_min), row.get(c_max)
                if pd.notna(mn) and pd.notna(mx):
                    lim[sensor] = (float(mn), float(mx))
            limits[code] = lim
    return names, info, limits


STATION_NAMES, STATION_INFO, STATION_LIMITS = load_station_config()


def station_label(code: str) -> str:
    """'1485100506' → '1485100506 (PT RTE SARRAT)'."""
    nom = STATION_NAMES.get(code)
    return f"{code} ({nom})" if nom else f"{code} (station hors liste)"


def get_limits(station: str, sensor: str):
    """Seuils de la station si définis dans le CSV, sinon défauts du capteur."""
    lim = STATION_LIMITS.get(station, {}).get(sensor)
    if lim:
        return lim
    base = SENSOR_DEFAULTS.get(sensor, {})
    if 'min_valid' in base:
        return (base['min_valid'], base['max_valid'])
    return None


# ─────────────────────────────────────────────
# 1. PARSEUR
# ─────────────────────────────────────────────

def parse_mis(filepath: str) -> list[pd.DataFrame]:
    """Parse un fichier .MIS et retourne une liste de DataFrames (un par capteur)."""
    blocks, current_meta, rows = [], {}, []

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('<STATION>'):
                if rows and current_meta:
                    blocks.append(_build_df(current_meta, rows))
                    rows = []
                station = re.search(r'<STATION>(.*?)</STATION>', line).group(1)
                sensor  = re.search(r'<SENSOR>(.*?)</SENSOR>',   line).group(1)
                current_meta = {'station': station, 'sensor': sensor}
            else:
                parts = line.split(';')
                if len(parts) == 3:
                    date_str, time_str, value_str = parts
                    try:
                        ts = datetime.strptime(date_str + time_str.replace(':', ''), '%Y%m%d%H%M%S')
                    except ValueError:
                        continue  # horodatage illisible → ligne ignorée
                    try:
                        value = float(value_str)
                        rows.append({'timestamp': ts, 'value': value, 'code': None})
                    except ValueError:
                        # Pas un nombre : c'est un code d'état, ex. "---/[10]"
                        # ('---' = le capteur n'a raccordé aucune valeur à cet instant)
                        m = re.search(r'\[(\d+)\]', value_str)
                        rows.append({'timestamp': ts, 'value': float('nan'),
                                     'code': m.group(1) if m else value_str.strip()})

    if rows and current_meta:
        blocks.append(_build_df(current_meta, rows))
    return blocks


def _build_df(meta: dict, rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df['station'] = meta['station']
    df['sensor']  = meta['sensor']
    df = df[['station', 'sensor', 'timestamp', 'value', 'code']]
    df.sort_values('timestamp', inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ─────────────────────────────────────────────
# 2a. RÈGLE BATTERIE (capteur 0002)
# ─────────────────────────────────────────────

def check_battery(df: pd.DataFrame) -> list[dict]:
    v = df['value']
    anomalies = []
    vmin = v.min()

    idx_nr  = df.index[v <= BATTERY['non_retour']].tolist()
    idx_dec = df.index[(v <= BATTERY['alerte']) & (v > BATTERY['non_retour'])].tolist()
    idx_sur = df.index[v > BATTERY['surcharge']].tolist()

    if idx_nr:
        premier = df['timestamp'].iloc[min(idx_nr)]
        anomalies.append({
            'type': 'batterie_non_retour', 'severity': 'high',
            'description': (f"🪫 POINT DE NON-RETOUR atteint : {vmin} V ≤ "
                            f"{BATTERY['non_retour']} V — batterie irrécupérable, "
                            f"station en risque d'arrêt (depuis le {premier:%d/%m %H:%M})"),
            'indices': idx_nr,
        })
    elif idx_dec:
        premier = df['timestamp'].iloc[min(idx_dec)]
        anomalies.append({
            'type': 'batterie_decharge', 'severity': 'high',
            'description': (f"🔋 Batterie en décharge : minimum {vmin} V "
                            f"(normal ≈ {BATTERY['nominal']} V) — intervention requise "
                            f"AVANT le point de non-retour ({BATTERY['non_retour']} V). "
                            f"Passage sous {BATTERY['alerte']} V le {premier:%d/%m %H:%M}"),
            'indices': idx_dec,
            'details': [f"{df['timestamp'].iloc[i]:%d/%m %H:%M} : {df['value'].iloc[i]} V"
                        for i in idx_dec[:10]],
        })

    if idx_sur:
        anomalies.append({
            'type': 'batterie_surcharge', 'severity': 'medium',
            'description': (f"Tension anormalement élevée (max {v.max()} V > "
                            f"{BATTERY['surcharge']} V) — régulateur de charge à vérifier"),
            'indices': idx_sur,
        })
    return anomalies


# ─────────────────────────────────────────────
# 2b. VALIDATION MONOSTATION (seuils min/max par station)
# ─────────────────────────────────────────────

def validate_and_correct(df: pd.DataFrame):
    """
    - Batterie (0002)      → règles d'alerte dédiées, aucune correction
    - Pluviométrie (0006)  → valeurs hors plage REMPLACÉES par la médiane des valides
    - Cote (0001) / Débit (0007) → valeurs hors plage SIGNALÉES mais non modifiées
      (remplacer automatiquement une cote ou un débit pourrait masquer une vraie crue)

    Colonnes ajoutées : value_raw (original), corrected (True/False)
    Retourne : (df, liste_anomalies)
    """
    df = df.copy()
    df['value_raw'] = df['value']
    df['corrected'] = False

    station = df['station'].iloc[0]
    sensor  = df['sensor'].iloc[0]
    base    = SENSOR_DEFAULTS.get(sensor, {})

    # Batterie → règles dédiées
    if base.get('battery'):
        return df, check_battery(df)

    limits = get_limits(station, sensor)
    if limits is None:
        return df, []          # capteur inconnu : pas de validation min/max

    mn, mx = limits
    bad = (df['value'] < mn) | (df['value'] > mx)
    if not bad.any():
        return df, []

    anomalies = []
    details = [f"{row.timestamp:%d/%m %H:%M} : {row.value}"
               for row in df.loc[bad].itertuples()]

    if base.get('correct'):
        # Pluie : correction par la médiane des valeurs valides
        valid = df.loc[~bad, 'value']
        replacement = round(valid.median(), 2) if len(valid) else 0.0
        details = [f"{row.timestamp:%d/%m %H:%M} : {row.value} → remplacé par {replacement}"
                   for row in df.loc[bad].itertuples()]
        df.loc[bad, 'value'] = replacement
        df.loc[bad, 'corrected'] = True
        anomalies.append({
            'type': 'out_of_range_corrected', 'severity': 'high',
            'description': (f"{int(bad.sum())} valeur(s) hors plage [{mn} ; {mx}] "
                            f"({base.get('name', sensor)}) — remplacée(s) par la "
                            f"médiane des valeurs valides ({replacement})"),
            'indices': df.index[bad].tolist(), 'details': details,
        })
    else:
        anomalies.append({
            'type': 'out_of_range', 'severity': 'high',
            'description': (f"{int(bad.sum())} valeur(s) hors plage [{mn} ; {mx}] "
                            f"({base.get('name', sensor)}) — à vérifier "
                            f"(non corrigées automatiquement)"),
            'indices': df.index[bad].tolist(), 'details': details,
        })
    return df, anomalies


# ─────────────────────────────────────────────
# 3. DÉTECTION D'ANOMALIES GÉNÉRIQUES (R1 → R7)
# ─────────────────────────────────────────────

def detect_anomalies(df: pd.DataFrame, thresholds: dict = None,
                     disable: tuple = ()) -> list[dict]:
    if thresholds is None:
        thresholds = {}

    anomalies = []
    n    = len(df)
    vals = df['value']

    # R9 : capteur muet ponctuel — lignes "---/[code]" où le capteur a explicitement
    # signalé n'avoir raccordé AUCUNE valeur à cet instant (≠ trou de transmission R7)
    if 'code' in df.columns:
        muets = df[df['code'].notna()]
        if len(muets):
            codes = ', '.join(sorted(muets['code'].astype(str).unique()))
            anomalies.append({
                'type': 'capteur_muet_ponctuel', 'severity': 'medium',
                'description': (f"{len(muets)} instant(s) où le capteur n'a transmis "
                                f"aucune valeur (code {codes}) entre "
                                f"{muets['timestamp'].min():%d/%m %H:%M} et "
                                f"{muets['timestamp'].max():%d/%m %H:%M} "
                                f"— signification du code à confirmer avec la DGRE"),
                'indices': muets.index.tolist(),
            })

    # R1 : valeurs négatives
    if 'negative' not in disable:
        idx = df.index[vals < 0].tolist()
        if idx:
            anomalies.append({
                'type': 'negative_value', 'severity': 'high',
                'description': f'{len(idx)} valeur(s) négative(s)', 'indices': idx,
            })

    # R3 : flatline — signale les VRAIS paliers (durée ≥ flatline_min_h),
    # et ignore les zéros de pluie/débit (0 prolongé = souvent normal : saison
    # sèche, oued à sec). Les micro-paliers de quelques points sont normaux
    # sur des données réelles (résolution des capteurs) et ne sont plus signalés.
    if 'flatline' not in disable and n >= 3:
        flatline_min_h = thresholds.get('flatline_min_h', 6)
        sensor_r3   = df['sensor'].iloc[0] if 'sensor' in df.columns else None
        zero_normal = sensor_r3 in ('0006', '0007')
        v  = vals.values
        tv = df['timestamp'].values

        if vals.nunique() == 1:
            if not (zero_normal and v[0] == 0):
                anomalies.append({
                    'type': 'flatline_full', 'severity': 'high',
                    'description': f'Valeur constante ({v[0]}) sur toute la série ({n} pts)',
                    'indices': df.index.tolist(),
                })
        else:
            best = (0.0, None, 0, 0)   # durée_h, valeur, i, j
            i = 0
            while i < n:
                j = i
                while j + 1 < n and v[j + 1] == v[i]:
                    j += 1
                if j > i and not (zero_normal and v[i] == 0):
                    d_h = float((tv[j] - tv[i]) / np.timedelta64(1, 'h'))
                    if d_h > best[0]:
                        best = (d_h, v[i], i, j)
                i = j + 1
            if best[0] >= flatline_min_h:
                d_h, val, i, j = best
                t0 = pd.Timestamp(tv[i]); t1 = pd.Timestamp(tv[j])
                anomalies.append({
                    'type': 'flatline_partial', 'severity': 'medium',
                    'description': (f'Valeur bloquée à {val} pendant {d_h:.1f} h '
                                    f'(du {t0:%d/%m %H:%M} au {t1:%d/%m %H:%M})'),
                    'indices': list(range(i, j + 1)),
                })

    # R4 : tous zéros
    if 'all_zeros' not in disable and (vals == 0).all():
        anomalies.append({
            'type': 'all_zeros', 'severity': 'low',
            'description': 'Toutes les valeurs à 0 — normal (saison sèche) ou faux zéro (capteur bouché) ?',
            'indices': df.index.tolist(),
        })

    # R5 : horodatages dupliqués
    dupes = df[df.duplicated(subset='timestamp', keep=False)]
    if not dupes.empty:
        anomalies.append({
            'type': 'duplicate_timestamps', 'severity': 'medium',
            'description': f'{len(dupes)} ligne(s) avec horodatage dupliqué',
            'indices': dupes.index.tolist(),
        })

    # R6 + R7 : pas de temps irrégulier / trous
    if n > 2:
        diffs = df['timestamp'].diff().dropna().dt.total_seconds()
        expected = diffs.mode()[0]

        irregular_idx = diffs[(diffs != expected) & (diffs <= expected * 1.5)].index.tolist()
        if irregular_idx:
            anomalies.append({
                'type': 'irregular_timestep', 'severity': 'medium',
                'description': f'{len(irregular_idx)} écart(s) ≠ {int(expected)}s attendu',
                'indices': irregular_idx,
            })

        gaps = diffs[diffs > expected * 1.5]
        if not gaps.empty:
            anomalies.append({
                'type': 'missing_data_gap', 'severity': 'high',
                'description': f'{len(gaps)} trou(s) dans la série temporelle',
                'indices': gaps.index.tolist(),
            })

    return anomalies


def analyse_serie(df: pd.DataFrame):
    """Chaîne complète pour UNE série station-capteur : validation + détection."""
    sensor  = df['sensor'].iloc[0]
    station = df['station'].iloc[0]
    df, anomalies = validate_and_correct(df)
    disable = []
    if SENSOR_DEFAULTS.get(sensor, {}).get('battery'):
        disable += ['flatline', 'all_zeros']
    lim = get_limits(station, sensor)
    if lim and lim[0] < 0:
        # la config tolère des négatifs (artefact connu) → pas d'alerte R1,
        # la règle hors-plage prend le relais en dessous de min_valid
        disable.append('negative')
    anomalies = anomalies + detect_anomalies(df, disable=tuple(disable))
    return df, anomalies


# ─────────────────────────────────────────────
# 4. RAPPORT CONSOLE
# ─────────────────────────────────────────────

def print_report(filepath: str):
    blocks = parse_mis(filepath)
    print(f"\n{'='*62}")
    print(f"Fichier : {Path(filepath).name}")
    print(f"Blocs capteurs : {len(blocks)}")
    if not STATION_NAMES:
        print("⚠ config_stations.csv introuvable → seuils par défaut pour toutes les stations")

    for df in blocks:
        df, anomalies = analyse_serie(df)
        station = df['station'].iloc[0]
        sensor  = df['sensor'].iloc[0]
        name    = SENSOR_DEFAULTS.get(sensor, {}).get('name', 'capteur inconnu')
        diffs   = df['timestamp'].diff().dropna().dt.total_seconds()
        step    = int(diffs.mode()[0]) if len(diffs) else '?'

        print(f"\n{'─'*58}")
        print(f"  Station {station_label(station)}  |  Capteur {sensor} ({name})")
        print(f"  {len(df)} mesures  |  "
              f"{df['timestamp'].min():%Y-%m-%d %H:%M} → {df['timestamp'].max():%H:%M}"
              f"  |  pas {step}s")

        if anomalies:
            for a in anomalies:
                icon = {'high': '🔴', 'medium': '🟡', 'low': '🔵'}.get(a['severity'], '❔')
                print(f"  {icon} [{a['type']}] {a['description']}")
                for d in a.get('details', [])[:10]:
                    print(f"       ↳ {d}")
        else:
            print("  ✅ Aucune anomalie détectée")


if __name__ == '__main__':
    import sys
    fp = sys.argv[1] if len(sys.argv) > 1 else 'exemple.MIS'
    print_report(fp)
