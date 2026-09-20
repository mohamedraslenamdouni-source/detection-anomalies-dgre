"""
observer_evenement.py — Regarder un événement avant d'écrire une règle
=======================================================================
On ne code pas une règle de cohérence amont-aval avant d'avoir vu, sur les
vraies données, à quoi ressemble (ou ne ressemble pas) la propagation.

Ce script affiche côte à côte, sur une période choisie :
  • la cote de la station AMONT   (PT RTE SARRAT)
  • la cote de la station AVAL    (Mellegue K13)
  • la pluie des deux stations
et trace un graphique ASCII, lisible dans le terminal, sans dépendance.

Lecture via donnees_propres : les valeurs impossibles sont déjà exclues.

Lancer :
    py observer_evenement.py
    py observer_evenement.py 2026-06-29 2026-07-02
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

from donnees_propres import charger_serie_propre

BASE = Path('historique_demo.db')
AMONT = '1485100506'      # PT RTE SARRAT
AVAL = '1485101210'       # Mellegue K13
NOM = {AMONT: 'PT RTE SARRAT (amont)', AVAL: 'Mellegue K13 (aval)'}

DEBUT = sys.argv[1] if len(sys.argv) > 2 else '2026-06-29'
FIN = sys.argv[2] if len(sys.argv) > 2 else '2026-07-02'

LARGEUR = 58              # largeur du graphique ASCII


def sparkline(df, largeur=LARGEUR, hauteur=12):
    """Petit graphique ASCII d'une série (timestamp, value)."""
    if df.empty:
        return ["   (aucune donnée sur la période)"]
    t0 = df['timestamp'].min()
    t1 = df['timestamp'].max()
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return [f"   point unique : {df['value'].iloc[0]:.1f}"]

    vmin, vmax = df['value'].min(), df['value'].max()
    etendue = vmax - vmin if vmax > vmin else 1.0

    # colonne -> valeur max observée dans cette tranche de temps
    colonnes = [None] * largeur
    for _, r in df.iterrows():
        c = int((r['timestamp'] - t0).total_seconds() / span * (largeur - 1))
        v = r['value']
        colonnes[c] = v if colonnes[c] is None else max(colonnes[c], v)

    lignes = []
    for h in range(hauteur, 0, -1):
        seuil = vmin + etendue * (h - 0.5) / hauteur
        ligne = ''.join('█' if (c is not None and c >= seuil) else
                        ('·' if c is not None else ' ') for c in colonnes)
        etiquette = f"{vmin + etendue * h / hauteur:7.1f} |"
        lignes.append(etiquette + ligne)
    lignes.append(f"{vmin:7.1f} |" + '─' * largeur)
    lignes.append(' ' * 8 + f"{t0:%d/%m %H:%M}" +
                  ' ' * max(1, largeur - 22) + f"{t1:%d/%m %H:%M}")
    return lignes


if not BASE.exists():
    sys.exit(f"Base introuvable : {BASE.resolve()}")
con = sqlite3.connect(BASE)

print(f"Période observée : {DEBUT} → {FIN}\n")

# ── COTES ────────────────────────────────────────────────────
cotes = {}
for st in (AMONT, AVAL):
    df, _ = charger_serie_propre(st, '0001', con, debut=DEBUT, fin=FIN)
    cotes[st] = df
    print("=" * 70)
    print(f"COTE — {NOM[st]}   [{st}]")
    print("=" * 70)
    if df.empty:
        print("   aucune mesure valide sur cette période\n")
        continue
    v = df['value']
    print(f"  {len(df)} point(s) · min {v.min():.1f} · médiane {v.median():.1f} "
          f"· max {v.max():.1f} cm")
    print(f"  du {df['timestamp'].min():%d/%m %H:%M} "
          f"au {df['timestamp'].max():%d/%m %H:%M}")
    print()
    for l in sparkline(df):
        print("  " + l)
    print()

# ── PLUIE ────────────────────────────────────────────────────
print("=" * 70)
print("PLUIE sur la période (cumuls horaires)")
print("=" * 70)
for st in (AMONT, AVAL):
    df, _ = charger_serie_propre(st, '0006', con, debut=DEBUT, fin=FIN)
    if df.empty:
        print(f"  {NOM[st]} : aucune donnée")
        continue
    horaire = (df.set_index('timestamp')['value'].resample('h').sum())
    pluvieux = horaire[horaire > 0]
    print(f"\n  {NOM[st]} : cumul {df['value'].sum():.1f} mm sur la période")
    if pluvieux.empty:
        print("     aucune heure pluvieuse — période SÈCHE")
    else:
        print(f"     {len(pluvieux)} heure(s) pluvieuse(s) :")
        for t, v in pluvieux.items():
            print(f"       {t:%d/%m %H:%M} : {v:.1f} mm")

# ── LECTURE CROISÉE ──────────────────────────────────────────
print("\n" + "=" * 70)
print("LECTURE CROISÉE — y a-t-il propagation amont → aval ?")
print("=" * 70)

am, av = cotes[AMONT], cotes[AVAL]
if am.empty or av.empty:
    print("  Comparaison impossible : une des deux stations n'a pas de cote "
          "valide sur cette période.")
else:
    # moment et ampleur du maximum de chaque station
    i_am = am['value'].idxmax()
    i_av = av['value'].idxmax()
    t_am, v_am = am.loc[i_am, 'timestamp'], am.loc[i_am, 'value']
    t_av, v_av = av.loc[i_av, 'timestamp'], av.loc[i_av, 'value']
    base_am, base_av = am['value'].median(), av['value'].median()

    print(f"\n  AMONT  pic {v_am:.1f} cm le {t_am:%d/%m %H:%M} "
          f"(médiane {base_am:.1f} → montée de {v_am - base_am:+.1f} cm)")
    print(f"  AVAL   pic {v_av:.1f} cm le {t_av:%d/%m %H:%M} "
          f"(médiane {base_av:.1f} → montée de {v_av - base_av:+.1f} cm)")

    decalage_h = (t_av - t_am).total_seconds() / 3600
    print(f"\n  Décalage des pics : {decalage_h:+.1f} h")

    # amplitude relative : une vraie crue sort nettement de l'étiage
    amp_am = (v_am - base_am) / max(base_am, 1) * 100
    amp_av = (v_av - base_av) / max(base_av, 1) * 100
    print(f"  Ampleur relative  : amont {amp_am:+.0f} %  ·  aval {amp_av:+.0f} %")

    print("\n  Interprétation (à confirmer par l'œil sur les graphiques) :")
    if amp_am > 30 and amp_av < 10:
        print("    ⚠️  L'amont monte franchement, l'aval ne réagit pas.")
        print("        Sur cette paire, un BARRAGE (Mellegue) sépare les deux")
        print("        stations : c'est exactement ce que la colonne")
        print("        `fiabilite = à valider` de reseau_stations.csv annonce.")
        print("        → une règle de cohérence de crue ne DOIT PAS s'appliquer")
        print("          telle quelle à ce type de paire.")
    elif amp_am > 30 and amp_av > 10 and 0 < decalage_h < 48:
        print("    ✅ L'amont monte, l'aval suit avec un décalage plausible.")
        print("        La propagation est visible malgré le barrage.")
    elif amp_am < 10 and amp_av < 10:
        print("    ℹ️  Aucune des deux stations ne montre de crue nette sur")
        print("        cette période : rien à valider ici, choisir une autre")
        print("        fenêtre temporelle.")
    else:
        print("    ❔ Situation ambiguë — regarder les graphiques ci-dessus")
        print("        avant de conclure.")

con.close()
print("\nObservation terminée.")
