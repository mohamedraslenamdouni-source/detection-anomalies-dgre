"""
etats_agent.py — État réel de chaque capteur, agrégé par station
=================================================================
BLOC 7b de l'agent. Ce module OBSERVE et CONCLUT ; il n'écrit rien et ne
propose rien. La création des propositions viendra au bloc 7c.

Python pur : aucune dépendance à Streamlit. Deux appelants l'utiliseront —
le site (bouton sur la page Inventaire) et l'agent FTP (automatique).

─────────────────────────────────────────────────────────────────────
PRINCIPE : « une station n'est jamais meilleure que son capteur le plus
dégradé »

L'état est constaté capteur par capteur, puis agrégé :

    tous les capteurs sains                → marche
    au moins un dégradé, station transmet  → critique
    plus aucun capteur ne transmet         → arret

C'est nécessaire parce que les pannes se produisent au niveau du CAPTEUR,
alors que l'état est déclaré au niveau de la STATION. Une station
hydro-pluviométrique peut très bien envoyer une pluie correcte et une cote
figée depuis trois jours : elle n'est ni « en marche » ni « en arrêt ».

─────────────────────────────────────────────────────────────────────
DEUX PRÉCAUTIONS DE MÉTHODE

1. La référence de temps est la dernière mesure reçue par le RÉSEAU, pas
   l'horloge de la machine. La base contient des données simulées dont la
   fin ne correspond pas à aujourd'hui : comparer à `maintenant` déclarerait
   les 102 stations muettes. C'est déjà le choix fait par la règle L3.

2. On ne relit pas l'historique entier. Pour savoir si un capteur est figé,
   seule la FIN de la série compte. On demande les agrégats à SQLite, puis
   on ne charge que la queue de chaque série.

Lancer :
    py etats_agent.py              rapport complet
    py etats_agent.py --anomalies  seulement les stations non conformes
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import propositions

DOSSIER = Path(__file__).parent
DB_PATH = DOSSIER / 'historique.db'
CSV_ETATS = DOSSIER / 'etats_stations.csv'

# Délais validés avec la DGRE avant qu'une observation compte comme un défaut
SEUILS = {
    'silence_capteur_h':      48,    # capteur sans données → muet
    'flatline_h':             72,    # valeur figée → capteur bloqué (L1)
    'batterie_alerte_v':      11.0,  # tension basse (règle batterie)
    'batterie_derive_v_jour': 0.05,  # pente de décharge (L2)
    'batterie_fenetre_j':     7,     # fenêtre d'analyse de la dérive
    'reprise_h':              24,    # transmission continue avant de proposer
}                                    #   qu'une station arrêtée est repartie

# ── PROFONDEUR DE LECTURE ────────────────────────────────────────────
# Elle n'est pas choisie : elle est DÉDUITE des seuils ci-dessus.
#
# Une fenêtre en temps plutôt qu'en nombre de points, d'abord, parce que les
# capteurs n'ont pas la même cadence : 3000 points couvrent ~10 jours à 5 min
# (pluie) mais ~125 jours à l'heure. Deux capteurs d'une même station seraient
# jugés sur des périodes incomparables.
#
# Dérivée plutôt que fixée, ensuite, pour deux raisons. Un nombre rond comme
# « 30 jours » ne se justifie pas ; « deux fois le plus long seuil » se
# défend. Et si un seuil change un jour, la fenêtre suit automatiquement —
# sans quoi une règle pourrait se retrouver privée de données et devenir
# silencieuse, sans erreur ni message.
#
# Le facteur 2 est une marge : pour mesurer une pente sur 7 jours, il faut un
# peu plus de 7 jours de données, sinon quelques points manquants suffisent à
# rendre le calcul impossible.
SEUILS['fenetre_lecture_j'] = 2 * max(
    SEUILS['flatline_h'] / 24,          # capteur figé      → 3 j
    SEUILS['batterie_fenetre_j'],       # dérive batterie   → 7 j
    SEUILS['silence_capteur_h'] / 24,   # capteur muet      → 2 j
)                                       # ⇒ 14 jours

# Cette fenêtre ne concerne QUE ce module. memoire.py continue de lire tout
# l'historique pour L4, L5 et L6 — cette dernière exige d'ailleurs au moins
# 60 jours de médianes journalières, et se tairait sous une limite plus basse.

CAPTEURS = {
    '0001': "Cote",
    '0002': "Batterie",
    '0006': "Pluie",
    '0007': "Débit",
}

# Capteurs dont un palier prolongé est anormal. Un zéro de pluie qui dure
# est normal (saison sèche) : la règle L1 ne s'y applique pas.
CAPTEURS_FLATLINE = ('0001', '0007')

ETAT_OK, ETAT_DEGRADE, ETAT_MUET = 'ok', 'degrade', 'muet'


# ─────────────────────────────────────────────
# 1. LECTURE DE LA BASE
# ─────────────────────────────────────────────

def connexion(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def signature_base(con) -> str:
    """Empreinte de l'état de la base : nombre de mesures + dernière date.

    Sert à décider s'il faut recalculer. Tant que cette signature ne change
    pas, aucune donnée nouvelle n'est arrivée et le résultat précédent reste
    valable — qu'il vienne d'un dépôt manuel ou de l'agent FTP.
    """
    r = con.execute("SELECT COUNT(*) n, MAX(timestamp) f FROM mesures").fetchone()
    return f"{r['n']}|{r['f']}"


def reference_reseau(con) -> pd.Timestamp:
    """Dernière mesure reçue par le réseau, toutes stations confondues."""
    r = con.execute("SELECT MAX(timestamp) f FROM mesures").fetchone()
    return pd.Timestamp(r['f']) if r and r['f'] else None


def resume_series(con) -> pd.DataFrame:
    """Une ligne par couple (station, capteur) : volume et dernière mesure.

    Une seule requête agrégée, quel que soit le volume de la base.
    """
    return pd.read_sql_query("""
        SELECT station, sensor,
               COUNT(*)       AS n_mesures,
               MAX(timestamp) AS derniere,
               MIN(timestamp) AS premiere
        FROM mesures
        GROUP BY station, sensor
        ORDER BY station, sensor""", con, parse_dates=['derniere', 'premiere'])


def queue_serie(con, station: str, sensor: str, derniere, jours: int = None):
    """Les `jours` derniers jours d'une série, dans l'ordre chronologique.

    Le point de départ est la DERNIÈRE MESURE de la série, pas la date du
    jour : une station muette depuis une semaine doit quand même livrer ses
    dernières données pour qu'on puisse constater dans quel état elle s'est
    arrêtée.
    """
    jours = jours or SEUILS['fenetre_lecture_j']
    depuis = (pd.Timestamp(derniere) - pd.Timedelta(days=jours)
              ).strftime('%Y-%m-%d %H:%M:%S')
    return pd.read_sql_query("""
        SELECT timestamp, value FROM mesures
        WHERE station=? AND sensor=? AND timestamp >= ?
        ORDER BY timestamp""",
        con, params=(station, sensor, depuis), parse_dates=['timestamp'])


def provenances(con) -> dict:
    """{station: 'reelle'|'simulee'} — la table peut ne pas exister."""
    try:
        lignes = con.execute(
            "SELECT station, provenance FROM provenance_station").fetchall()
        return {r['station']: r['provenance'] for r in lignes}
    except sqlite3.Error:
        return {}


# ─────────────────────────────────────────────
# 2. ÉTAT D'UN CAPTEUR
# ─────────────────────────────────────────────

def _duree_palier_final(df) -> tuple:
    """Durée (h) du palier de valeur constante à la FIN de la série.

    Retourne (duree_h, valeur, tronque). `tronque` vaut True quand le palier
    remonte jusqu'au début de la fenêtre chargée : sa durée réelle est alors
    plus longue que mesurée. On l'annonce (« depuis au moins N h ») plutôt
    que de donner un chiffre précis et faux.
    """
    if len(df) < 2:
        return 0.0, None, False
    v = df['value'].values
    i = len(v) - 1
    while i > 0 and v[i - 1] == v[i]:
        i -= 1
    duree = (df['timestamp'].iloc[-1] - df['timestamp'].iloc[i]).total_seconds() / 3600
    return duree, v[-1], i == 0


def _pente_batterie(df, fenetre_j: int) -> float:
    """Pente de la tension en V/jour sur la dernière fenêtre. None si trop court."""
    fin = df['timestamp'].max()
    fen = df[df['timestamp'] >= fin - pd.Timedelta(days=fenetre_j)]
    if len(fen) < 10:
        return None
    etendue_j = (fen['timestamp'].max() - fen['timestamp'].min()).total_seconds() / 86400
    if etendue_j < 2:
        return None
    t = (fen['timestamp'] - fen['timestamp'].min()).dt.total_seconds() / 86400
    return float(pd.Series(fen['value'].values).cov(pd.Series(t.values))
                 / pd.Series(t.values).var()) if t.var() else None


def etat_capteur(con, station: str, sensor: str, derniere, reference) -> dict:
    """Constate l'état d'UN capteur. Retourne état, motif et règle invoquée."""
    nom = CAPTEURS.get(sensor, f"Capteur {sensor}")
    retard_h = (reference - derniere).total_seconds() / 3600

    # ── Le capteur ne transmet plus ──
    if retard_h >= SEUILS['silence_capteur_h']:
        return {'capteur': sensor, 'nom': nom, 'etat': ETAT_MUET, 'regle': 'L3',
                'motif': (f"{nom} : aucune donnée depuis {retard_h:.0f} h "
                          f"(dernière le {derniere:%d/%m %H:%M})")}

    df = queue_serie(con, station, sensor, derniere)
    if df.empty:
        return {'capteur': sensor, 'nom': nom, 'etat': ETAT_OK, 'regle': None,
                'motif': f"{nom} : nominal"}

    # ── Batterie : tension basse ou décharge continue ──
    if sensor == '0002':
        v = float(df['value'].iloc[-1])
        if v <= SEUILS['batterie_alerte_v']:
            return {'capteur': sensor, 'nom': nom, 'etat': ETAT_DEGRADE,
                    'regle': 'batterie',
                    'motif': (f"{nom} : {v:.1f} V — sous le seuil d'alerte "
                              f"({SEUILS['batterie_alerte_v']} V)")}
        pente = _pente_batterie(df, SEUILS['batterie_fenetre_j'])
        if pente is not None and pente <= -SEUILS['batterie_derive_v_jour']:
            jours = (v - 8.0) / (-pente)
            return {'capteur': sensor, 'nom': nom, 'etat': ETAT_DEGRADE,
                    'regle': 'L2',
                    'motif': (f"{nom} : décharge de {pente:.2f} V/jour "
                              f"(actuellement {v:.1f} V) — point de non-retour "
                              f"dans ~{jours:.0f} jour(s)")}
        return {'capteur': sensor, 'nom': nom, 'etat': ETAT_OK, 'regle': None,
                'motif': f"{nom} : {v:.1f} V, nominale"}

    # ── Cote et débit : valeur figée ──
    if sensor in CAPTEURS_FLATLINE:
        duree, valeur, tronque = _duree_palier_final(df)
        fige = duree >= SEUILS['flatline_h'] and not (sensor == '0007' and valeur == 0)
        if fige:
            depuis = (f"depuis au moins {duree:.0f} h" if tronque
                      else f"depuis {duree:.0f} h")
            return {'capteur': sensor, 'nom': nom, 'etat': ETAT_DEGRADE, 'regle': 'L1',
                    'motif': (f"{nom} : valeur bloquée à {valeur} {depuis} "
                              f"— capteur probablement en panne")}

    return {'capteur': sensor, 'nom': nom, 'etat': ETAT_OK, 'regle': None,
            'motif': f"{nom} : nominal"}


# ─────────────────────────────────────────────
# 3. AGRÉGATION VERS LA STATION
# ─────────────────────────────────────────────

def agreger(capteurs: list[dict]) -> str:
    """« Une station n'est jamais meilleure que son capteur le plus dégradé »."""
    if not capteurs:
        return None
    etats = [c['etat'] for c in capteurs]
    if all(e == ETAT_MUET for e in etats):
        return 'arret'
    if any(e in (ETAT_DEGRADE, ETAT_MUET) for e in etats):
        return 'critique'
    return 'marche'


