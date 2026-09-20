"""
agent_ingestion.py — Agent d'ingestion automatique des fichiers .MIS (DGRE)
===========================================================================
Reçoit automatiquement les fichiers .MIS et les fait entrer dans la mémoire
long terme, sans jamais rien supprimer côté serveur.

CONCEPTION (section 28 du rapport)
  • Tâche planifiée, pas service permanent — plus robuste sur des mois.
  • Journal des fichiers traités (nom, taille, empreinte, statut) — sans lui,
    tout redémarrage retéléchargerait l'intégralité du dépôt.
  • Quarantaine avant ingestion — un fichier n'entre en mémoire qu'après
    avoir été parsé avec succès.
  • Aucune suppression côté serveur (principe de lecture seule).
  • Détection des fichiers en cours d'écriture — taille contrôlée deux fois.
  • Sauvegarde de la base avant une campagne importante, mode WAL.

SOURCE INTERCHANGEABLE — le point clé
  Toute la logique (journal, quarantaine, ingestion) est indépendante du
  transport. Seuls « lister » et « télécharger » changent :
      SourceLocale  → un dossier du disque (mode démo, testable aujourd'hui)
      SourceFTP     → un serveur FTP ou FTPS (ftplib, bibliothèque standard)
  Le jour où les accès réels sont connus, on change les paramètres de
  connexion : rien d'autre ne bouge.

GARDE-FOU PROVENANCE (règle de séparation réel / simulé)
  Si la base cible contient une table `provenance_station`, l'agent REFUSE
  d'ingérer un fichier dont la station y est marquée « simulé ». Une station
  est soit entièrement réelle, soit entièrement simulée : un fichier .MIS
  contient des mesures réelles, il ne peut donc pas compléter une station
  simulée. La contrainte est appliquée par le code, pas par la vigilance.

UTILISATION
  # mode démo : le « serveur » est un dossier local
  py agent_ingestion.py --dossier-local "C:\\Users\\USER\\Desktop\\hydras"

  # répétition à blanc : montre ce qui serait fait, n'écrit rien
  py agent_ingestion.py --dossier-local "..." --simulation

  # ne traiter que les fichiers récents (le nom encode la date)
  py agent_ingestion.py --dossier-local "..." --depuis 2026-08-25

  # le jour où les accès FTP existent
  py agent_ingestion.py --ftp-hote 10.0.0.5 --ftp-utilisateur dgre \\
                        --ftp-dossier /exports --ftps
"""

import argparse
import ftplib
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DOSSIER = Path(__file__).parent

D_QUARANTAINE = DOSSIER / 'quarantaine'      # fichiers en cours de traitement
D_REJETS      = DOSSIER / 'quarantaine_rejets'  # fichiers illisibles, conservés
D_SAUVEGARDES = DOSSIER / 'sauvegardes'
F_JOURNAL     = DOSSIER / 'journal_ingestion.db'
F_VERROU      = DOSSIER / 'agent.verrou'     # empêche deux exécutions simultanées

VERROU_PERIME_H = 6.0      # au-delà, le verrou est considéré comme abandonné

DELAI_STABILITE_S = 3.0    # attente entre les deux mesures de taille
SEUIL_SAUVEGARDE  = 50     # au-delà de N fichiers à ingérer → sauvegarde d'abord

# <station>_<AAAAMMJJhhmmss>.MIS — convention observée sur les fichiers réels
MOTIF_NOM = re.compile(r'^(?P<station>\d+)_(?P<horodatage>\d{14})\.MIS$', re.I)


# ─────────────────────────────────────────────
# 1. SOURCES (transport interchangeable)
# ─────────────────────────────────────────────
@dataclass
class InfoFichier:
    chemin: str        # identifiant côté source (chemin local ou distant)
    nom: str           # nom de fichier seul
    taille: int


class Source:
    """Interface commune. Une source sait lister, mesurer et télécharger."""

    def lister(self) -> list[InfoFichier]:
        raise NotImplementedError

    def taille(self, chemin: str) -> int:
        raise NotImplementedError

    def telecharger(self, chemin: str, destination: Path) -> None:
        raise NotImplementedError

    def fermer(self) -> None:
        pass


