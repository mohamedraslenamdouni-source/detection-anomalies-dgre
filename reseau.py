"""
reseau.py — Graphe hydrologique amont-aval du réseau DGRE
==========================================================
Construit la topologie « qui envoie l'eau à qui » à partir des couches SIG,
sans MNT : le tracé des oueds suit déjà les vallées, donc deux stations
reliées par un chemin continu de tronçons sont hydrologiquement connectées.

Principe :
  1. Chaque tronçon d'oued devient une arête orientée (départ → arrivée).
     Vérifié sur les données : le réseau est numérisé dans le sens de
     l'écoulement à 99,5 %, contrôlé par l'ordre de Strahler.
  2. Le tracé s'interrompt souvent à l'entrée d'un barrage ou d'un lac : le
     polygone de la retenue sert alors de connecteur physique entre les
     tronçons qui y arrivent et ceux qui en repartent. Ces franchissements
     sont tracés (colonne `franchit`) car un barrage rompt la continuité
     naturelle : le débit aval dépend de décisions de gestion, pas de la pluie.
  3. Les interruptions qu'aucun barrage ni plan d'eau n'explique sont listées
     dans `arrets_non_resolus` — trous de numérisation à signaler.
  4. Chaque station est rattachée au tronçon le plus proche, puis un parcours
     du graphe donne les relations amont/aval, distances et obstacles.

Prérequis :  py -m pip install geopandas networkx
Shapefiles attendus dans le sous-dossier `sig/` :
    Cours_d_eau_Project.shp   stations.shp
    Grands_Barrages_Project.shp   Plans_d_eau_Project.shp

Utilisation :
    py reseau.py                     # construit, exporte les CSV, affiche un bilan
    from reseau import ReseauHydro
    r = ReseauHydro().construire()
    r.aval_de('1485400110')          # {code: (distance_m, nb_barrages)}
    r.amont_de('1485101210')
    r.arrets_non_resolus             # DataFrame des trous de données
"""

from pathlib import Path

import pandas as pd

try:
    import geopandas as gpd
    import networkx as nx
    from shapely.geometry import Point
except ImportError as e:
    raise SystemExit(
        f"Dépendance manquante ({e.name}).\n"
        "Installe-les avec :  py -m pip install geopandas networkx"
    )


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
DOSSIER_SIG = Path(__file__).parent / 'sig'

F_COURS_EAU = 'Cours_d_eau_Project.shp'
F_STATIONS  = 'stations.shp'
F_BARRAGES  = 'Grands_Barrages_Project.shp'
F_PLANS_EAU = 'Plans_d_eau_Project.shp'

TOL_NOEUD     = 1.0      # m — tolérance de raccordement entre extrémités
DIST_MAX_SNAP = 500.0    # m — au-delà, la station n'est pas « sur le réseau »
BUFFER_POLY   = 500.0    # m — marge autour d'une retenue

# ── Trous de numérisation confirmés par vérification terrain ──────────────
# (imagerie satellite Google Earth/Maps — voir Synthese_projet_DGRE, section 5)
# Colonnes attendues : num;oued;x;y
F_TROUS_CONFIRMES = 'trous_numerisation_confirmes.csv'

# Deux rayons, selon la force de la preuve que le tronçon visé est bien la
# suite du même cours d'eau :
#   - même nom d'oued  → preuve forte, on tolère un écart important
#   - nom différent ou absent → simple proximité, on reste strict
RAYON_MEME_NOM = 2000.0
RAYON_ANONYME  =  600.0

# CRS des colonnes x/y du fichier de trous. Il provient de arrets_classes.csv,
# lui-même issu des shapefiles « _Project » reprojetés en WGS84 UTM 32N.
# Si les shapefiles chargés sont dans la projection d'origine des données DGRE
# (Carthage UTM 32N = EPSG:22332), les points sont reprojetés automatiquement :
# environ 460 m séparent ces deux systèmes en Tunisie.
CRS_TROUS = 'EPSG:32632'


def _cle(pt, tol=TOL_NOEUD):
    return (round(pt[0] / tol) * tol, round(pt[1] / tol) * tol)


def _extremites(geom):
    coords = (list(geom.coords) if geom.geom_type == 'LineString'
              else list(geom.geoms[0].coords))
    return _cle(coords[0]), _cle(coords[-1])


