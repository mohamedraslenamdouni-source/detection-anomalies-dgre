"""
diagnostic_silences.py — Pourquoi telle station est-elle « muette » ?
======================================================================
Ne relit AUCUN fichier .MIS : interroge directement historique.db (déjà
construit par preparer_demo.py) pour montrer, station par station, la date
de sa dernière mesure — et si elle dépasse le seuil de silence (6 h par
rapport à la donnée la plus récente du réseau).

But : distinguer deux cas très différents qui donnent le même symptôme
(« station_muette ») :
  A) La station a vraiment arrêté de transmettre avant les autres — c'est
     une vraie anomalie, et c'est un bon exemple à montrer en démo.
  B) Les fichiers fournis par la société pour cette région s'arrêtent
     simplement plus tôt que les autres (couverture différente selon la
     région) — ce n'est pas une panne, juste un artefact du jeu de données
     de démo. Dans ce cas rien à corriger dans le code : soit on l'ignore
     en présentant la démo, soit on dépose des fichiers plus récents pour
     cette région si on les a.

Lancer :
    py diagnostic_silences.py
"""

import pandas as pd

import memoire
from mis_parser import STATION_NAMES, STATION_INFO

SEUIL_H = memoire.LT['silence_h']   # 6h par défaut, cohérent avec la vraie règle L3


def main():
    con = memoire.connexion()
    stats = memoire.stats_memoire(con)
    if not stats['fin']:
        print("Mémoire vide — lance d'abord preparer_demo.py.")
        return

    ref = pd.Timestamp(stats['fin'])
    print(f"Donnée la plus récente de TOUT le réseau : {ref}\n")

    lignes = []
    for station, sensor in memoire.series_presentes(con):
        last = pd.Timestamp(con.execute(
            "SELECT MAX(timestamp) FROM mesures WHERE station=? AND sensor=?",
            (station, sensor)).fetchone()[0])
        retard_h = (ref - last).total_seconds() / 3600
        lignes.append({
            'station':   station,
            'nom':       STATION_NAMES.get(station, '— hors liste —'),
            'gouv':      STATION_INFO.get(station, {}).get('gouvernorat', ''),
            'capteur':   sensor,
            'derniere':  last,
            'retard_h':  retard_h,
        })

    df = pd.DataFrame(lignes).sort_values('derniere')

    # Vue par STATION (pire capteur = celui qui a le plus de retard)
    par_station = (df.sort_values('retard_h', ascending=False)
                     .drop_duplicates(subset='station'))

    print(f"{'Station':<12} {'Nom':<22} {'Gouv.':<12} {'Dernière donnée':<18} {'Retard':>10}  Muette (≥{SEUIL_H}h) ?")
    print("─" * 92)
    for r in par_station.itertuples():
        muette = "🔴 OUI" if r.retard_h >= SEUIL_H else "—"
        retard_txt = f"{r.retard_h/24:.1f} j" if r.retard_h >= 24 else f"{r.retard_h:.1f} h"
        print(f"{r.station:<12} {r.nom[:22]:<22} {str(r.gouv)[:12]:<12} "
              f"{str(r.derniere):<18} {retard_txt:>10}  {muette}")

    n_muettes = (par_station['retard_h'] >= SEUIL_H).sum()
    print(f"\n{n_muettes}/{len(par_station)} station(s) dépassent le seuil de silence ({SEUIL_H} h).")

    # Indice « artefact vs vraie panne » : les dates de fin se regroupent-elles ?
    fins = par_station.loc[par_station['retard_h'] >= SEUIL_H, 'gouv']
    if len(fins):
        print("\nRépartition des stations muettes par gouvernorat :")
        print(fins.value_counts().to_string())
        print("\n→ Si presque toutes les stations muettes viennent d'un même gouvernorat "
              "(ou de plusieurs, mais avec la MÊME date de dernière donnée), c'est très "
              "probablement un artefact de couverture des fichiers fournis, pas une vraie panne.")
        print("→ Si elles sont dispersées entre plusieurs gouvernorats avec des dates de fin "
              "différentes, ce sont probablement de vraies interruptions dans les données réelles.")


if __name__ == '__main__':
    main()
