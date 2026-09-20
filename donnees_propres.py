"""
donnees_propres.py — Porte d'entrée des données pour les règles inter-stations
==============================================================================
Toutes les règles de la phase 2 lisent leurs données ICI, jamais directement
dans la base. Deux fonctions, deux philosophies, jamais mélangées :

  charger_serie_propre()   PLAN A (défaut) — les valeurs physiquement
                           impossibles sont EXCLUES du calcul. Rien n'est
                           inventé : là où la valeur était fausse, il y a un
                           trou. C'est la seule fonction que les règles
                           inter-stations utilisent.

  reconstruire()           PLAN B (sur demande) — comble les petits trous par
                           interpolation, chaque point reconstruit étant
                           marqué `reconstruit=True`. Réservé à l'affichage
                           (graphes continus). JAMAIS utilisé par les règles.

Le seuil de validité n'est pas inventé ici : c'est get_limits() de
mis_parser.py, donc les mêmes bornes min/max par station que la phase 1 (R2),
lues dans config_stations.csv. Phase 1 et phase 2 jugent une valeur avec le
même critère — seule la conséquence diffère (signaler vs exclure du calcul).

La base n'est JAMAIS modifiée : tout se passe en mémoire, à la lecture.
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from mis_parser import get_limits, SENSOR_DEFAULTS

# Trou maximal comblé par reconstruire(), en nombre de pas de temps.
# Au-delà, interpoler inventerait trop : le trou reste un trou.
# Court exprès : sur une cote, interpoler à travers une crue écraserait
# le pic — la seule chose qu'on ne doit jamais lisser.
MAX_TROU_RECONSTRUIT = 4


# ─────────────────────────────────────────────
# PLAN A — LECTURE PROPRE (défaut des règles)
# ─────────────────────────────────────────────

def charger_serie_propre(station: str, sensor: str, con: sqlite3.Connection,
                         debut=None, fin=None):
    """Série d'une station/capteur, valeurs impossibles EXCLUES.

    Retourne (df, rejets) :
      df      colonnes [timestamp, value] — uniquement des valeurs plausibles
      rejets  DataFrame des lignes écartées, avec leur raison — pour que
              l'exclusion soit toujours visible et jamais silencieuse

    `debut`/`fin` (optionnels) restreignent la période (str ou Timestamp).
    """
    q = ("SELECT timestamp, value FROM mesures "
         "WHERE station=? AND sensor=? ORDER BY timestamp")
    df = pd.read_sql_query(q, con, params=(str(station), str(sensor)),
                           parse_dates=['timestamp'])
    if debut is not None:
        df = df[df['timestamp'] >= pd.Timestamp(debut)]
    if fin is not None:
        df = df[df['timestamp'] <= pd.Timestamp(fin)]
    df = df.reset_index(drop=True)

    if df.empty:
        return df, df.assign(raison=pd.Series(dtype=str))

    garde = pd.Series(True, index=df.index)
    raison = pd.Series('', index=df.index)

    # 1. Non-numérique / NaN (ne devrait pas exister en base, mais on vérifie)
    mauvais = ~np.isfinite(df['value'].to_numpy(dtype=float))
    garde &= ~mauvais
    raison[mauvais] = 'valeur non numérique'

    # 2. Hors des bornes min/max de la station (mêmes seuils que R2 en phase 1)
    lim = get_limits(str(station), str(sensor))
    if lim is not None:
        mn, mx = lim
        hors = garde & ((df['value'] < mn) | (df['value'] > mx))
        garde &= ~hors
        raison[hors] = f'hors bornes station [{mn} ; {mx}]'

    rejets = df.loc[~garde].assign(raison=raison[~garde])
    propre = df.loc[garde].reset_index(drop=True)
    return propre, rejets.reset_index(drop=True)


def pas_de_temps(df: pd.DataFrame):
    """Pas de temps dominant de la série (Timedelta), ou None si indécidable."""
    if len(df) < 3:
        return None
    diffs = df['timestamp'].diff().dropna()
    return diffs.mode().iloc[0] if len(diffs) else None


# ─────────────────────────────────────────────
# PLAN B — RECONSTRUCTION ÉTIQUETÉE (affichage uniquement)
# ─────────────────────────────────────────────

def reconstruire(df: pd.DataFrame, max_trou: int = MAX_TROU_RECONSTRUIT):
    """Comble les petits trous par interpolation linéaire, en l'affichant.

    Prend la sortie de charger_serie_propre(). Retourne un DataFrame
    [timestamp, value, reconstruit] où reconstruit=True marque chaque point
    interpolé. Les trous plus longs que `max_trou` pas de temps restent des
    trous : interpoler à travers une longue absence (ou une crue manquée)
    fabriquerait une hydrologie qui n'a jamais existé.

    À n'utiliser QUE pour l'affichage. Les règles inter-stations ne
    consomment jamais cette sortie.
    """
    if df.empty:
        return df.assign(reconstruit=pd.Series(dtype=bool))

    pas = pas_de_temps(df)
    if pas is None:
        return df.assign(reconstruit=False)

    # grille temporelle régulière entre le premier et le dernier point
    grille = pd.date_range(df['timestamp'].iloc[0], df['timestamp'].iloc[-1],
                           freq=pas)
    s = (df.set_index('timestamp')['value']
           .reindex(grille))                     # NaN là où il manque un point

    manquant = s.isna()
    # taille de chaque trou : un trou = série de NaN consécutifs
    groupe = (~manquant).cumsum()
    taille_trou = manquant.groupby(groupe).transform('sum')

    comblable = manquant & (taille_trou <= max_trou)
    s_interp = s.interpolate(method='time', limit_area='inside')

    out = pd.DataFrame({
        'timestamp': grille,
        'value': np.where(comblable, s_interp, s),
        'reconstruit': comblable.to_numpy(),
    })
    # on ne garde pas les NaN restants (trous trop longs, non comblés)
    return out.dropna(subset=['value']).reset_index(drop=True)


# ─────────────────────────────────────────────
# AUTO-TEST (py donnees_propres.py)
# ─────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    base = Path(sys.argv[1] if len(sys.argv) > 1 else 'historique_demo.db')
    if not base.exists():
        raise SystemExit(f"Base introuvable : {base}")
    con = sqlite3.connect(base)

    station, sensor = '1485101210', '0001'      # Mellegue K13, cote
    propre, rejets = charger_serie_propre(station, sensor, con)

    print(f"Station {station} · capteur {sensor}")
    print(f"  points gardés  : {len(propre)}")
    print(f"  points écartés : {len(rejets)}")
    if len(rejets):
        print("\n  Détail des exclusions (jamais silencieuses) :")
        for _, r in rejets.iterrows():
            print(f"    {r['timestamp']} : {r['value']:g}  ← {r['raison']}")
    if len(propre):
        v = propre['value']
        print(f"\n  Après exclusion : min {v.min():.1f} · "
              f"médiane {v.median():.1f} · max {v.max():.1f}")

    rec = reconstruire(propre)
    n_rec = int(rec['reconstruit'].sum())
    print(f"\n  reconstruire() : {len(rec)} points dont {n_rec} interpolé(s) "
          f"(trous ≤ {MAX_TROU_RECONSTRUIT} pas)")
    print("  (fonction d'affichage — les règles inter-stations ne l'utilisent pas)")