def analyser_reseau(con) -> list[dict]:
    """État détecté de chaque station présente dans la base.

    Les stations absentes de historique.db ne sont pas retournées : sans
    données, l'agent n'a rien à constater. Leur état DGRE reste seul valable.
    """
    reference = reference_reseau(con)
    if reference is None:
        return []

    resume = resume_series(con)
    prov = provenances(con)
    resultats = []

    for station, groupe in resume.groupby('station'):
        capteurs = [etat_capteur(con, station, r['sensor'], r['derniere'], reference)
                    for _, r in groupe.iterrows()]
        derniere = groupe['derniere'].max()
        resultats.append({
            'station':      station,
            'etat_detecte': agreger(capteurs),
            'capteurs':     capteurs,
            'derniere':     derniere,
            'retard_h':     (reference - derniere).total_seconds() / 3600,
            'n_mesures':    int(groupe['n_mesures'].sum()),
            'provenance':   prov.get(station, 'inconnue'),
        })

    return sorted(resultats, key=lambda r: r['station'])


# ─────────────────────────────────────────────
# 4. COMPARAISON AVEC L'ÉTAT DÉCLARÉ
# ─────────────────────────────────────────────

def comparer_au_declare(resultats: list[dict], csv_path: Path = CSV_ETATS):
    """Ajoute à chaque résultat l'état DGRE et le fait qu'il diverge.

    etats_stations.csv n'est jamais modifié : la comparaison est en mémoire.
    """
    if not csv_path.exists():
        for r in resultats:
            r['etat_dgre'], r['diverge'] = None, False
        return resultats

    ref = pd.read_csv(csv_path, sep=';', encoding='utf-8-sig',
                      dtype={'code_station': str})
    declare = dict(zip(ref['code_station'].str.strip(), ref['etat_dgre']))
    noms    = dict(zip(ref['code_station'].str.strip(), ref['nom']))

    for r in resultats:
        d = declare.get(r['station'])
        r['etat_dgre'] = d
        r['nom'] = noms.get(r['station'], '— hors liste —')
        r['diverge'] = (d is not None and r['etat_detecte'] != d)
    return resultats


