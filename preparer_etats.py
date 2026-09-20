"""
preparer_etats.py — Transforme la feuille de suivi DGRE en etats_stations.csv
=============================================================================
La feuille Excel du réseau (102 stations) n'a pas d'en-têtes exploitables :
les colonnes utiles s'appellent « Unnamed: 5 » à « Unnamed: 12 ». Ce script
la nettoie UNE FOIS pour toutes et produit le fichier de référence du site :

    etats_stations.csv

Ce que le script fait :
  1. Renomme les colonnes en clair.
  2. Calcule l'ÉTAT de chaque station par croisement de deux colonnes
     (« transmet ? » x « observation »), car l'état ne se lit pas dans une
     seule colonne — une station peut transmettre tout en ayant une panne.
  3. Normalise le vocabulaire des pannes (fautes de frappe, pannes multiples).
  4. Convertit les coordonnées UTM en latitude/longitude pour la carte Leaflet.

Lancer :
    py preparer_etats.py

À relancer uniquement si la DGRE met la feuille à jour.

⚠ IMPORTANT — etats_stations.csv est une RÉFÉRENCE EN LECTURE SEULE.
   La colonne s'appelle `etat_dgre` et non `etat`, parce que le système
   manipule trois notions distinctes qu'il ne faut jamais confondre :

     etat_dgre     ce que la DGRE a déclaré      ← ce fichier, jamais réécrit
     etat_detecte  ce que l'agent observe        ← calculé depuis historique.db
     etat_affiche  ce que le tableau de bord montre
                   = etat_dgre, corrigé par les changements VALIDÉS par un
                     humain après vérification sur le terrain

   Aucun programme n'écrit dans ce fichier. Les changements d'état validés
   sont enregistrés dans historique.db (tables propositions_etat et
   historique_etats), ce qui permet de retrouver à tout moment l'état
   d'origine et l'historique complet des modifications.
"""

import math
import sys
import unicodedata
from pathlib import Path

import pandas as pd

DOSSIER = Path(__file__).parent
FEUILLE = DOSSIER / 'Feuille_de_calcul_sans_titre.xlsx'   # ← adapte le nom
SORTIE  = DOSSIER / 'etats_stations.csv'
CONFIG  = DOSSIER / 'config_stations.csv'                 # pour récupérer altitude_m

# ─────────────────────────────────────────────
# ⚠ SYSTÈME DE COORDONNÉES — point de vigilance connu du projet
# ─────────────────────────────────────────────
# Les x/y de la feuille sont en UTM zone 32N, mais SUR QUEL DATUM ?
#   'EPSG:22332' = Carthage / UTM 32N  → datum tunisien, celui des shapefiles DGRE
#   'EPSG:32632' = WGS84   / UTM 32N   → datum mondial, celui utilisé par altitudes.py
# Se tromper ne provoque AUCUNE erreur : ça décale simplement toutes les
# stations d'environ 460 m, en silence. Vérifie ce réglage avant de conclure
# qu'une station est mal placée sur la carte.
CRS_SOURCE = 'EPSG:22332'

# ─────────────────────────────────────────────
# Correspondance colonnes brutes → noms en clair
# ─────────────────────────────────────────────
RENOMMAGE = {
    'gouv':        'gouvernorat',
    'code':        'code_station',
    'nom':         'nom',
    'Unnamed: 3':  'code_interne',
    'type':        'type_station',
    'Unnamed: 5':  'capteur',
    'Unnamed: 6':  'nb_capteurs',
    'Unnamed: 7':  'transmission_etat',
    'Unnamed: 8':  'mode_transmission',
    'Unnamed: 9':  'observation',
    'x':           'x',
    'y':           'y',
    'Unnamed: 12': 'date_reference',
}

