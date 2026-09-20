"""
preparer_demo.py — Amorce la mémoire pour la démonstration DGRE
===============================================================
CONTEXTE
  Avant : seules les 2 stations du Kef étaient en mémoire.
  Maintenant : un sous-dossier par gouvernorat (Ariana, Beja, Ben_Arous,
  Jendouba, Monastir, Siliana…), chacun contenant des fichiers .MIS qui
  couvrent du 23/06/2026 au 31/08/2026.

OBJECTIF
  Le jour de la démo, l'évaluateur dépose UNIQUEMENT le(s) fichier(s) du
  dernier jour (31/08) et le tableau de bord doit afficher les trois familles
  d'anomalies :
    • court terme      → sur le(s) fichier(s) déposé(s)   (mis_parser.py)
    • long terme       → sur TOUT l'historique en mémoire (memoire.py)
    • inter-stations   → en comparant les stations entre elles
  Pour cela, la mémoire (historique.db) doit DÉJÀ contenir le 23/06 → 30/08.

CE QUE FAIT CE SCRIPT
  1. Parcourt récursivement le dossier des gouvernorats et lit tous les .MIS.
  2. Coupe chaque série à une DATE PIVOT (défaut 2026-08-31) :
       - horodatage AVANT le pivot  → injecté dans historique.db (la mémoire)
       - horodatage LE JOUR du pivot → réécrit dans  demo/<gouvernorat>_<pivot>.MIS
  3. Affiche un bilan : stations vues, stations absentes de config, points/jour,
     et un aperçu des anomalies long terme déjà présentes en mémoire.

LE JOUR J
  On dépose dans le dashboard :
    • demo/<gouvernorat>_20260831.MIS   (la région testée), ou
    • les 6 fichiers demo/*_20260831.MIS ensemble (dépôt multiple) pour un
      réseau entièrement à jour — voir la note « stations muettes » plus bas.

LANCEMENT
    py preparer_demo.py --dossier "chemin/vers/les/gouvernorats"
    py preparer_demo.py --dossier . --pivot 2026-08-31 --reset
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from mis_parser import parse_mis, analyse_serie, STATION_NAMES
import memoire


def _diagnostiquer_ligne_invalide(chemin: Path, max_lignes: int = 3):
    """Cherche la/les ligne(s) '<STATION>' mal formée(s) dans un fichier qui a
    fait planter parse_mis, et retourne [(num_ligne, contenu_brut), ...] pour
    qu'on puisse voir ce qui diffère du format attendu."""
    trouvees = []
    try:
        with open(chemin, 'r', encoding='utf-8', errors='replace') as f:
            for i, raw in enumerate(f, start=1):
                line = raw.strip()
                if line.startswith('<STATION>') and not re.search(
                        r'<STATION>(.*?)</STATION>', line):
                    trouvees.append((i, raw.rstrip('\n')))
                    if len(trouvees) >= max_lignes:
                        break
    except Exception:
        pass
    return trouvees


# ─────────────────────────────────────────────
# Réécriture d'un DataFrame parsé au format .MIS
# ─────────────────────────────────────────────
def _valeur_mis(v, code):
    """Reproduit une cellule .MIS : un nombre, ou une ligne 'capteur muet'."""
    if pd.isna(v):
        return f"---/[{code}]" if pd.notna(code) else "---"
    return f"{v:g}"


def ecrire_mis(df: pd.DataFrame, chemin: Path):
    """Écrit df (colonnes station, sensor, timestamp, value, code) en .MIS,
    un bloc <STATION>/<SENSOR> par série, dans l'ordre chronologique."""
    with open(chemin, "w", encoding="utf-8") as f:
        for (station, sensor), g in df.groupby(["station", "sensor"], sort=True):
            f.write(f"<STATION>{station}</STATION><SENSOR>{sensor}</SENSOR>\n")
            for r in g.sort_values("timestamp").itertuples():
                ts = r.timestamp
                f.write(f"{ts:%Y%m%d};{ts:%H:%M:%S};{_valeur_mis(r.value, r.code)}\n")