def etat_reseau(con, csv_path: Path = CSV_ETATS) -> list[dict]:
    """Point d'entrée unique : constat + comparaison à l'état déclaré."""
    return comparer_au_declare(analyser_reseau(con), csv_path)


# ─────────────────────────────────────────────
# 5. RAPPORT CONSOLE
# ─────────────────────────────────────────────

ICONES = {'marche': '🟢', 'critique': '🔴', 'arret': '🔵'}
ICONE_CAPTEUR = {ETAT_OK: '🟢', ETAT_DEGRADE: '🟡', ETAT_MUET: '⚫'}


def rapport(seulement_anomalies: bool = False):
    con = connexion()
    debut = datetime.now()
    resultats = etat_reseau(con)
    duree = (datetime.now() - debut).total_seconds()

    if not resultats:
        print("\nAucune donnée dans historique.db — rien à analyser.")
        return

    ref = reference_reseau(con)
    print(f"\n{'=' * 66}")
    print("  ÉTAT DÉTECTÉ DU RÉSEAU  —  bloc 7b (observation seule)")
    print(f"{'=' * 66}")
    print(f"  Référence de temps : dernière mesure du réseau, {ref:%d/%m/%Y %H:%M}")
    print(f"  Stations analysées : {len(resultats)}   ({duree:.1f} s)\n")

    compte = {}
    for r in resultats:
        compte[r['etat_detecte']] = compte.get(r['etat_detecte'], 0) + 1
    for etat in ('marche', 'critique', 'arret'):
        print(f"    {ICONES[etat]} {etat:<9} {compte.get(etat, 0):>3}")

    divergentes = [r for r in resultats if r['diverge']]
    print(f"\n  Divergences avec l'état déclaré par la DGRE : {len(divergentes)}")

    a_montrer = divergentes if seulement_anomalies else resultats
    if seulement_anomalies and not divergentes:
        print("  ✅ Aucune divergence — le relevé DGRE correspond aux données.")
        return

    print(f"\n{'─' * 66}")
    for r in a_montrer:
        fleche = ""
        if r['diverge']:
            fleche = (f"   ⚠ DGRE dit {ICONES.get(r['etat_dgre'], '?')} "
                      f"{r['etat_dgre']} → détecté {ICONES[r['etat_detecte']]} "
                      f"{r['etat_detecte']}")
        print(f"\n{ICONES[r['etat_detecte']]} {r['station']} — {r.get('nom', '')}"
              f"  [{r['provenance']}]{fleche}")
        for c in r['capteurs']:
            print(f"     {ICONE_CAPTEUR[c['etat']]} {c['motif']}"
                  + (f"  [{c['regle']}]" if c['regle'] else ""))