# Vocabulaire des pannes : brut → normalisé
# (« RAS » = Rien À Signaler : ce n'est pas une panne, c'est son absence)
PANNES = {
    'ras':                                  None,
    'batterie':                             'Batterie',
    'modem':                                'Modem',
    'sonde':                                'Sonde',
    'radar':                                'Radar',
    'duosens':                              'Duosens',
    'panneau solaire':                      'Panneau solaire',
    'vendalisee':                           'Vandalisme',
    'pas de couverture reseau en gsm data': 'Couverture réseau',
    'recuperation du station a la dgre':    'Station retirée (DGRE)',
}


def _sans_accents(s: str) -> str:
    """'Récuperation' → 'recuperation' — pour comparer sans se soucier des accents."""
    s = unicodedata.normalize('NFD', str(s))
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn').strip().lower()


def normaliser_pannes(observation) -> list[str]:
    """'Modem+Batterie' → ['Modem', 'Batterie'] · 'RAS' → []"""
    if pd.isna(observation):
        return []
    pannes = []
    for morceau in str(observation).split('+'):
        cle = _sans_accents(morceau)
        if cle in PANNES:
            valeur = PANNES[cle]
            if valeur:
                pannes.append(valeur)
        elif cle:
            pannes.append(str(morceau).strip())   # panne inconnue : on la garde telle quelle
    return pannes


def calculer_etat(transmet: bool, pannes: list[str]) -> str:
    """L'état résulte du CROISEMENT de deux colonnes, pas d'une seule.

    - ne transmet pas          → arret     (la donnée n'arrive plus)
    - transmet + aucune panne  → marche    (station saine)
    - transmet + panne connue  → critique  (elle fonctionne encore, mais un
                                            défaut est déclaré : c'est elle
                                            qu'il faut traiter en priorité
                                            avant qu'elle ne bascule en arrêt)
    """
    if not transmet:
        return 'arret'
    return 'critique' if pannes else 'marche'


# ─────────────────────────────────────────────
# Conversion UTM → latitude/longitude
# ─────────────────────────────────────────────