class ReseauHydro:
    """Graphe orienté du réseau hydrographique, avec stations, barrages et lacs."""

    def __init__(self, dossier=DOSSIER_SIG):
        self.dossier  = Path(dossier)
        self.G        = nx.DiGraph()
        self.troncons = None
        self.stations = None
        self.barrages = None
        self.plans    = None
        self.troncons_barres    = set()
        self.arrets_non_resolus = pd.DataFrame()
        self.n_raccords = {'barrage': 0, 'plan_eau': 0}

    # ─────────────────────────────────────────
    # CONSTRUCTION
    # ─────────────────────────────────────────
    def construire(self, verbose=True):
        self._charger(verbose)
        self._batir_graphe(verbose)
        self._raccorder_retenues(verbose)
        self._raccorder_trous_numerisation(verbose)
        self._recenser_arrets(verbose)
        self._rattacher_stations(verbose)
        self._marquer_troncons_barres(verbose)
        return self

    def _chemin(self, nom, obligatoire=True):
        for p in (self.dossier / nom, Path(__file__).parent / nom):
            if p.exists():
                return p
        if obligatoire:
            raise SystemExit(f"Fichier SIG introuvable : {self.dossier / nom}")
        return None

    def _charger(self, verbose):
        self.troncons = gpd.read_file(self._chemin(F_COURS_EAU))
        self.stations = gpd.read_file(self._chemin(F_STATIONS))
        for attr, f in (('barrages', F_BARRAGES), ('plans', F_PLANS_EAU)):
            p = self._chemin(f, obligatoire=False)
            setattr(self, attr, gpd.read_file(p) if p else None)
        if verbose:
            n_b = len(self.barrages) if self.barrages is not None else 0
            n_p = len(self.plans)    if self.plans    is not None else 0
            print(f"  {len(self.troncons)} tronçons · {len(self.stations)} stations "
                  f"· {n_b} barrages · {n_p} plans d'eau")

    def _batir_graphe(self, verbose):
        """Une arête orientée par tronçon : départ de ligne → arrivée de ligne."""
        for i, row in self.troncons.iterrows():
            amont, aval = _extremites(row.geometry)
            if amont != aval:
                self.G.add_edge(amont, aval, troncon=i,
                                longueur=float(row.geometry.length),
                                ordre=row.get('ORD_STR'), nom=row.get('LIB_FR'),
                                ssh=row.get('COD_SSH'), franchit=None)
        if verbose:
            n_comp = nx.number_weakly_connected_components(self.G)
            print(f"  graphe brut : {self.G.number_of_nodes()} nœuds, "
                  f"{self.G.number_of_edges()} arêtes, {n_comp} morceaux séparés")

    def _raccorder_retenues(self, verbose):
        """Relie les tronçons qui ENTRENT dans une retenue à ceux qui en SORTENT.

        Le polygone du barrage ou du lac est le connecteur physique : l'eau
        traverse réellement la retenue. Chaque arête créée est marquée
        (`franchit`) pour que les règles d'anomalies sachent qu'un ouvrage ou
        une nappe se trouve entre les deux stations.
        """
        noeuds = list(self.G.nodes)
        pts = gpd.GeoSeries([Point(n) for n in noeuds], crs=self.troncons.crs)

        for couche, label in ((self.barrages, 'barrage'), (self.plans, 'plan_eau')):
            if couche is None:
                continue
            for _, poly in couche.iterrows():
                zone = poly.geometry.buffer(BUFFER_POLY)
                dedans = [n for n, p in zip(noeuds, pts) if zone.contains(p)]
                if len(dedans) < 2:
                    continue
                entrees = [n for n in dedans if self.G.out_degree(n) == 0]
                sorties = [n for n in dedans if self.G.in_degree(n) == 0]
                for e in entrees:
                    for s in sorties:
                        if e != s and not self.G.has_edge(e, s):
                            self.G.add_edge(e, s, troncon=-1, longueur=0.0,
                                            ordre=None, nom=f'(via {label})',
                                            ssh=None, franchit=label)
                            self.n_raccords[label] += 1
        if verbose:
            n_comp = nx.number_weakly_connected_components(self.G)
            print(f"  raccords via retenues : {self.n_raccords['barrage']} barrage(s), "
                  f"{self.n_raccords['plan_eau']} plan(s) d'eau "
                  f"→ {n_comp} morceaux restants")

    def _raccorder_trous_numerisation(self, verbose):
        """Raccorde les interruptions confirmées manuellement comme « trou de
        numérisation » (vérification terrain sur imagerie satellite).

        Différence de nature avec _raccorder_retenues : aucun obstacle physique
        n'est traversé ici. Le tracé aval EXISTE déjà dans le shapefile, il n'a
        simplement jamais été raccordé lors de la numérisation. On cherche donc
        le TRONÇON le plus proche du point d'arrêt (la ligne entière, pas ses
        seules extrémités), en excluant les tronçons qui se TERMINENT sur ce
        point — ce sont des affluents entrants, pas la suite du tracé.

        Un tronçon portant le même nom d'oued est une preuve forte de
        continuité : on l'accepte jusqu'à RAYON_MEME_NOM. Sinon, seule la
        proximité plaide, et on se limite à RAYON_ANONYME.

        L'eau rejoint le tronçon retenu en un point quelconque de son tracé
        puis s'écoule jusqu'à son extrémité aval : c'est ce nœud que l'on relie.
        """
        self.n_raccords_trous = 0
        self.trous_non_raccordes = []
        chemin = self._chemin(F_TROUS_CONFIRMES, obligatoire=False)
        if chemin is None:
            if verbose:
                print(f"  ⚠ {F_TROUS_CONFIRMES} introuvable — aucune correction "
                      "de trou de numérisation appliquée")
            return

        confirmes = pd.read_csv(chemin, sep=';', encoding='utf-8-sig')

        # Reprojection si le fichier de trous et les shapefiles ne partagent
        # pas le même système de coordonnées (voir CRS_TROUS ci-dessus).
        pts = gpd.GeoSeries([Point(xy) for xy in zip(confirmes['x'], confirmes['y'])],
                            crs=CRS_TROUS)
        if self.troncons.crs is not None and pts.crs != self.troncons.crs:
            pts = pts.to_crs(self.troncons.crs)
            if verbose:
                print(f"  trous de numérisation reprojetés {CRS_TROUS} → "
                      f"{self.troncons.crs.to_string()}")

        arrets_libres = [n for n in self.G.nodes if self.G.out_degree(n) == 0]
        rayon_max = max(RAYON_MEME_NOM, RAYON_ANONYME)

        for (_, row), pt in zip(confirmes.iterrows(), pts):
            nom_arret = row['oued'] if pd.notna(row.get('oued')) else None
            etiquette = nom_arret or f"point #{row.get('num', '?')}"

            # nœud d'arrêt réel dans le graphe (la clé est arrondie à TOL_NOEUD)
            arret = _cle((pt.x, pt.y))
            if arret not in self.G.nodes:
                if not arrets_libres:
                    self.trous_non_raccordes.append(f"{etiquette} (graphe sans arrêt libre)")
                    continue
                arret = min(arrets_libres, key=lambda n: Point(n).distance(pt))
                if Point(arret).distance(pt) > TOL_NOEUD * 10:
                    self.trous_non_raccordes.append(f"{etiquette} (nœud d'arrêt introuvable)")
                    continue

            # candidats : tronçons proches qui ne se terminent PAS sur l'arrêt
            cands_nom, cands_autres = [], []
            for i in self.troncons.sindex.query(pt.buffer(rayon_max),
                                                predicate='intersects'):
                i = int(i)
                geom = self.troncons.geometry.iloc[i]
                d = float(geom.distance(pt))
                if d <= 1.0:
                    continue                      # le tronçon qui finit sur ce point
                _, aval_i = _extremites(geom)
                if aval_i == arret:
                    continue                      # affluent entrant, pas la suite
                if nom_arret and self.troncons.iloc[i].get('LIB_FR') == nom_arret:
                    if d <= RAYON_MEME_NOM:
                        cands_nom.append((d, i))
                elif d <= RAYON_ANONYME:
                    cands_autres.append((d, i))

            retenus = cands_nom or cands_autres
            if not retenus:
                self.trous_non_raccordes.append(
                    f"{etiquette} (aucune suite : ≤{RAYON_MEME_NOM:.0f} m même nom "
                    f"/ ≤{RAYON_ANONYME:.0f} m sinon)")
                continue

            # On essaie les candidats du plus proche au plus lointain, en
            # refusant tout raccord qui créerait une boucle : si l'eau peut
            # déjà revenir du nœud visé jusqu'à l'arrêt, alors ce nœud est en
            # AMONT, pas en aval — le raccorder ferait tourner l'eau en rond.
            raccorde = False
            for d_min, i_cible in sorted(retenus, key=lambda t: t[0]):
                geom = self.troncons.geometry.iloc[i_cible]
                _, aval = _extremites(geom)
                if aval == arret or self.G.has_edge(arret, aval):
                    continue
                if nx.has_path(self.G, aval, arret):
                    continue                      # créerait un cycle → écarté
                # longueur = trajet jusqu'au tronçon + parcours restant vers l'aval
                reste = float(geom.length - geom.project(pt))
                self.G.add_edge(arret, aval, troncon=-2, longueur=d_min + reste,
                                ordre=self.troncons.iloc[i_cible].get('ORD_STR'),
                                nom=f"(trou numérisation — {etiquette})",
                                ssh=None, franchit='trou_numerisation')
                self.n_raccords_trous += 1
                raccorde = True
                break
            if not raccorde:
                self.trous_non_raccordes.append(
                    f"{etiquette} (candidats écartés : boucle hydrologique)")

        if verbose:
            n_comp = nx.number_weakly_connected_components(self.G)
            print(f"  raccords trous de numérisation confirmés : "
                  f"{self.n_raccords_trous}/{len(confirmes)} → {n_comp} morceaux restants")
            if self.trous_non_raccordes:
                print("  ⚠ non raccordés (à traiter à la main) : "
                      + " · ".join(self.trous_non_raccordes))

    def _recenser_arrets(self, verbose):
        """Liste les interruptions du tracé qu'aucune retenue n'explique."""
        xmin, ymin, xmax, ymax = self.troncons.total_bounds
        marge = 3000.0

        lignes = []
        for n in self.G.nodes:
            if self.G.out_degree(n) != 0:
                continue                       # le tracé continue : pas un arrêt
            if (n[0] <= xmin + marge or n[0] >= xmax - marge
                    or n[1] <= ymin + marge or n[1] >= ymax - marge):
                continue                       # sortie de carte : mer ou frontière
            preds = list(self.G.predecessors(n))
            tr = self.G.edges[preds[0], n]['troncon'] if preds else None
            info = self.troncons.iloc[tr] if (tr is not None and tr >= 0) else None
            lignes.append({
                'x': n[0], 'y': n[1],
                'oued':        info['LIB_FR']  if info is not None else None,
                'ordre':       info['ORD_STR'] if info is not None else None,
                'sous_bassin': info['COD_SH']  if info is not None else None,
            })

        self.arrets_non_resolus = (
            pd.DataFrame(lignes).sort_values('ordre', ascending=False).reset_index(drop=True)
            if lignes else pd.DataFrame())

        if verbose and len(self.arrets_non_resolus):
            n_nommes = int(self.arrets_non_resolus['oued'].notna().sum())
            print(f"  ⚠ {len(self.arrets_non_resolus)} interruption(s) non expliquée(s) "
                  f"(dont {n_nommes} sur un oued nommé)")

    def _rattacher_stations(self, verbose):
        st = self.stations.copy()
        st['code'] = st['code'].astype(str).str.strip()

        j = gpd.sjoin_nearest(
            st[['code', 'nom', 'type', 'geometry']],
            self.troncons[['geometry']].reset_index(names='troncon'),
            how='left', distance_col='dist_troncon_m'
        ).drop_duplicates(subset='code').reset_index(drop=True)

        noeuds = []
        for t in j['troncon']:
            if pd.isna(t):
                noeuds.append(None)
            else:
                _, aval = _extremites(self.troncons.geometry.iloc[int(t)])
                noeuds.append(aval)
        j['noeud_aval'] = noeuds
        j['sur_reseau'] = j['dist_troncon_m'] <= DIST_MAX_SNAP

        self.stations = j.drop(columns='geometry', errors='ignore')
        if verbose:
            hyd = j[j['type'].astype(str).str.contains('Hydro', case=False, na=False)]
            print(f"  stations rattachées (≤ {DIST_MAX_SNAP:.0f} m) : "
                  f"{int(j['sur_reseau'].sum())}/{len(j)} "
                  f"— dont {int(hyd['sur_reseau'].sum())}/{len(hyd)} hydro")

    def _marquer_troncons_barres(self, verbose):
        """Tronçons situés dans l'emprise d'un barrage : comptés comme obstacles."""
        if self.barrages is None:
            return
        b = self.barrages.copy()
        b['geometry'] = b.geometry.centroid
        j = gpd.sjoin_nearest(b, self.troncons[['geometry']].reset_index(names='troncon'),
                              how='left', distance_col='d')
        self.troncons_barres = set(j.loc[j['d'] <= 1000, 'troncon'].dropna().astype(int))
        if verbose:
            print(f"  tronçons sous emprise de barrage : {len(self.troncons_barres)}")

    # ─────────────────────────────────────────
    # PARCOURS
    # ─────────────────────────────────────────
    def _station_noeud(self, code):
        r = self.stations[self.stations['code'] == str(code)]
        if r.empty or not r['sur_reseau'].iloc[0]:
            return None
        return r['noeud_aval'].iloc[0]

    def _parcours(self, code, sens='aval'):
        """{code_station: (distance_m, nb_barrages, franchissements)}"""
        depart = self._station_noeud(code)
        if depart is None or depart not in self.G:
            return {}

        G = self.G if sens == 'aval' else self.G.reverse(copy=False)
        dist     = {depart: 0.0}
        barr     = {depart: 0}
        franchis = {depart: frozenset()}

        for n in nx.bfs_tree(G, depart):
            for v in G.successors(n):
                d  = G.edges[n, v]
                nd = dist[n] + d['longueur']
                nb = barr[n] + (1 if (d['troncon'] in self.troncons_barres
                                      or d.get('franchit') == 'barrage') else 0)
                fr = franchis[n] | ({d['franchit']} if d.get('franchit') else set())
                if v not in dist or nd < dist[v]:
                    dist[v], barr[v], franchis[v] = nd, nb, frozenset(fr)

        res = {}
        for _, s in self.stations.iterrows():
            n = s['noeud_aval']
            if (s['code'] != str(code) and s['sur_reseau']
                    and n in dist and n != depart):
                res[s['code']] = (dist[n], barr[n], franchis[n])
        return res

    def aval_de(self, code):
        """Stations qui reçoivent l'eau de `code`."""
        return {k: (d, b) for k, (d, b, _) in self._parcours(code, 'aval').items()}

    def amont_de(self, code):
        """Stations qui envoient leur eau vers `code`."""
        return {k: (d, b) for k, (d, b, _) in self._parcours(code, 'amont').items()}

    def nom(self, code):
        r = self.stations[self.stations['code'] == str(code)]
        return r['nom'].iloc[0] if not r.empty else code

    # ─────────────────────────────────────────
    # EXPORT
    # ─────────────────────────────────────────
    def table_relations(self):
        """Toutes les relations amont→aval, avec obstacles traversés."""
        lignes = []
        for _, s in self.stations[self.stations['sur_reseau']].iterrows():
            for code_aval, (d, nb, fr) in self._parcours(s['code'], 'aval').items():
                lignes.append({
                    'station_amont':  s['code'],
                    'nom_amont':      s['nom'],
                    'station_aval':   code_aval,
                    'nom_aval':       self.nom(code_aval),
                    'distance_km':    round(d / 1000, 1),
                    'barrages_entre': nb,
                    'franchit':       ', '.join(sorted(fr)) if fr else 'direct',
                    # un trou de numérisation n'est PAS un obstacle physique
                    # (contrairement à un barrage) : neutre pour la fiabilité
                    'fiabilite':      'haute' if not (fr - {'trou_numerisation'}) else
                                      ('moyenne' if (fr - {'trou_numerisation'}) == {'plan_eau'}
                                       else 'à valider'),
                })
        df = pd.DataFrame(lignes)
        return (df.sort_values(['station_amont', 'distance_km']).reset_index(drop=True)
                if len(df) else df)


# ─────────────────────────────────────────────
# LIGNE DE COMMANDE
# ─────────────────────────────────────────────
if __name__ == '__main__':
    print("Construction du graphe hydrologique…\n")
    r = ReseauHydro().construire()
    dossier = Path(__file__).parent

    rel = r.table_relations()
    if rel.empty:
        print("\nAucune relation amont-aval trouvée.")
    else:
        rel.to_csv(dossier / 'reseau_stations.csv', sep=';',
                   index=False, encoding='utf-8-sig')
        print(f"\n{len(rel)} relation(s) amont→aval · "
              f"{rel['station_amont'].nunique()} station(s) amont")
        print("\nFiabilité :")
        print(rel['fiabilite'].value_counts().to_string())
        print("\n💾 reseau_stations.csv")
        print(rel.head(12).to_string(index=False))

    if len(r.arrets_non_resolus):
        r.arrets_non_resolus.to_csv(dossier / 'arrets_non_resolus.csv', sep=';',
                                    index=False, encoding='utf-8-sig')
        print(f"\n💾 arrets_non_resolus.csv — {len(r.arrets_non_resolus)} interruption(s) "
              "du tracé à signaler aux encadrants")