class SourceLocale(Source):
    """Mode démo : un dossier du disque tient lieu de serveur.

    Permet de développer et de tester la totalité de l'agent avant d'avoir
    les accès FTP. Les sous-dossiers sont parcourus (un par gouvernorat).
    """

    def __init__(self, dossier):
        self.racine = Path(dossier)
        if not self.racine.is_dir():
            sys.exit(f"Dossier introuvable : {self.racine}")

    def lister(self):
        out = []
        for p in sorted(self.racine.rglob('*')):
            if p.is_file() and p.suffix.upper() == '.MIS':
                out.append(InfoFichier(str(p), p.name, p.stat().st_size))
        return out

    def taille(self, chemin):
        return Path(chemin).stat().st_size

    def telecharger(self, chemin, destination):
        shutil.copy2(chemin, destination)   # jamais de move : on ne touche pas la source


class SourceFTP(Source):
    """Serveur FTP ou FTPS. Aucune écriture, aucune suppression : lecture seule.

    Non testé faute d'accès au serveur DGRE au moment de l'écriture — la
    logique appelante est en revanche entièrement testée via SourceLocale.
    """

    def __init__(self, hote, utilisateur, mot_de_passe, dossier='/',
                 port=21, tls=False, timeout=30):
        self.dossier = dossier
        self.ftp = ftplib.FTP_TLS() if tls else ftplib.FTP()
        self.ftp.connect(hote, port, timeout=timeout)
        self.ftp.login(utilisateur, mot_de_passe)
        if tls:
            self.ftp.prot_p()               # chiffre aussi le canal de données
        self.ftp.set_pasv(True)

    def _parcourir(self, dossier, profondeur=0):
        """Descend récursivement, au plus 3 niveaux (serveur/gouvernorat/…)."""
        entrees = []
        try:
            self.ftp.cwd(dossier)
        except ftplib.error_perm:
            return entrees
        for nom, faits in self.ftp.mlsd():
            if nom in ('.', '..'):
                continue
            chemin = f"{dossier.rstrip('/')}/{nom}"
            if faits.get('type') == 'dir':
                if profondeur < 3:
                    entrees += self._parcourir(chemin, profondeur + 1)
            elif faits.get('type') == 'file' and nom.upper().endswith('.MIS'):
                entrees.append(InfoFichier(chemin, nom, int(faits.get('size', 0))))
        return entrees

    def lister(self):
        return sorted(self._parcourir(self.dossier), key=lambda f: f.chemin)

    def taille(self, chemin):
        try:
            return self.ftp.size(chemin) or 0
        except ftplib.all_errors:
            return 0

    def telecharger(self, chemin, destination):
        with open(destination, 'wb') as f:
            self.ftp.retrbinary(f"RETR {chemin}", f.write)

    def fermer(self):
        try:
            self.ftp.quit()
        except ftplib.all_errors:
            pass


# ─────────────────────────────────────────────
# 2. JOURNAL DES FICHIERS TRAITÉS
# ─────────────────────────────────────────────
def chemin_journal(chemin_db: Path) -> Path:
    """Un journal PAR base de mémoire.

    Le journal répond à « ce fichier est-il déjà entré dans CETTE base ». Un
    journal unique et partagé ferait sauter tous les fichiers dès qu'on change
    de base cible, alors que la nouvelle base, elle, est vide.
    """
    return DOSSIER / f"journal_{Path(chemin_db).stem}.db"


