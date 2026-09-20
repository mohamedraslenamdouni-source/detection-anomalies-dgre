"""
verifier_base.py — Contrôle de la base historique_demo.db
=========================================================
Vérifie en une passe :
  1. la séparation réel / simulé (aucune station ne doit mélanger les deux)
  2. la profondeur d'historique de chaque station RÉELLE, capteur par capteur
  3. si cet historique suffit à déclencher les règles long terme L1-L6
  4. les relations amont-aval exploitables pour les règles inter-stations

Lancer :
    py verifier_base.py
    py verifier_base.py historique_demo.db
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else 'historique_demo.db')
CONFIG = Path('config_stations.csv')
RESEAU = Path('reseau_stations.csv')

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

print(f"Base : {BASE.resolve()}")
print(f"Taille : {BASE.stat().st_size / 1024 / 1024:.1f} Mo\n")

# ── 1. Provenance ────────────────────────────────────────────
try:
    prov = pd.read_sql("SELECT station, origine FROM provenance_station", con)
except Exception:
    sys.exit("Table provenance_station absente : cette base n'a pas été "
             "produite par donnees_synthetiques.py.")

print("=" * 62)
print("1. PROVENANCE")
print("=" * 62)
print(prov['origine'].value_counts().to_string())

reelles = prov.loc[prov['origine'] == 'réel', 'station'].tolist()
print(f"\nStations RÉELLES ({len(reelles)}) :")
for s in reelles:
    print(f"   {s}  {noms.get(s, '')}")

orphelines = pd.read_sql(
    "SELECT COUNT(DISTINCT station) n FROM mesures "
    "WHERE station NOT IN (SELECT station FROM provenance_station)", con)['n'][0]
print(f"\nStations sans provenance déclarée : {orphelines}"
      f"{'  ✅' if orphelines == 0 else '  ⚠️ à corriger'}")

# ── 2. Profondeur d'historique des stations réelles ──────────
print("\n" + "=" * 62)
print("2. HISTORIQUE RÉEL DISPONIBLE")
print("=" * 62)

lignes = []
for s in reelles:
    d = pd.read_sql(
        "SELECT sensor, COUNT(*) n, MIN(timestamp) t0, MAX(timestamp) t1 "
        "FROM mesures WHERE station=? GROUP BY sensor", con, params=(s,))
    for _, r in d.iterrows():
        t0, t1 = pd.Timestamp(r['t0']), pd.Timestamp(r['t1'])
        jours = (t1 - t0).total_seconds() / 86400
        pas = (jours * 24 * 60 / (r['n'] - 1)) if r['n'] > 1 else float('nan')
        lignes.append({
            'station': s, 'nom': noms.get(s, ''),
            'capteur': f"{r['sensor']} {CAPTEURS.get(r['sensor'], '')}",
            'points': int(r['n']),
            'du': t0.strftime('%d/%m/%Y'), 'au': t1.strftime('%d/%m/%Y'),
            'jours': round(jours, 1), 'pas_min': round(pas, 1),
        })
h = pd.DataFrame(lignes)
print(h.to_string(index=False) if len(h) else "  (aucune donnée réelle)")

# ── 3. Règles long terme applicables au réel ─────────────────
# Préconditions RÉELLES des règles long terme, telles qu'écrites dans
# memoire.py. Attention : L1, L4, L5 et L6 sont réservées aux capteurs
# 0001 (cote) et 0007 (débit) — elles ne tournent JAMAIS sur la pluie.
# L2/L2b sont réservées au capteur 0002 (batterie).

def evaluer_regles(sensor, n, jours, t0, t1, valeurs_par_jour):
    """Retourne [(règle, applicable, raison), ...] pour une série donnée."""
    res = []
    hydro = sensor in ('0001', '0007')
    batt = sensor == '0002'

    if hydro:
        res.append(('L1 flatline longue', jours * 24 >= 72,
                    "série ≥ 72 h" if jours * 24 >= 72
                    else f"série trop courte ({jours * 24:.0f} h < 72 h)"))
    else:
        res.append(('L1 flatline longue', False,
                    "capteur non concerné (cote/débit uniquement)"))

    if batt:
        recents = valeurs_par_jour.get('7j', 0)
        res.append(('L2 dérive batterie', recents >= 10 and jours >= 2,
                    f"{recents} pts sur les 7 derniers jours (≥ 10 requis)"))
        hist = n - recents
        res.append(('L2b absence de charge', recents >= 10 and hist >= 50,
                    f"{hist} pts d'historique avant 7 j (≥ 50 requis)"))
    else:
        res.append(('L2 dérive batterie', False, "capteur non batterie"))

    if hydro:
        hist24 = n - valeurs_par_jour.get('1j', 0)
        res.append(('L4 valeur inhabituelle', hist24 >= 100,
                    f"{hist24} pts avant les 24 dernières h (≥ 100 requis)"))
        rec7 = valeurs_par_jour.get('7j', 0)
        hist7 = n - rec7
        ok5 = rec7 >= 30 and hist7 >= 200 and jours >= 11
        res.append(('L5 tendance continue', ok5,
                    f"{rec7} pts récents (≥ 30) + {hist7} pts d'historique (≥ 200)"))
        res.append(('L6 rupture de moyenne', jours >= 60,
                    f"{jours:.1f} jours de données (≥ 60 jours requis)"))
    else:
        for r in ('L4 valeur inhabituelle', 'L5 tendance continue',
                  'L6 rupture de moyenne'):
            res.append((r, False, "capteur non concerné (cote/débit uniquement)"))
    return res


print("\n" + "=" * 62)
print("3. RÈGLES LONG TERME APPLICABLES AUX DONNÉES RÉELLES")
print("=" * 62)
print("  (préconditions lues directement dans memoire.py)")

for s in reelles:
    for sensor in sorted(CAPTEURS):
        d = pd.read_sql(
            "SELECT timestamp FROM mesures WHERE station=? AND sensor=? "
            "ORDER BY timestamp", con, params=(s, sensor),
            parse_dates=['timestamp'])
        if d.empty:
            continue
        t0, t1 = d['timestamp'].min(), d['timestamp'].max()
        jours = (t1 - t0).total_seconds() / 86400
        vpj = {'1j': int((d['timestamp'] > t1 - pd.Timedelta(days=1)).sum()),
               '7j': int((d['timestamp'] >= t1 - pd.Timedelta(days=7)).sum())}
        print(f"\n  {s} · {sensor} {CAPTEURS[sensor]} "
              f"({len(d)} pts, {jours:.1f} j)")
        for nom_regle, ok, raison in evaluer_regles(
                sensor, len(d), jours, t0, t1, vpj):
            print(f"     {'✅' if ok else '❌'} {nom_regle:24s} — {raison}")

# ── 3bis. Contenu hydrologique réel ──────────────────────────
print("\n" + "=" * 62)
print("3bis. CONTENU HYDROLOGIQUE DES DONNÉES RÉELLES")
print("=" * 62)
for s in reelles:
    print(f"\n  {s}  {noms.get(s, '')}")
    for sensor, lib in (('0006', 'Pluie (mm)'), ('0001', 'Cote (cm)'),
                        ('0007', 'Débit (m³/s)'), ('0002', 'Batterie (V)')):
        d = pd.read_sql(
            "SELECT value FROM mesures WHERE station=? AND sensor=?",
            con, params=(s, sensor))
        if d.empty:
            print(f"     {lib:14s} — AUCUNE DONNÉE")
            continue
        v = d['value']
        if sensor == '0006':
            print(f"     {lib:14s} cumul {v.sum():.1f} mm · max {v.max():.1f} "
                  f"· {int((v > 0).sum())} pas de temps pluvieux")
        else:
            print(f"     {lib:14s} min {v.min():.2f} · médiane {v.median():.2f} "
                  f"· max {v.max():.2f} · amplitude {v.max() - v.min():.2f}")

# ── 4. Relations amont-aval exploitables ─────────────────────
print("\n" + "=" * 62)
print("4. RELATIONS AMONT-AVAL (règles inter-stations)")
print("=" * 62)
if RESEAU.exists():
    r = pd.read_csv(RESEAU, sep=';', encoding='utf-8-sig', dtype=str)
    orig = dict(zip(prov['station'], prov['origine']))
    r['o_amont'] = r['station_amont'].map(orig)
    r['o_aval'] = r['station_aval'].map(orig)
    r['couple'] = r['o_amont'].fillna('?') + ' → ' + r['o_aval'].fillna('?')
    print(r['couple'].value_counts().to_string())

    vrai = r[(r['o_amont'] == 'réel') & (r['o_aval'] == 'réel')]
    print(f"\n  Paires 100 % RÉELLES ({len(vrai)}) — les seules qui valident "
          "physiquement une règle inter-stations :")
    if len(vrai):
        print(vrai[['nom_amont', 'nom_aval', 'distance_km',
                    'fiabilite']].to_string(index=False))
    else:
        print("     aucune")

    mixte = r[((r['o_amont'] == 'réel') & (r['o_aval'] == 'simulé'))
              | ((r['o_amont'] == 'simulé') & (r['o_aval'] == 'réel'))]
    if len(mixte):
        print(f"\n  ⚠️ Paires MIXTES ({len(mixte)}) — à EXCLURE des tests : "
              "un côté réel + un côté simulé ne peuvent pas être corrélés,")
        print("     toute anomalie détectée sur ces paires est un artefact.")
        print(mixte[['nom_amont', 'nom_aval', 'distance_km',
                     'fiabilite']].to_string(index=False))
else:
    print("  reseau_stations.csv introuvable")

con.close()
print("\nContrôle terminé.")
