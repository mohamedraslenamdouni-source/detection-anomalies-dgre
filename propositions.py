"""
propositions.py — Mémoire des changements d'état proposés et validés
=====================================================================
BLOC 7a de l'agent. Ce module ne contient AUCUNE règle de détection : il ne
fait que stocker, lire et répondre. Les règles viendront dans etats_agent.py.

Python pur : aucune dépendance à Streamlit. C'est indispensable, car deux
appelants très différents l'utiliseront —
  · le site (bouton « Lancer l'analyse » sur la page Inventaire)
  · l'agent FTP (script automatique, sans navigateur ouvert)

Deux tables, ajoutées à historique.db :

  propositions_etat   Les demandes de vérification. L'agent en crée une quand
                      il détecte une divergence persistante entre l'état
                      déclaré par la DGRE et ce qu'il observe. Une proposition
                      attend une réponse humaine ; elle ne change rien seule.

  historique_etats    Les changements RÉELLEMENT validés. Seule table qui
                      fait autorité sur l'état affiché. etats_stations.csv
                      reste la référence d'origine, jamais réécrite.

Les trois réponses possibles à une proposition :
  confirme       la panne est réelle et toujours là  → l'état change
  repare         la panne était réelle, elle est corrigée → pas de changement
  faux_positif   il n'y avait rien → pas de changement, la règle a eu tort

« repare » et « faux_positif » donnent tous deux « pas de changement », mais
les distinguer est essentiel : le premier dit que la règle a bien travaillé,
le second qu'un seuil est à revoir. C'est ce qui, à l'usage, constituera la
vérité terrain dont le projet manque aujourd'hui.

Lancer pour créer les tables et voir l'état des lieux :
    py propositions.py
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / 'historique.db'

ETATS_VALIDES = ('marche', 'critique', 'arret')

REPONSES = {
    'confirme':     "Panne confirmée sur le terrain",
    'repare':       "Réparée entre-temps",
    'faux_positif': "Fausse alerte",
}
# Seule une confirmation modifie l'état de la station.
REPONSES_QUI_CHANGENT_ETAT = ('confirme',)

STATUTS = ('en_attente',) + tuple(REPONSES) + ('caduque',)


def _maintenant() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ─────────────────────────────────────────────
# 1. LES TABLES
# ─────────────────────────────────────────────

def connexion(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Ouvre historique.db et s'assure que les deux tables existent."""
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    creer_tables(con)
    return con


def creer_tables(con: sqlite3.Connection):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS propositions_etat (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        station       TEXT    NOT NULL,
        etat_actuel   TEXT    NOT NULL,   -- état affiché au moment de la demande
        etat_propose  TEXT    NOT NULL,
        motif         TEXT    NOT NULL,   -- phrase lisible par un exploitant
        capteurs      TEXT,               -- JSON : détail capteur par capteur
        regles        TEXT,               -- ex. « L1, L3 »
        provenance    TEXT,               -- 'reelle' ou 'simulee'
        cree_le       TEXT    NOT NULL,
        maj_le        TEXT    NOT NULL,
        statut        TEXT    NOT NULL DEFAULT 'en_attente',
        reponse_le    TEXT,
        reponse_par   TEXT,
        commentaire   TEXT
    );

    -- Une seule proposition EN ATTENTE par station, imposée par la base
    -- elle-même : le code ne peut pas l'oublier. Un capteur instable ne
    -- produira donc jamais cinquante demandes pour la même panne.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_une_attente_par_station
        ON propositions_etat(station) WHERE statut = 'en_attente';

    CREATE TABLE IF NOT EXISTS historique_etats (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        station        TEXT    NOT NULL,
        etat_avant     TEXT,
        etat_apres     TEXT    NOT NULL,
        origine        TEXT    NOT NULL,  -- 'validation' (réponse humaine)
        proposition_id INTEGER,
        motif          TEXT,
        valide_par     TEXT,
        date           TEXT    NOT NULL,
        FOREIGN KEY (proposition_id) REFERENCES propositions_etat(id)
    );

    CREATE INDEX IF NOT EXISTS idx_hist_station
        ON historique_etats(station, date);
    """)
    con.commit()


# ─────────────────────────────────────────────
# 2. ÉCRITURE DES PROPOSITIONS
# ─────────────────────────────────────────────

def proposition_en_attente(con, station: str):
    """La proposition non traitée d'une station, ou None."""
    return con.execute(
        "SELECT * FROM propositions_etat WHERE station=? AND statut='en_attente'",
        (str(station),)).fetchone()