# ─────────────────────────────────────────────
# 6. DES DIVERGENCES AUX PROPOSITIONS  (bloc 7c)
# ─────────────────────────────────────────────
# L'agent ne modifie JAMAIS l'état d'une station. Il ouvre une demande de
# vérification, qu'un humain tranchera après contrôle sur le terrain.
#
# Sur les délais : l'exigence de persistance est déjà portée par les seuils
# de détection (48 h de silence, 72 h de palier, 7 jours de dérive). Ajouter
# un second délai reviendrait à attendre deux fois. Un seul cas y échappe,
# la REPRISE : une station déclarée à l'arrêt qui renvoie un point isolé ne
# doit pas déclencher de demande — d'où la condition des 24 h ci-dessous.

# Du meilleur au pire. Sert à distinguer une dégradation d'une amélioration.
ORDRE_ETATS = {'marche': 0, 'critique': 1, 'arret': 2}


def duree_transmission_recente(con, station: str, reference,
                               capteurs_actifs: list = None) -> float:
    """Heures couvertes par les données des capteurs qui transmettent ENCORE.

    Répond à : « la station transmet-elle vraiment, ou n'est-ce qu'un point
    isolé ? »

    Ne compter que les capteurs actifs est indispensable. Sinon, une station
    dont deux capteurs se sont tus il y a cinq jours et dont un troisième
    vient d'émettre six heures de données afficherait une couverture de cinq
    jours — et passerait le contrôle des 24 h alors qu'elle vient à peine de
    redonner signe de vie.
    """
    depuis = (pd.Timestamp(reference) - pd.Timedelta(days=7)
              ).strftime('%Y-%m-%d %H:%M:%S')

    if capteurs_actifs is not None:
        if not capteurs_actifs:
            return 0.0
        trous = ','.join('?' * len(capteurs_actifs))
        r = con.execute(f"""SELECT MIN(timestamp) d, MAX(timestamp) f
                            FROM mesures WHERE station=? AND timestamp >= ?
                            AND sensor IN ({trous})""",
                        (station, depuis, *capteurs_actifs)).fetchone()
    else:
        r = con.execute("""SELECT MIN(timestamp) d, MAX(timestamp) f
                           FROM mesures WHERE station=? AND timestamp >= ?""",
                        (station, depuis)).fetchone()

    if not r or not r['d']:
        return 0.0
    return (pd.Timestamp(r['f']) - pd.Timestamp(r['d'])).total_seconds() / 3600


