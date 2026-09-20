"""
DÉMO de la mémoire long terme — génère 30 jours d'historique synthétique
========================================================================
Crée une base de démonstration SÉPARÉE (demo_historique.db) pour montrer
les règles long terme en action, sans toucher à ta vraie mémoire.

Scénario simulé sur la station HenchirTobias (1485900187) :
  - Cote    : varie normalement pendant 28,5 j… puis se BLOQUE 4 jours (panne)
  - Batterie: se décharge lentement de 0,12 V/jour (panneau solaire sale ?)
  - Débit   : ~1 m³/s pendant 30 j… puis pic soudain à 180 m³/s (crue ? erreur ?)

Lancer :  python demo_memoire.py
"""

from pathlib import Path
import numpy as np
import pandas as pd

import memoire
from mis_parser import SENSOR_DEFAULTS, station_label

DEMO_DB = Path(__file__).parent / 'demo_historique.db'
if DEMO_DB.exists():
    DEMO_DB.unlink()          # on repart de zéro à chaque lancement

con = memoire.connexion(DEMO_DB)
rng = np.random.default_rng(42)
STATION = '1485900187'
t0 = pd.Timestamp('2026-06-08 00:00')

def serie(sensor, timestamps, valeurs):
    df = pd.DataFrame({'station': STATION, 'sensor': sensor,
                       'timestamp': timestamps, 'value': valeurs})
    df['value_raw'] = df['value']
    df['corrected'] = False
    return df[['station', 'sensor', 'timestamp', 'value', 'value_raw', 'corrected']]

# ── Cote : 30 j au pas de 15 min, niveau ~245 cm qui respire… puis bloqué 36 h
ts = pd.date_range(t0, periods=30*96, freq='15min')
niveau = 245 + 8*np.sin(np.arange(len(ts))/96*2*np.pi/7) + rng.normal(0, 0.8, len(ts))
niveau = niveau.round(1)
niveau[-384:] = 245.3                     # 384 pas de 15 min = 96 h bloquées
memoire.ajouter_mesures(serie('0001', ts, niveau), con)

# ── Batterie : 30 j au pas de 1 h, décharge de 0,12 V/jour
ts_b = pd.date_range(t0, periods=30*24, freq='1h')
volts = (12.6 - 0.12*np.arange(len(ts_b))/24 + rng.normal(0, 0.02, len(ts_b))).round(2)
memoire.ajouter_mesures(serie('0002', ts_b, volts), con)

# ── Débit : 30 j au pas de 1 h, ~1 m³/s… puis pic à 180 m³/s sur les 6 dernières h
ts_d = pd.date_range(t0, periods=30*24, freq='1h')
debit = np.clip(rng.normal(1.0, 0.3, len(ts_d)), 0.1, None).round(2)
debit[-6:] = [95, 140, 180, 170, 150, 120]
memoire.ajouter_mesures(serie('0007', ts_d, debit), con)

# ── Rapport long terme ──
s = memoire.stats_memoire(con)
print(f"🧠 Mémoire de démo : {s['mesures']} mesures, du {s['debut']} au {s['fin']}\n")
print("="*62)
print("ANALYSE LONG TERME")
print("="*62)
for station, sensor in memoire.series_presentes(con):
    for a in memoire.analyser_long_terme(station, sensor, con):
        icon = {'high': '🔴', 'medium': '🟡'}.get(a['severity'], '🔵')
        name = SENSOR_DEFAULTS.get(sensor, {}).get('name', sensor)
        print(f"\n{icon} {station_label(station)} · {name}")
        print(f"   {a['description']}")
print("\n(la vraie mémoire de l'agent — historique.db — n'a pas été touchée)")
