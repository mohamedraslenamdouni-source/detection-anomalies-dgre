"""
diagnostic_reel.py — Ce qui reste vraiment exploitable dans les données réelles
===============================================================================
Rejoue l'inventaire des stations RÉELLES, mais en lisant à travers
donnees_propres.charger_serie_propre() — donc APRÈS exclusion des valeurs
physiquement impossibles.

Pourquoi ce script existe : l'inventaire précédent comptait comme « données »
les 221 points de panne du capteur de cote de Mellegue K13. Une fenêtre
d'analyse calculée sur des valeurs fausses est une fenêtre fausse. On refait
donc le décompte sur ce qui est réellement mesuré.

Lancer :
    py diagnostic_reel.py
    py diagnostic_reel.py historique_demo.db
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

from donnees_propres import charger_serie_propre, pas_de_temps

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else 'historique_demo.db')
CONFIG = Path('config_stations.csv')
CAPTEURS = {'0001': 'Cote', '0002': 'Batterie',
            '0006': 'Pluie', '0007': 'Débit'}

if not BASE.exists():
    sys.exit(f"Base introuvable : {BASE.resolve()}")

con = sqlite3.connect(BASE)

noms = {}
if CONFIG.exists():
    c = pd.read_csv(CONFIG, sep=';', encoding='utf-8-sig',
                    dtype={'code_station': str})
    noms = {str(r['code_station']).strip(): str(r['nom']).strip()
            for _, r in c.iterrows()}

try:
    reelles = [r[0] for r in con.execute(
        "SELECT station FROM provenance_station WHERE origine='réel'")]
except sqlite3.OperationalError:
    sys.exit("Table provenance_station absente.")

print(f"Base : {BASE.name}")
print(f"Stations réelles : {len(reelles)}\n")


def blocs_consecutifs(ts, pas):
    """Découpe une série d'horodatages en blocs continus (au pas attendu)."""
    if len(ts) == 0 or pas is None:
        return []
    coupures = ts.diff() > pas * 1.5
    groupe = coupures.cumsum()
    return [(g.min(), g.max(), len(g)) for _, g in ts.groupby(groupe)]


resume = []
for st in reelles:
    print("=" * 66)
    print(f"{st}  {noms.get(st, '')}")
    print("=" * 66)

    for sensor in sorted(CAPTEURS):
        brut = pd.read_sql_query(
            "SELECT timestamp, value FROM mesures WHERE station=? AND sensor=? "
            "ORDER BY timestamp", con, params=(st, sensor),
            parse_dates=['timestamp'])
        if brut.empty:
            continue

        propre, rejets = charger_serie_propre(st, sensor, con)
        n_brut, n_ok, n_rej = len(brut), len(propre), len(rejets)

        print(f"\n  Capteur {sensor} {CAPTEURS[sensor]}")
        print(f"    bruts {n_brut} · exclus {n_rej} · EXPLOITABLES {n_ok}"
              f"  ({100 * n_ok / n_brut:.0f} %)")

        if n_ok == 0:
            print("    ⛔ plus rien d'exploitable")
            resume.append((st, sensor, 0, 0, 'inutilisable'))
            continue

        t0, t1 = propre['timestamp'].min(), propre['timestamp'].max()
        jours = (t1 - t0).total_seconds() / 86400
        print(f"    fenêtre réelle : {t0:%d/%m/%Y %H:%M} → {t1:%d/%m/%Y %H:%M}"
              f"  ({jours:.1f} j)")

        # La panne est-elle À LA FIN ? C'est le pire cas : les règles long
        # terme regardent justement les données les plus récentes.
        if n_rej:
            fin_brut = brut['timestamp'].max()
            retard_h = (fin_brut - t1).total_seconds() / 3600
            if retard_h > 1:
                print(f"    ⚠️  les {retard_h:.0f} dernières heures sont "
                      f"inexploitables (panne en fin de série)")
                print(f"       dernière mesure VALIDE : {t1:%d/%m/%Y %H:%M}")

        pas = pas_de_temps(propre)
        if pas is not None:
            blocs = blocs_consecutifs(propre['timestamp'], pas)
            print(f"    pas de temps : {pas.total_seconds() / 60:.0f} min · "
                  f"{len(blocs)} bloc(s) continu(s)")
            if len(blocs) > 1:
                for b0, b1, n in blocs[:5]:
                    print(f"       {b0:%d/%m %H:%M} → {b1:%d/%m %H:%M}  ({n} pts)")
                if len(blocs) > 5:
                    print(f"       … et {len(blocs) - 5} autre(s)")

        v = propre['value']
        print(f"    valeurs : min {v.min():.1f} · médiane {v.median():.1f} "
              f"· max {v.max():.1f}")
        resume.append((st, sensor, n_ok, round(jours, 1), 'ok'))

# ── Ce que les données propres permettent réellement ──────────
print("\n" + "=" * 66)
print("CE QUI EST TESTABLE SUR DONNÉES RÉELLES PROPRES")
print("=" * 66)

r = pd.DataFrame(resume, columns=['station', 'capteur', 'points', 'jours', 'etat'])
hydro = r[(r['capteur'] == '0001') & (r['etat'] == 'ok')]
pluie = r[(r['capteur'] == '0006') & (r['etat'] == 'ok')]

print("\n  Cote (0001) — support des règles de crue amont/aval :")
if hydro.empty:
    print("     aucune série de cote exploitable")
for _, x in hydro.iterrows():
    print(f"     {x['station']} {noms.get(x['station'], '')}: "
          f"{x['points']} pts sur {x['jours']} j")

print("\n  Pluie (0006) — support de la règle « faux zéro » :")
for _, x in pluie.iterrows():
    print(f"     {x['station']} {noms.get(x['station'], '')}: "
          f"{x['points']} pts sur {x['jours']} j")

# fenêtre COMMUNE aux deux stations réelles (indispensable pour comparer)
print("\n  Fenêtre COMMUNE aux deux stations réelles (comparaison possible) :")
for sensor in ('0001', '0006'):
    fenetres = []
    for st in reelles:
        p, _ = charger_serie_propre(st, sensor, con)
        if len(p):
            fenetres.append((p['timestamp'].min(), p['timestamp'].max(), st))
    if len(fenetres) < 2:
        print(f"     {sensor} {CAPTEURS[sensor]} : impossible "
              "(une seule station exploitable)")
        continue
    debut = max(f[0] for f in fenetres)
    fin = min(f[1] for f in fenetres)
    if fin <= debut:
        print(f"     {sensor} {CAPTEURS[sensor]} : aucun recouvrement")
    else:
        h = (fin - debut).total_seconds() / 3600
        print(f"     {sensor} {CAPTEURS[sensor]} : {debut:%d/%m %H:%M} → "
              f"{fin:%d/%m %H:%M}  ({h / 24:.1f} j en commun)")

con.close()
print("\nDiagnostic terminé.")