def _motif(r: dict) -> str:
    """Phrase lisible par un exploitant, nommant les capteurs en cause."""
    en_cause = [c for c in r['capteurs'] if c['etat'] != ETAT_OK]
    sains = [c['nom'] for c in r['capteurs'] if c['etat'] == ETAT_OK]

    if not en_cause:
        return (f"Tous les capteurs transmettent normalement "
                f"({', '.join(sains) or 'aucun détail'}) alors que la station "
                f"est déclarée « {r['etat_dgre']} »")

    texte = ' ; '.join(c['motif'] for c in en_cause)
    if sains:
        texte += f". En revanche {', '.join(sains)} " \
                 f"{'fonctionne' if len(sains) == 1 else 'fonctionnent'} normalement"
    return texte


def justifie_une_proposition(con, r: dict, reference) -> tuple:
    """(bool, raison_du_refus). Décide si une divergence mérite une demande."""
    if not r['diverge'] or r['etat_dgre'] is None:
        return False, "pas de divergence"

    amelioration = ORDRE_ETATS[r['etat_detecte']] < ORDRE_ETATS[r['etat_dgre']]
    if amelioration:
        actifs = [c['capteur'] for c in r['capteurs'] if c['etat'] != ETAT_MUET]
        duree = duree_transmission_recente(con, r['station'], reference, actifs)
        if duree < SEUILS['reprise_h']:
            return False, (f"reprise trop courte ({duree:.0f} h < "
                           f"{SEUILS['reprise_h']} h)")
    return True, None