def convertir_coordonnees(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute lat/lon. Utilise pyproj (exact) si disponible, sinon une
    conversion WGS84 de secours issue de altitudes.py."""
    try:
        from pyproj import Transformer
        tr = Transformer.from_crs(CRS_SOURCE, 'EPSG:4326', always_xy=True)
        lon, lat = tr.transform(df['x'].values, df['y'].values)
        df['lat'], df['lon'] = lat, lon
        print(f"  coordonnées converties depuis {CRS_SOURCE} (pyproj)")
    except ImportError:
        print("  ⚠ pyproj absent — conversion de secours en WGS84/UTM 32N.")
        print("    Si CRS_SOURCE = EPSG:22332, les positions seront décalées")
        print("    d'environ 460 m. Installe pyproj :  py -m pip install pyproj")
        lat, lon = zip(*[_utm_wgs84_vers_latlon(x, y)
                         for x, y in zip(df['x'], df['y'])])
        df['lat'], df['lon'] = lat, lon
    return df


def _utm_wgs84_vers_latlon(easting: float, northing: float, zone: int = 32):
    """Conversion UTM 32N (WGS84) → degrés décimaux, sans dépendance externe.
    Reprise de altitudes.py pour rester cohérent avec le reste du projet."""
    a, f = 6378137.0, 1 / 298.257223563
    e2, k0 = f * (2 - f), 0.9996
    x, y = easting - 500000.0, northing
    m = y / k0
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    ep2 = e2 / (1 - e2)
    C1 = ep2 * math.cos(phi1) ** 2
    T1 = math.tan(phi1) ** 2
    N1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    R1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    D = x / (N1 * k0)
    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D ** 2 / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2 - 9 * ep2) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 ** 2 - 252 * ep2 - 3 * C1 ** 2) * D ** 6 / 720)
    lon = (D
           - (1 + 2 * T1 + C1) * D ** 3 / 6
           + (5 - 2 * C1 + 28 * T1 - 3 * C1 ** 2 + 8 * ep2 + 24 * T1 ** 2) * D ** 5 / 120
           ) / math.cos(phi1)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return math.degrees(lat), math.degrees(lon0 + lon)


# ─────────────────────────────────────────────
# PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

def main(feuille: Path = FEUILLE):
    if not feuille.exists():
        sys.exit(f"Feuille introuvable : {feuille}\n"
                 "Adapte la constante FEUILLE en haut du script.")

    df = pd.read_excel(feuille)
    df = df.rename(columns=RENOMMAGE)

    # Nettoyage de base
    df['code_station'] = df['code_station'].astype(str).str.strip()
    for c in ('nom', 'gouvernorat', 'type_station', 'capteur', 'mode_transmission'):
        df[c] = df[c].astype(str).str.strip()

    # État
    df['transmet'] = (df['transmission_etat'].apply(_sans_accents) == 'transmet')
    df['pannes']   = df['observation'].apply(normaliser_pannes)
    df['etat_dgre']     = [calculer_etat(t, p) for t, p in zip(df['transmet'], df['pannes'])]
    df['nb_pannes']       = df['pannes'].apply(len)
    df['type_panne']      = df['pannes'].apply(lambda p: ' + '.join(p) if p else '')
    df['panne_principale'] = df['pannes'].apply(lambda p: p[0] if p else '')

    # Date de référence (nombres de série Excel → dates réelles)
    df['date_reference'] = pd.to_datetime(
        pd.to_numeric(df['date_reference'], errors='coerce'),
        unit='D', origin='1899-12-30', errors='coerce').dt.date

    # Coordonnées
    df = convertir_coordonnees(df)

    # Altitude reprise de config_stations.csv si disponible
    if CONFIG.exists():
        cfg = pd.read_csv(CONFIG, sep=';', encoding='utf-8-sig',
                          dtype={'code_station': str})
        if 'altitude_m' in cfg.columns:
            df = df.merge(cfg[['code_station', 'altitude_m']],
                          on='code_station', how='left')

    colonnes = ['code_station', 'nom', 'gouvernorat', 'type_station', 'capteur',
                'mode_transmission', 'transmet', 'etat_dgre', 'type_panne',
                'panne_principale', 'nb_pannes', 'observation',
                'date_reference', 'x', 'y', 'lat', 'lon']
    colonnes += [c for c in ('altitude_m',) if c in df.columns]

    sortie = df[colonnes]
    sortie.to_csv(SORTIE, sep=';', index=False, encoding='utf-8-sig')

    # ── Bilan à l'écran ──
    print(f"\n💾 {SORTIE.name} — {len(sortie)} stations\n")
    libelle = {'marche': '🟢 En marche', 'critique': '🔴 Critique', 'arret': '🔵 En arrêt'}
    for etat, n in sortie['etat_dgre'].value_counts().items():
        print(f"  {libelle.get(etat, etat):<15} {n:>3}")

    crit = sortie[sortie['etat_dgre'] == 'critique']
    if len(crit):
        print(f"\n  Stations qui transmettent AVEC une panne déclarée ({len(crit)}) :")
        for _, r in crit.iterrows():
            print(f"    {r['code_station']}  {r['nom']:<22} {r['gouvernorat']:<12} → {r['type_panne']}")

    print("\n  Pannes les plus fréquentes :")
    toutes = df.explode('pannes')['pannes'].dropna()
    for p, n in toutes.value_counts().items():
        print(f"    {p:<25} {n:>3}")

    print(f"\n  Emprise géographique : lat {sortie['lat'].min():.3f}–{sortie['lat'].max():.3f}"
          f" · lon {sortie['lon'].min():.3f}–{sortie['lon'].max():.3f}")
    print("  Contrôle : la Tunisie va d'environ 30,2° à 37,5° N et 7,5° à 11,6° E.")


if __name__ == '__main__':
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else FEUILLE)