class Journal:
    """Mémoire de l'agent : quel fichier a déjà été vu, et avec quel résultat.

    L'empreinte SHA-256 sert de clé : un fichier réémis sous un autre nom,
    ou un même nom dont le contenu a changé, sont correctement distingués.
    """

    def __init__(self, chemin=F_JOURNAL, lecture_seule=False):
        self.absent = lecture_seule and not Path(chemin).exists()
        if self.absent:
            self.con = None          # rien à lire, rien à créer
            return
        self.con = sqlite3.connect(chemin)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS fichiers (
                empreinte  TEXT PRIMARY KEY,
                nom        TEXT,
                chemin     TEXT,
                taille     INTEGER,
                vu_le      TEXT,
                statut     TEXT,
                mesures    INTEGER,
                detail     TEXT
            )""")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_nom ON fichiers(nom, taille)")
        self.con.commit()

    def deja_traite(self, nom, taille):
        if self.con is None:
            return None
        """Pré-filtre AVANT téléchargement : même nom + même taille déjà vus.

        L'empreinte est plus sûre mais exige d'avoir le contenu, donc d'avoir
        déjà téléchargé. Ce pré-filtre évite précisément ce téléchargement.
        """
        r = self.con.execute(
            "SELECT statut FROM fichiers WHERE nom=? AND taille=?",
            (nom, taille)).fetchone()
        return r[0] if r else None

    def empreinte_connue(self, empreinte):
        if self.con is None:
            return False
        return self.con.execute(
            "SELECT statut FROM fichiers WHERE empreinte=?",
            (empreinte,)).fetchone() is not None

    def noter(self, empreinte, nom, chemin, taille, statut, mesures=0, detail=''):
        if self.con is None:
            return
        self.con.execute(
            "INSERT OR REPLACE INTO fichiers VALUES (?,?,?,?,?,?,?,?)",
            (empreinte, nom, chemin, taille,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             statut, mesures, detail))
        self.con.commit()

    def bilan(self):
        return dict(self.con.execute(
            "SELECT statut, COUNT(*) FROM fichiers GROUP BY statut").fetchall())

    def fermer(self):
        if self.con is not None:
            self.con.close()


# ─────────────────────────────────────────────
# 3. OUTILS
# ─────────────────────────────────────────────
def empreinte_fichier(chemin: Path) -> str:
    h = hashlib.sha256()
    with open(chemin, 'rb') as f:
        for bloc in iter(lambda: f.read(65536), b''):
            h.update(bloc)
    return h.hexdigest()


def date_du_nom(nom: str):
    """Extrait la date encodée dans le nom, ou None si le nom ne suit pas
    la convention. Permet de filtrer AVANT de télécharger."""
    m = MOTIF_NOM.match(nom)
    if not m:
        return None
    try:
        return datetime.strptime(m.group('horodatage'), '%Y%m%d%H%M%S')
    except ValueError:
        return None


def station_du_nom(nom: str):
    m = MOTIF_NOM.match(nom)
    return m.group('station') if m else None


def charger_provenances(con) -> dict:
    """{station: 'réel'|'simulé'} si la table existe, sinon {}.

    Une base purement réelle n'a pas cette table : dans ce cas aucun contrôle
    n'est nécessaire, tout ce qui arrive par .MIS est réel par construction.
    """
    try:
        return dict(con.execute(
            "SELECT station, origine FROM provenance_station").fetchall())
    except sqlite3.OperationalError:
        return {}


def sauvegarder_base(chemin_db: Path) -> Path:
    """Copie cohérente de la base via l'API de sauvegarde SQLite (jamais un
    simple copier-coller de fichier, qui peut capturer un état intermédiaire)."""
    D_SAUVEGARDES.mkdir(exist_ok=True)
    dest = D_SAUVEGARDES / f"{chemin_db.stem}_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(chemin_db)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


class Verrou:
    """Empêche deux exécutions simultanées de l'agent.

    Indispensable en tâche planifiée : si une campagne dure plus longtemps que
    l'intervalle de déclenchement, Windows lancerait une seconde instance qui
    écrirait dans la même base SQLite au même moment. Le verrou fait sortir la
    seconde proprement, sans erreur — elle reprendra au passage suivant.

    Un verrou plus vieux que VERROU_PERIME_H est ignoré : il provient d'une
    exécution interrompue (coupure de courant, machine redémarrée), et sans
    cette péremption l'agent resterait bloqué indéfiniment.
    """

    def __init__(self, chemin=F_VERROU):
        self.chemin = Path(chemin)
        self.pris = False

    def prendre(self) -> bool:
        if self.chemin.exists():
            age_h = (time.time() - self.chemin.stat().st_mtime) / 3600
            if age_h < VERROU_PERIME_H:
                contenu = self.chemin.read_text(encoding='utf-8', errors='replace')
                print(f"⏸ Une autre exécution est en cours depuis {age_h:.1f} h "
                      f"({contenu.strip()}) — ce passage est abandonné.")
                return False
            print(f"⚠ Verrou périmé ({age_h:.0f} h) — exécution précédente "
                  f"probablement interrompue, on reprend la main.")
        self.chemin.write_text(
            f"PID {os.getpid()} démarré le "
            f"{datetime.now():%Y-%m-%d %H:%M:%S}", encoding='utf-8')
        self.pris = True
        return True

    def rendre(self):
        if self.pris:
            self.chemin.unlink(missing_ok=True)
            self.pris = False


# ─────────────────────────────────────────────
# 4. L'AGENT
# ─────────────────────────────────────────────
def executer(source: Source, chemin_db: Path, depuis=None, stations=None,
             simulation=False, rejouer=False, limite=None, verbose=True):
    import memoire

    D_QUARANTAINE.mkdir(exist_ok=True)
    D_REJETS.mkdir(exist_ok=True)

    # En simulation, on n'ouvre RIEN en écriture : ouvrir une base SQLite la
    # crée si elle n'existe pas, ce qui contredirait « rien ne sera écrit ».
    # Le journal et la base existants sont lus s'ils sont là, jamais créés.
    journal = Journal(chemin_journal(chemin_db),
                      lecture_seule=simulation)
    if simulation and not Path(chemin_db).exists():
        con, provenances = None, {}
    else:
        con = memoire.connexion(chemin_db)
        if not simulation:
            con.execute("PRAGMA journal_mode=WAL")   # robustesse aux coupures
        provenances = charger_provenances(con)
    if provenances and verbose:
        n_sim = sum(1 for o in provenances.values() if o == 'simulé')
        print(f"  base mixte détectée : {n_sim} station(s) simulée(s) protégée(s) "
              f"contre l'ingestion de mesures réelles")

    # ── 1. lister ──
    tous = source.lister()
    if verbose:
        print(f"  {len(tous)} fichier(s) .MIS visible(s) sur la source")

    # ── 2. filtrer sur le NOM, avant tout téléchargement ──
    candidats, hors_filtre, deja = [], 0, 0
    for f in tous:
        if depuis:
            d = date_du_nom(f.nom)
            if d is not None and d < depuis:
                hors_filtre += 1
                continue
        if stations:
            s = station_du_nom(f.nom)
            if s is not None and s not in stations:
                hors_filtre += 1
                continue
        if not rejouer and journal.deja_traite(f.nom, f.taille):
            deja += 1
            continue
        candidats.append(f)

    if verbose:
        print(f"  {hors_filtre} écarté(s) par le filtre · {deja} déjà traité(s) "
              f"· {len(candidats)} à examiner")
    if limite:
        candidats = candidats[:limite]

    if not candidats:
        if verbose:
            print("\n✅ Rien de nouveau à ingérer.")
        journal.fermer()
        if con is not None:
            con.close()
        return {}

    # ── 3. sauvegarde avant une campagne importante ──
    if not simulation and len(candidats) >= SEUIL_SAUVEGARDE and chemin_db.exists():
        dest = sauvegarder_base(chemin_db)
        if verbose:
            print(f"  💾 sauvegarde préalable : {dest.name}")

    # ── 4. traiter fichier par fichier ──
    compte = {'ingéré': 0, 'doublon': 0, 'instable': 0, 'vide': 0,
              'illisible': 0, 'refusé_provenance': 0}
    n_mesures = 0

    for i, f in enumerate(candidats, 1):
        # 4a. fichier en cours d'écriture ? taille mesurée deux fois
        t1 = f.taille
        t2 = source.taille(f.chemin)

        if t1 != t2:
            # la taille bouge encore : le fichier est en cours d'écriture
            compte['instable'] += 1
            if verbose:
                print(f"  ⏳ {f.nom} — taille instable ({t1}→{t2}), "
                      f"repris au prochain passage")
            continue

        if t2 == 0:
            # Taille nulle ET stable : ce n'est PAS un transfert en cours,
            # c'est un fichier vide. Sans ce cas séparé, il serait réessayé
            # à chaque passage, indéfiniment, sans jamais être journalisé.
            compte['vide'] += 1
            if not simulation:
                journal.noter(f"vide_{f.nom}_{f.taille}", f.nom, f.chemin,
                              f.taille, 'vide', 0, 'fichier de 0 octet')
            if verbose:
                print(f"  ∅ {f.nom} — fichier vide (0 octet), écarté")
            continue

        if simulation:
            compte['ingéré'] += 1
            if verbose and i <= 10:
                print(f"  [simulation] {f.nom} serait ingéré")
            continue

        # 4b. quarantaine
        local = D_QUARANTAINE / f.nom
        try:
            source.telecharger(f.chemin, local)
        except Exception as e:
            compte['illisible'] += 1
            journal.noter('erreur_dl_' + f.nom, f.nom, f.chemin, f.taille,
                          'illisible', 0, f"téléchargement : {e}")
            continue

        emp = empreinte_fichier(local)
        if not rejouer and journal.empreinte_connue(emp):
            compte['doublon'] += 1
            local.unlink(missing_ok=True)
            journal.noter(emp, f.nom, f.chemin, f.taille, 'doublon')
            continue

        # 4c. parser AVANT d'ingérer — un fichier illisible n'entre jamais
        try:
            blocs = memoire.parse_mis(str(local))
        except Exception as e:
            compte['illisible'] += 1
            shutil.move(str(local), D_REJETS / f.nom)   # conservé pour analyse
            journal.noter(emp, f.nom, f.chemin, f.taille, 'illisible', 0,
                          f"{type(e).__name__}: {e}")
            if verbose:
                print(f"  ✗ {f.nom} — illisible ({type(e).__name__}), "
                      f"mis en quarantaine_rejets")
            continue

        if not blocs:
            compte['illisible'] += 1
            shutil.move(str(local), D_REJETS / f.nom)
            journal.noter(emp, f.nom, f.chemin, f.taille, 'illisible', 0,
                          'aucune donnée exploitable')
            continue

        # 4d. GARDE-FOU : ne jamais compléter une station simulée par du réel
        stations_fichier = {str(b['station'].iloc[0]) for b in blocs}
        simulees = [s for s in stations_fichier
                    if provenances.get(s) == 'simulé']
        if simulees:
            compte['refusé_provenance'] += 1
            local.unlink(missing_ok=True)
            journal.noter(emp, f.nom, f.chemin, f.taille, 'refusé_provenance', 0,
                          f"station(s) marquée(s) simulé : {', '.join(simulees)}")
            if verbose:
                print(f"  ⛔ {f.nom} — station {', '.join(simulees)} est "
                      f"« simulé » dans cette base : ingestion refusée")
            continue

        # 4e. ingestion par le code existant (mêmes règles que le dashboard)
        try:
            resultats, nouvelles = memoire.ingester_fichier(str(local), con)
        except Exception as e:
            compte['illisible'] += 1
            shutil.move(str(local), D_REJETS / f.nom)
            journal.noter(emp, f.nom, f.chemin, f.taille, 'illisible', 0,
                          f"ingestion : {type(e).__name__}: {e}")
            continue

        n_anomalies = sum(len(r['anomalies']) for r in resultats)
        compte['ingéré'] += 1
        n_mesures += nouvelles
        journal.noter(emp, f.nom, f.chemin, f.taille, 'ingéré', nouvelles,
                      f"{n_anomalies} anomalie(s) instantanée(s)")
        local.unlink(missing_ok=True)      # la quarantaine se vide au succès

        if verbose and (i <= 5 or i % 500 == 0):
            print(f"  ✓ [{i}/{len(candidats)}] {f.nom} → {nouvelles} mesure(s)")

    # ── 5. bilan ──
    if verbose:
        print("\n── Bilan de la campagne ──")
        for k, v in compte.items():
            if v:
                print(f"  {k:<20} {v}")
        print(f"  {n_mesures} nouvelle(s) mesure(s) en mémoire")
        if not simulation and con is not None:
            s = memoire.stats_memoire(con)
            print(f"  mémoire : {s['mesures']} mesures · {s['stations']} station(s) "
                  f"· du {s['debut']} au {s['fin']}")
        print("\n  (aucun fichier n'a été supprimé côté source)")

    journal.fermer()
    if con is not None:
        con.close()
    return compte


# ─────────────────────────────────────────────
# 5. LIGNE DE COMMANDE
# ─────────────────────────────────────────────
def main():
    # Windows encode stdout en cp1252 dès que la sortie est redirigée vers un
    # fichier (le cas de lancer_agent.bat, via >>) — cet encodage ne connaît
    # pas les émojis utilisés dans les messages, et Python plante au lieu de
    # les ignorer. On force UTF-8 explicitement, avec un filet de sécurité
    # (errors='replace') pour qu'un caractère imprévu ne fasse jamais échouer
    # une campagne d'ingestion en cours.
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass   # flux déjà UTF-8, ou ne supporte pas reconfigure : sans risque

    ap = argparse.ArgumentParser(
        description="Agent d'ingestion automatique des fichiers .MIS (DGRE).")
    ap.add_argument('--dossier-local',
                    help="MODE DÉMO : dossier tenant lieu de serveur.")
    ap.add_argument('--ftp-hote', help="adresse du serveur FTP")
    ap.add_argument('--ftp-port', type=int, default=21)
    ap.add_argument('--ftp-utilisateur', default='anonymous')
    ap.add_argument('--ftp-motdepasse', default='')
    ap.add_argument('--ftp-dossier', default='/')
    ap.add_argument('--ftps', action='store_true',
                    help="connexion chiffrée (FTPS explicite)")
    ap.add_argument('--base', type=Path, default=DOSSIER / 'historique.db',
                    help="base de mémoire à alimenter")
    ap.add_argument('--depuis', help="ne traiter que les fichiers datés d'après "
                                     "cette date (AAAA-MM-JJ), d'après leur nom")
    ap.add_argument('--stations', help="codes station à traiter, séparés par des "
                                       "virgules")
    ap.add_argument('--limite', type=int, help="s'arrêter après N fichiers")
    ap.add_argument('--simulation', action='store_true',
                    help="répétition à blanc : n'écrit ni base ni journal")
    ap.add_argument('--rejouer', action='store_true',
                    help="ignorer le journal et retraiter les fichiers")
    args = ap.parse_args()

    if not args.dossier_local and not args.ftp_hote:
        ap.error("précise --dossier-local (mode démo) ou --ftp-hote (serveur).")

    depuis = datetime.strptime(args.depuis, '%Y-%m-%d') if args.depuis else None
    stations = ({s.strip() for s in args.stations.split(',')}
                if args.stations else None)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] démarrage de l'agent")
    if args.dossier_local:
        print(f"Source : dossier local « {args.dossier_local} » (mode démo)")
        source = SourceLocale(args.dossier_local)
    else:
        print(f"Source : {'FTPS' if args.ftps else 'FTP'} {args.ftp_hote}"
              f"{args.ftp_dossier}")
        source = SourceFTP(args.ftp_hote, args.ftp_utilisateur,
                           args.ftp_motdepasse, args.ftp_dossier,
                           args.ftp_port, args.ftps)

    print(f"Base   : {args.base}")
    if args.simulation:
        print("⚠ MODE SIMULATION — rien ne sera écrit\n")
    else:
        print()

    # Le verrou ne sert qu'aux exécutions qui ÉCRIVENT : une simulation ne
    # touche à rien, elle peut tourner pendant une vraie campagne.
    verrou = Verrou()
    if not args.simulation and not verrou.prendre():
        source.fermer()
        return

    debut = datetime.now()
    try:
        executer(source, args.base, depuis, stations,
                 args.simulation, args.rejouer, args.limite)
    finally:
        source.fermer()
        verrou.rendre()
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] "
              f"fin — durée {(datetime.now() - debut).total_seconds():.0f} s")


if __name__ == '__main__':
    main()