# ─────────────────────────────────────────────
# Préparation
# ─────────────────────────────────────────────
def preparer(dossier: str, pivot: str, reset: bool, sortie: str):
    pivot_d = datetime.strptime(pivot, "%Y-%m-%d").date()
    dossier = Path(dossier)
    sortie = Path(sortie)
    sortie.mkdir(exist_ok=True)

    if reset and memoire.DB_PATH.exists():
        memoire.DB_PATH.unlink()
        print(f"🗑  {memoire.DB_PATH.name} supprimée — la mémoire repart de zéro.\n")

    fichiers = sorted(p for p in dossier.rglob("*") if p.suffix.lower() == ".mis")
    if not fichiers:
        raise SystemExit(f"Aucun fichier .MIS trouvé sous « {dossier} ».")

    con = memoire.connexion()

    demo_par_gouv: dict[str, list] = {}   # gouvernorat → blocs du jour pivot
    vus, absents = set(), set()
    n_hist = 0
    n_blocs = 0
    erreurs = []   # (chemin, message_erreur, [(num_ligne, ligne_brute), ...])

    print(f"📂 {len(fichiers)} fichier(s) .MIS · date pivot = {pivot_d:%d/%m/%Y}\n")

    for mis in fichiers:
        gouv = mis.parent.name
        try:
            blocs = parse_mis(str(mis))
        except Exception as e:
            erreurs.append((mis, f"{type(e).__name__}: {e}",
                            _diagnostiquer_ligne_invalide(mis)))
            continue

        for bloc in blocs:
            n_blocs += 1
            station = bloc["station"].iloc[0]
            vus.add(station)
            if STATION_NAMES and station not in STATION_NAMES:
                absents.add(station)

            jours = bloc["timestamp"].dt.date
            hist = bloc[jours < pivot_d]
            jour = bloc[jours == pivot_d]

            # 1) historique → mémoire (analyse_serie fournit value_raw/corrected)
            if len(hist):
                df_a, _ = analyse_serie(hist.reset_index(drop=True))
                n_hist += memoire.ajouter_mesures(df_a, con)

            # 2) jour pivot → fichier démo de sa région
            if len(jour):
                demo_par_gouv.setdefault(gouv, []).append(jour)

    # ── fichiers qui n'ont pas pu être lus du tout ──
    if erreurs:
        print(f"⚠ {len(erreurs)} fichier(s) n'ont PAS pu être lus (ignorés — "
              f"aucune de leurs données n'est en mémoire) :\n")
        for chemin, err, diag in erreurs[:10]:
            print(f"  ✗ {chemin.relative_to(dossier)}")
            print(f"      erreur : {err}")
            for num, raw in diag:
                print(f"      ligne {num} telle quelle : {raw!r}")
        if len(erreurs) > 10:
            print(f"  … et {len(erreurs) - 10} autre(s) fichier(s) similaire(s).")
        print("\n  ⚠️ IMPORTANT : app.py utilise le MÊME parseur (mis_parser.py). "
              "Si un fichier de ce type est déposé le jour de la démo, le "
              "dashboard plantera pareil. Montre les lignes ci-dessus pour qu'on "
              "corrige mis_parser.py une bonne fois — pas seulement ce script.\n")
    else:
        print("✓ Tous les fichiers ont été lus sans erreur.\n")

    # ── écrit un fichier démo par gouvernorat ──
    print("── Fichiers de démo (à déposer le jour J) ──")
    if not demo_par_gouv:
        print(f"  ⚠ Aucune donnée au {pivot_d:%d/%m/%Y} : vérifie la date pivot "
              f"ou le contenu des dossiers.")
    for gouv in sorted(demo_par_gouv):
        dfj = pd.concat(demo_par_gouv[gouv], ignore_index=True)
        chemin = sortie / f"{gouv}_{pivot_d:%Y%m%d}.MIS"
        ecrire_mis(dfj, chemin)
        print(f"  📤 {chemin.name:<28} {len(dfj):>6} mesures · "
              f"{dfj['station'].nunique()} station(s)")

    # ── bilan mémoire ──
    s = memoire.stats_memoire(con)
    print("\n── Mémoire amorcée (historique.db) ──")
    print(f"  {n_hist} mesure(s) ajoutée(s) sur cette exécution")
    print(f"  total : {s['mesures']} mesures · {s['stations']} stations · "
          f"du {s['debut']} au {s['fin']}")
    print(f"  séries station-capteur vues : {n_blocs} · stations distinctes : {len(vus)}")

    if absents:
        print(f"\n  ⚠ {len(absents)} station(s) hors config_stations.csv "
              f"(seuils par défaut) : {', '.join(sorted(absents))}")
    else:
        print("  ✓ Toutes les stations vues sont connues de config_stations.csv.")

    # ── aperçu des anomalies long terme déjà en mémoire ──
    from collections import Counter
    compte = Counter()
    for st_, se_ in memoire.series_presentes(con):
        for a in memoire.analyser_long_terme(st_, se_, con):
            compte[a["type"]] += 1
    for a in memoire.detecter_silences(con):
        compte[a["type"]] += 1

    print("\n── Aperçu anomalies LONG TERME déjà détectables en mémoire ──")
    if compte:
        for t, n in compte.most_common():
            print(f"  • {t:<26} {n}")
        print("  (elles s'afficheront dans le panneau « long terme » du dashboard)")
    else:
        print("  Aucune pour l'instant — historique peut-être encore court, "
              "ou données très saines.")
    print("\n✅ Prêt. Lance le dashboard, dépose un fichier de demo/ et observe.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Amorce la mémoire DGRE pour la démo.")
    ap.add_argument("--dossier", default=".",
                    help="Dossier contenant les sous-dossiers par gouvernorat.")
    ap.add_argument("--pivot", default="2026-08-31",
                    help="Date pivot AAAA-MM-JJ : ce jour = démo, avant = mémoire.")
    ap.add_argument("--sortie", default="demo",
                    help="Dossier où écrire les fichiers .MIS du jour pivot.")
    ap.add_argument("--reset", action="store_true",
                    help="Supprime historique.db avant de recharger (démo propre).")
    args = ap.parse_args()
    preparer(args.dossier, args.pivot, args.reset, args.sortie)