def enregistrer_proposition(con, station: str, etat_actuel: str, etat_propose: str,
                            motif: str, capteurs: dict = None, regles=None,
                            provenance: str = None) -> int:
    """Crée une proposition, ou ENRICHIT celle qui attend déjà.

    Enrichir plutôt que dupliquer : si l'agent détecte un second problème sur
    une station dont la vérification est déjà demandée, on met à jour la
    demande existante au lieu d'en ouvrir une deuxième.
    """
    if etat_propose not in ETATS_VALIDES:
        raise ValueError(f"État inconnu : {etat_propose}")

    capteurs_json = json.dumps(capteurs or {}, ensure_ascii=False)
    regles_txt = ', '.join(regles) if isinstance(regles, (list, tuple, set)) else (regles or '')
    maintenant = _maintenant()

    existante = proposition_en_attente(con, station)
    if existante:
        con.execute("""UPDATE propositions_etat
                       SET etat_propose=?, motif=?, capteurs=?, regles=?,
                           provenance=COALESCE(?, provenance), maj_le=?
                       WHERE id=?""",
                    (etat_propose, motif, capteurs_json, regles_txt,
                     provenance, maintenant, existante['id']))
        con.commit()
        return int(existante['id'])

    cur = con.execute("""INSERT INTO propositions_etat
        (station, etat_actuel, etat_propose, motif, capteurs, regles,
         provenance, cree_le, maj_le, statut)
        VALUES (?,?,?,?,?,?,?,?,?, 'en_attente')""",
        (str(station), etat_actuel, etat_propose, motif, capteurs_json,
         regles_txt, provenance, maintenant, maintenant))
    con.commit()
    return int(cur.lastrowid)


def cloturer_caduque(con, station: str, raison: str = "Le problème a disparu"):
    """Ferme une proposition devenue sans objet.

    Cas typique : l'agent propose « en arrêt », puis la station se remet à
    transmettre avant qu'on ait répondu. La demande n'a plus lieu d'être,
    mais on garde sa trace — elle raconte un incident réel et résolu.
    """
    p = proposition_en_attente(con, station)
    if p is None:
        return False
    con.execute("""UPDATE propositions_etat
                   SET statut='caduque', reponse_le=?, commentaire=?, maj_le=?
                   WHERE id=?""",
                (_maintenant(), raison, _maintenant(), p['id']))
    con.commit()
    return True


# ─────────────────────────────────────────────
# 3. RÉPONSE HUMAINE
# ─────────────────────────────────────────────

def repondre(con, proposition_id: int, reponse: str,
             par: str = None, commentaire: str = None) -> dict:
    """Enregistre la réponse d'un utilisateur après vérification terrain.

    Retourne {'etat_change': bool, 'station':…, 'nouvel_etat':…}.
    Seule la réponse « confirme » modifie l'état de la station.
    """
    if reponse not in REPONSES:
        raise ValueError(f"Réponse inconnue : {reponse} "
                         f"(attendu : {', '.join(REPONSES)})")

    p = con.execute("SELECT * FROM propositions_etat WHERE id=?",
                    (proposition_id,)).fetchone()
    if p is None:
        raise ValueError(f"Proposition {proposition_id} introuvable")
    if p['statut'] != 'en_attente':
        raise ValueError(f"Proposition {proposition_id} déjà traitée "
                         f"({p['statut']})")

    maintenant = _maintenant()
    con.execute("""UPDATE propositions_etat
                   SET statut=?, reponse_le=?, reponse_par=?, commentaire=?, maj_le=?
                   WHERE id=?""",
                (reponse, maintenant, par, commentaire, maintenant, proposition_id))

    change = reponse in REPONSES_QUI_CHANGENT_ETAT
    if change:
        con.execute("""INSERT INTO historique_etats
            (station, etat_avant, etat_apres, origine, proposition_id,
             motif, valide_par, date)
            VALUES (?,?,?, 'validation', ?,?,?,?)""",
            (p['station'], p['etat_actuel'], p['etat_propose'],
             proposition_id, p['motif'], par, maintenant))

    con.commit()
    return {'etat_change': change,
            'station': p['station'],
            'nouvel_etat': p['etat_propose'] if change else p['etat_actuel']}


# ─────────────────────────────────────────────
# 4. LECTURE
# ─────────────────────────────────────────────

def en_attente(con) -> list[dict]:
    """Toutes les vérifications en attente, la plus ancienne d'abord."""
    lignes = con.execute("""SELECT * FROM propositions_etat
                            WHERE statut='en_attente' ORDER BY cree_le""").fetchall()
    return [_en_dict(r) for r in lignes]


def journal(con, station: str = None, limite: int = 200) -> list[dict]:
    """Historique des propositions, toutes réponses confondues."""
    if station:
        lignes = con.execute("""SELECT * FROM propositions_etat WHERE station=?
                                ORDER BY cree_le DESC LIMIT ?""",
                             (str(station), limite)).fetchall()
    else:
        lignes = con.execute("""SELECT * FROM propositions_etat
                                ORDER BY cree_le DESC LIMIT ?""", (limite,)).fetchall()
    return [_en_dict(r) for r in lignes]