def synchroniser(con, simulation: bool = False) -> dict:
    """Confronte l'état détecté à l'état déclaré et met à jour les demandes.

    Trois actions possibles par station :
      · ouvrir (ou enrichir) une demande quand une divergence est justifiée
      · clore une demande devenue sans objet — la divergence a disparu
      · ne rien faire

    En mode simulation, rien n'est écrit : l'agent dit seulement ce qu'il ferait.
    """
    propositions.creer_tables(con)
    reference = reference_reseau(con)
    resultats = etat_reseau(con)

    ouvertes, ignorees, closes = [], [], []

    for r in resultats:
        ok, raison = justifie_une_proposition(con, r, reference)

        if ok:
            regles = sorted({c['regle'] for c in r['capteurs'] if c['regle']})
            detail = {c['capteur']: c['motif'] for c in r['capteurs']}
            if not simulation:
                propositions.enregistrer_proposition(
                    con, r['station'], r['etat_dgre'], r['etat_detecte'],
                    _motif(r), capteurs=detail, regles=regles,
                    provenance=r['provenance'])
            ouvertes.append({**r, 'regles': regles})
        else:
            # La divergence a disparu : une demande encore ouverte n'a plus
            # d'objet. On la clôt en gardant sa trace — elle raconte un
            # incident réel, résolu avant qu'on ait eu à répondre.
            if propositions.proposition_en_attente(con, r['station']):
                if not simulation:
                    propositions.cloturer_caduque(
                        con, r['station'],
                        f"L'agent ne constate plus d'écart ({raison})")
                closes.append(r['station'])
            elif r['diverge']:
                ignorees.append({**r, 'raison': raison})

    return {'ouvertes': ouvertes, 'closes': closes, 'ignorees': ignorees,
            'simulation': simulation,
            'en_attente': len(propositions.en_attente(con))}


def rapport_propositions(simulation: bool = True):
    con = connexion()
    bilan = synchroniser(con, simulation=simulation)

    entete = "SIMULATION — rien n'est écrit" if simulation else "PROPOSITIONS ENREGISTRÉES"
    print(f"\n{'=' * 66}")
    print(f"  {entete}  —  bloc 7c")
    print(f"{'=' * 66}\n")

    if bilan['ouvertes']:
        print(f"  {len(bilan['ouvertes'])} demande(s) de vérification :\n")
        for r in bilan['ouvertes']:
            print(f"  {ICONES[r['etat_dgre']]} → {ICONES[r['etat_detecte']]}  "
                  f"{r['station']} — {r.get('nom', '')}  [{r['provenance']}]")
            print(f"     {r['etat_dgre']} → {r['etat_propose'] if 'etat_propose' in r else r['etat_detecte']}"
                  f"   règles : {', '.join(r['regles']) or '—'}")
            print(f"     {_motif(r)}\n")
    else:
        print("  Aucune demande de vérification à ouvrir.\n")

    if bilan['closes']:
        print(f"  {len(bilan['closes'])} demande(s) devenue(s) sans objet : "
              f"{', '.join(bilan['closes'])}\n")

    if bilan['ignorees']:
        print(f"  {len(bilan['ignorees'])} divergence(s) écartée(s) :")
        for r in bilan['ignorees']:
            print(f"     {r['station']} — {r['raison']}")
        print()

    print(f"  Total en attente de vérification : {bilan['en_attente']}")
    if simulation:
        print("\n  Pour enregistrer réellement :  py etats_agent.py --proposer")


if __name__ == '__main__':
    if '--proposer' in sys.argv:
        rapport_propositions(simulation=False)
    elif '--simulation' in sys.argv:
        rapport_propositions(simulation=True)
    else:
        rapport(seulement_anomalies='--anomalies' in sys.argv)
