"""
altitudes.py — Récupère l'altitude de chaque station via OpenTopoData
=====================================================================
Lit config_stations.csv, convertit les coordonnées UTM 32N en latitude/longitude,
interroge l'API publique OpenTopoData, et écrit la colonne `altitude_m`.

Aucune dépendance en plus de pandas (déjà installé) : la conversion de
coordonnées et les appels réseau utilisent uniquement la bibliothèque standard.

Lancer :
    py altitudes.py

À ne lancer qu'UNE FOIS : les altitudes ne changent pas. Le script saute
automatiquement les stations déjà renseignées (relance sans risque).

Limites de l'API publique : 100 points par requête, 1 appel/seconde,
1000 appels/jour. Avec 102 stations, cela fait 2 requêtes.
"""

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

CONFIG = Path(__file__).parent / 'config_stations.csv'

# Modèle numérique de terrain utilisé. Alternatives : 'aster30m', 'srtm90m',
# 'mapzen' (couverture mondiale, comble les trous de SRTM).
DATASET = 'srtm30m'

UTM_ZONE = 32          # Tunisie = zone 32N
BATCH    = 100         # maximum autorisé par requête
PAUSE_S  = 1.2         # respect de la limite de 1 appel/seconde


# ─────────────────────────────────────────────
# 1. CONVERSION UTM (WGS84) → LATITUDE / LONGITUDE
# ─────────────────────────────────────────────

def utm_to_latlon(easting: float, northing: float,
                  zone: int = UTM_ZONE, northern: bool = True):
    """Convertit des coordonnées UTM en degrés décimaux (EPSG:4326)."""
    a  = 6378137.0                      # demi-grand axe WGS84
    f  = 1 / 298.257223563              # aplatissement
    e2 = f * (2 - f)
    k0 = 0.9996

    x = easting - 500000.0
    y = northing if northern else northing - 10000000.0

    m  = y / k0
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    mu = m / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))

    phi1 = (mu
            + (3*e1/2 - 27*e1**3/32)      * math.sin(2*mu)
            + (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
            + (151*e1**3/96)              * math.sin(6*mu)
            + (1097*e1**4/512)            * math.sin(8*mu))

    ep2 = e2 / (1 - e2)
    C1  = ep2 * math.cos(phi1)**2
    T1  = math.tan(phi1)**2
    N1  = a / math.sqrt(1 - e2 * math.sin(phi1)**2)
    R1  = a * (1 - e2) / (1 - e2 * math.sin(phi1)**2)**1.5
    D   = x / (N1 * k0)

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
            D**2/2
            - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*ep2) * D**4/24
            + (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*ep2 - 3*C1**2) * D**6/720)

    lon = (D
           - (1 + 2*T1 + C1) * D**3/6
           + (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*ep2 + 24*T1**2) * D**5/120
          ) / math.cos(phi1)

    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lat), math.degrees(lon0 + lon)


# ─────────────────────────────────────────────
# 2. APPEL DE L'API OPENTOPODATA
# ─────────────────────────────────────────────

def fetch_elevations(points, dataset=DATASET, timeout=60):
    """points = [(lat, lon), ...] (100 maximum). Retourne [altitude|None, ...]."""
    locations = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    url = f"https://api.opentopodata.org/v1/{dataset}?locations={locations}"
    req = urllib.request.Request(url, headers={'User-Agent': 'DGRE-anomalies/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    if data.get('status') != 'OK':
        raise RuntimeError(f"Réponse API : {data.get('status')} — {data.get('error', '')}")
    return [res.get('elevation') for res in data['results']]


# ─────────────────────────────────────────────
# 3. PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

def main():
    if not CONFIG.exists():
        sys.exit(f"Fichier introuvable : {CONFIG}")

    df = pd.read_csv(CONFIG, sep=';', encoding='utf-8-sig', dtype={'code_station': str})

    for col in ('x', 'y'):
        if col not in df.columns:
            sys.exit(f"Colonne '{col}' absente de config_stations.csv — "
                     "utilise la version du fichier qui contient les coordonnées.")
    if 'altitude_m' not in df.columns:
        df['altitude_m'] = pd.NA

    # stations à traiter : coordonnées présentes ET altitude encore vide
    a_faire = df.index[df['x'].notna() & df['y'].notna()
                       & df['altitude_m'].isna()].tolist()

    if not a_faire:
        print("✅ Toutes les altitudes sont déjà renseignées — rien à faire.")
        return

    print(f"Stations à traiter : {len(a_faire)}")
    print(f"Modèle de terrain  : {DATASET}")
    print(f"Requêtes prévues   : {math.ceil(len(a_faire)/BATCH)}\n")

    ok = echecs = 0

    for début in range(0, len(a_faire), BATCH):
        lot = a_faire[début:début + BATCH]
        pts = [utm_to_latlon(df.at[i, 'x'], df.at[i, 'y']) for i in lot]

        print(f"  Requête {début//BATCH + 1} — {len(lot)} stations… ", end='', flush=True)
        try:
            altitudes = fetch_elevations(pts)
        except urllib.error.URLError as e:
            print(f"\n  ❌ Réseau indisponible : {e.reason}")
            print("     Vérifie ta connexion Internet, puis relance le script.")
            break
        except Exception as e:
            print(f"\n  ❌ {type(e).__name__} : {e}")
            break

        for i, alt in zip(lot, altitudes):
            if alt is None:
                echecs += 1
            else:
                df.at[i, 'altitude_m'] = round(float(alt), 1)
                ok += 1
        print("ok")

        if début + BATCH < len(a_faire):
            time.sleep(PAUSE_S)

    df.to_csv(CONFIG, sep=';', index=False, encoding='utf-8-sig')

    print(f"\n💾 {CONFIG.name} mis à jour — {ok} altitude(s) récupérée(s)"
          + (f", {echecs} hors couverture" if echecs else ""))

    valides = pd.to_numeric(df['altitude_m'], errors='coerce').dropna()
    if len(valides):
        print(f"   Altitudes : min {valides.min():.0f} m | "
              f"médiane {valides.median():.0f} m | max {valides.max():.0f} m")
        print("\n   Contrôle : les altitudes de Tunisie doivent aller d'environ "
              "0 m (côte)\n   jusqu'à ~1544 m (Djebel Chambi). Des valeurs négatives "
              "sont possibles\n   près des chotts, qui sont sous le niveau de la mer.")

        st_hautes = df.loc[valides.nlargest(3).index, ['nom', 'gouvernorat', 'altitude_m']]
        print("\n   Stations les plus hautes :")
        for _, r in st_hautes.iterrows():
            print(f"     {r['nom']:<22} {r['gouvernorat']:<14} {r['altitude_m']} m")


if __name__ == '__main__':
    main()