def _en_dict(row) -> dict:
    d = dict(row)
    try:
        d['capteurs'] = json.loads(d.get('capteurs') or '{}')
    except json.JSONDecodeError:
        d['capteurs'] = {}
    d['libelle_statut'] = REPONSES.get(d['statut'], {
        'en_attente': "En attente de vérification",
        'caduque':    "Sans objet (problème disparu)",
    }.get(d['statut'], d['statut']))
    return d


def etats_valides(con) -> dict:
    """{station: état} — uniquement les stations dont l'état a été changé
    par une validation humaine. Les autres gardent leur état DGRE."""
    lignes = con.execute("""
        SELECT station, etat_apres FROM historique_etats h
        WHERE date = (SELECT MAX(date) FROM historique_etats
                      WHERE station = h.station)""").fetchall()
    return {r['station']: r['etat_apres'] for r in lignes}


def appliquer_aux_stations(con, df):
    """Ajoute à un DataFrame de stations les colonnes d'affichage :

        etat            l'état à afficher = etat_dgre, remplacé par la
                        dernière validation humaine s'il en existe une
        etat_valide     True si l'état vient d'une validation
        proposition_en_attente  True → halo ambré sur la carte

    etats_stations.csv n'est jamais modifié : la fusion se fait en mémoire.
    """
    df = df.copy()
    valides = etats_valides(con)
    attentes = {p['station'] for p in en_attente(con)}

    df['etat_valide'] = df['code_station'].isin(valides)
    df['etat'] = [valides.get(c, e)
                  for c, e in zip(df['code_station'], df['etat_dgre'])]
    df['proposition_en_attente'] = df['code_station'].isin(attentes)
    return df


def statistiques(con) -> dict:
    """Compteurs pour le tableau de bord — et, à terme, mesure de la
    performance des règles : combien d'alertes se sont révélées justes."""
    compte = {r['statut']: r['n'] for r in con.execute(
        "SELECT statut, COUNT(*) n FROM propositions_etat GROUP BY statut")}
    traitees = sum(compte.get(k, 0) for k in REPONSES)
    justes   = compte.get('confirme', 0) + compte.get('repare', 0)
    return {
        'en_attente':   compte.get('en_attente', 0),
        'confirmees':   compte.get('confirme', 0),
        'reparees':     compte.get('repare', 0),
        'faux_positifs': compte.get('faux_positif', 0),
        'caduques':     compte.get('caduque', 0),
        'traitees':     traitees,
        'taux_justesse': (100 * justes / traitees) if traitees else None,
        'changements':  con.execute(
            "SELECT COUNT(*) n FROM historique_etats").fetchone()['n'],
    }


def anciennete_max(con) -> float:
    """Nombre de jours de la plus vieille demande non traitée (0 si aucune)."""
    r = con.execute("""SELECT MIN(cree_le) m FROM propositions_etat
                       WHERE statut='en_attente'""").fetchone()
    if not r or not r['m']:
        return 0.0
    return (datetime.now() - datetime.strptime(r['m'], '%Y-%m-%d %H:%M:%S')
            ).total_seconds() / 86400


# ─────────────────────────────────────────────
# 5. LIGNE DE COMMANDE
# ─────────────────────────────────────────────

if __name__ == '__main__':
    con = connexion()
    print(f"\n✅ Tables prêtes dans {DB_PATH.name}\n")

    for table in ('propositions_etat', 'historique_etats'):
        cols = [r['name'] for r in con.execute(f"PRAGMA table_info({table})")]
        n = con.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()['n']
        print(f"  {table:<20} {n:>4} ligne(s)")
        print(f"     colonnes : {', '.join(cols)}\n")

    s = statistiques(con)
    print("  État des propositions :")
    print(f"     en attente      {s['en_attente']}")
    print(f"     confirmées      {s['confirmees']}")
    print(f"     réparées        {s['reparees']}")
    print(f"     fausses alertes {s['faux_positifs']}")
    print(f"     sans objet      {s['caduques']}")
    if s['taux_justesse'] is not None:
        print(f"\n  Justesse des alertes : {s['taux_justesse']:.0f} % "
              f"({s['traitees']} vérifiée(s) sur le terrain)")
    else:
        print("\n  Aucune vérification terrain enregistrée pour l'instant —")
        print("  ce compteur mesurera la justesse des règles à l'usage.")

    if s['en_attente']:
        print(f"\n  ⏳ Plus ancienne demande : {anciennete_max(con):.1f} jour(s)")
