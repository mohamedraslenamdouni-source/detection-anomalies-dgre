# Détection d'anomalies — réseau hydrologique DGRE

> Agent de détection et d'alerte sur les anomalies des données hydrologiques et
> pluviométriques d'un réseau de **102 stations automatiques** en Tunisie.

Projet réalisé lors d'un stage à la **Direction Générale des Ressources en Eau (DGRE)**,
Ministère de l'Agriculture, des Ressources Hydrauliques et de la Pêche — juillet à
septembre 2026.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?logo=streamlit&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-150458?logo=pandas&logoColor=white)

---

## Sommaire

1. [Contexte](#contexte)
2. [Le problème](#le-problème)
3. [Fonctionnalités](#fonctionnalités)
4. [Architecture](#architecture)
5. [Les 17 règles de détection](#les-17-règles-de-détection)
6. [Le graphe hydrologique](#le-graphe-hydrologique)
7. [Le tableau de bord](#le-tableau-de-bord)
8. [L'agent d'ingestion automatique](#lagent-dingestion-automatique)
9. [Validation et résultats](#validation-et-résultats)
10. [Choix techniques](#choix-techniques)
11. [Installation](#installation)
12. [Utilisation](#utilisation)
13. [Structure du projet](#structure-du-projet)
14. [Données](#données)
15. [Limites et perspectives](#limites-et-perspectives)
16. [Auteur](#auteur)

---

## Contexte

La DGRE exploite un réseau de 102 stations réparties sur 24 gouvernorats. Chaque
station mesure une ou plusieurs grandeurs : **pluie**, **cote** (hauteur d'eau),
**débit** et **tension de batterie**, avec une mesure toutes les **5 minutes**.

La donnée suit ce trajet :

```
Capteurs OTT  →  transmission GPRS  →  logiciel Hydras 3  →  export .MIS  →  cet agent
└──────────────── existant, en production ─────────────────┘                 └ développé ┘
```

| Type de station | Nombre | Grandeurs mesurées |
|---|---:|---|
| Pluviométrique | 46 | Pluie |
| Hydrométrique | 38 | Cote, débit |
| Hydro-pluviométrique | 18 | Pluie et cote |
| **Total** | **102** | |

## Le problème

Entre la mesure et l'archivage, **aucun contrôle automatique** n'était appliqué.
Les anomalies étaient repérées à la main, après coup. Or :

- **Le contrôle manuel ne suit pas le volume** : environ 84 mesures par station
  et par jour. En pratique, on ne vérifie que les stations déjà suspectes.
- **Certaines pannes sont invisibles sur un fichier isolé.** Une batterie qui
  perd 0,05 V par jour paraît normale chaque matin. Un pluviomètre bouché
  n'affiche pas d'erreur : il affiche **0 mm**, la valeur la plus fréquente et la
  plus légitime qui soit.
- **Une panne découverte tard est une perte définitive** : les données de toute
  la période sont perdues.

**Trois contraintes** ont guidé toute la conception :

1. **Lecture seule** : aucune écriture dans Hydras 3, qui est en production.
2. **Explicabilité** : chaque alerte doit pouvoir être justifiée devant un technicien.
3. **Aucune vérité terrain** : aucune liste d'anomalies confirmées n'existait,
   donc aucun modèle à entraîner.

## Fonctionnalités

- 📄 **Lecture des exports `.MIS`** d'Hydras 3, avec gestion des formats irréguliers
  et des fichiers tronqués.
- 🔍 **17 règles de détection explicables**, sur trois niveaux de portée.
- 💧 **Correction automatique de la pluie** hors plage ; la cote et le débit sont
  **seulement signalés**, jamais modifiés. La valeur d'origine est toujours conservée.
- 🗺️ **Graphe hydrologique amont-aval** construit à partir des couches SIG.
- 🖥️ **Tableau de bord web** en 6 pages, avec carte interactive et authentification.
- ✅ **Boucle de validation terrain** : l'agent propose, un technicien tranche.
- ⚙️ **Agent d'ingestion automatique**, prévu pour des milliers de fichiers par jour.

## Architecture

Cinq couches, chacune dans un fichier autonome, qui communiquent par une base
SQLite servant de mémoire commune.

```
Fichiers .MIS (dépôt manuel ou agent d'ingestion)
        │
        ▼
┌──────────────────────┐
│ mis_parser.py        │  lecture + règles instantanées R1–R9
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ historique.db        │  mémoire SQLite partagée
└──────────┬───────────┘
           ├──────────────► memoire.py                règles long terme L1–L7
           ├──────────────► reseau.py                 graphe amont-aval
           └──────────────► regles_inter_stations.py  règles I1–I2
           │
           ▼
┌──────────────────────┐
│ site.py              │  tableau de bord Streamlit
└──────────────────────┘
```

Le tableau de bord **ne calcule rien lui-même** : il lit la base et appelle les
modules. Le module d'agent ne dépend pas de l'interface, pour pouvoir être utilisé
à la fois par le site et par l'agent d'ingestion, qui tourne sans navigateur.

## Les 17 règles de détection

Les règles sont classées selon **ce qu'elles sont capables de voir**.

### Niveau 1 — Règles instantanées (un seul fichier)

| Règle | Anomalie | Principe |
|---|---|---|
| R1 | Valeur négative | Physiquement impossible (pluie de −3 mm) |
| R2 | Hors plage | Valeur hors des bornes propres à la station |
| R3 | Palier constant | Même valeur répétée sur plusieurs points |
| R4 | Tout à zéro | Ambigu : saison sèche ou capteur bouché ? |
| R5 | Horodatages dupliqués | La même date et heure apparaît deux fois |
| R6 | Pas de temps irrégulier | Écart différent du rythme normal |
| R7 | Trou de données | Écart supérieur à 1,5 fois le pas attendu |
| R8 | Correction de la pluie | Remplacement par la médiane des valeurs valides |
| R9 | Capteur muet ponctuel | Code d'état transmis à la place d'une valeur |
| — | Batterie | Décharge ≤ 11 V · point de non-retour ≤ 8 V · surcharge > 15 V |

### Niveau 2 — Règles long terme (tout l'historique)

| Règle | Anomalie | Méthode |
|---|---|---|
| L1 | Flatline longue | Valeur figée ≥ 72 h (cote, débit) ou 24 h (autres) |
| L2 | Dérive de batterie | Régression sur 7 jours, **date projetée du point de non-retour** |
| L2b | Absence de recharge | Plus de remontée de tension liée au panneau solaire |
| L3 | Station muette | Silence de 6 h **par rapport au reste du réseau** |
| L4 | Valeur inhabituelle | Écart supérieur à 6 fois le MAD de l'historique |
| L5 | Tendance continue | Régression sur 7 jours, R² ≥ 0,7 |
| L6 | Rupture de moyenne | Test de Pettitt sur les médianes journalières |
| L7 | Pluie sans réponse | Pluie significative sans montée de la cote |

### Niveau 3 — Règles inter-stations (les voisines)

| Règle | Anomalie | Méthode |
|---|---|---|
| I1 | Faux zéro pluviométrique | Station à 0 mm alors que ses voisines mesurent de la pluie (interpolation inverse-distance) |
| I2 | Incohérence de crue | Une crue vue en amont doit arriver en aval ; **abstention** si un barrage sépare les deux stations |
| I3 | Débit croissant vers l'aval | Conçue, non implémentée (pas de courbes de tarage) |

**Pourquoi des délais différents selon les capteurs ?** Parce que chaque grandeur a
sa propre dynamique. En étiage, la cote peut rester immobile **42 heures** de façon
naturelle : le seuil de 72 h a été calibré sur un an de données réelles. Une
batterie, elle, varie chaque jour avec le soleil : 24 h de valeur figée suffisent
à signaler une panne.

## Le graphe hydrologique

Comparer une station à ses voisines suppose de savoir **qui est réellement relié
par l'eau**. La distance ne suffit pas : deux stations à 5 km peuvent être
séparées par une ligne de crête, et deux stations à 80 km sur le même oued sont
connectées.

Le graphe est construit avec **GeoPandas** et **NetworkX** à partir des couches SIG :
6 101 tronçons de cours d'eau, 55 barrages, 230 plans d'eau.

- Le sens d'écoulement a été vérifié avec l'ordre de Strahler : **2 583 jonctions
  cohérentes contre 14 (99,5 %)**.
- Les retenues servent de connecteurs : relations amont-aval portées de **74 à 263**.
- Le réseau livré était fragmenté en **793 morceaux**. L'hypothèse d'une cause
  naturelle a été testée puis écartée : 787 morceaux appartiennent à un seul
  sous-bassin, qui est par définition continu. C'est donc un défaut de numérisation.
- **686 interruptions** recensées, 167 résolues automatiquement, et **65 points**
  prioritaires vérifiés un par un sur imagerie satellite : 14 trous confirmés,
  34 arrêts légitimes, 17 indéterminables.

## Le tableau de bord

Application **Streamlit** en six pages, organisées **par question posée** :

| Page | Question |
|---|---|
| Accueil | Où aller ? |
| Inventaire | Comment va le réseau dans son ensemble ? |
| Liste | Quelles stations, précisément, sont dans cet état ? |
| Fiche station | Que se passe-t-il ici, capteur par capteur ? |
| Vérifications | Quelles questions l'agent a posées, et qu'a-t-on répondu ? |
| Cohérence | Que contiennent les fichiers déposés ? |

**Carte interactive (Folium)** : la couleur porte l'état officiel de la station, un
anneau ambré signale une question en attente. Deux informations indépendantes,
deux signaux distincts.

**Boucle de validation** : l'agent ne réécrit jamais l'état déclaré. Il ouvre une
demande de vérification, et un technicien répond par l'une de trois réponses :

| Réponse | Effet sur l'état | Ce que ça dit de la règle |
|---|---|---|
| Panne confirmée | L'état change | Elle avait raison |
| Réparée entre-temps | Aucun changement | Elle avait raison |
| Fausse alerte | Aucun changement | Elle avait tort |

Chaque réponse étiquette une détection : le système construit ainsi, par l'usage,
la vérité terrain qui manquait au départ.

**Sécurité** : mot de passe haché avec PBKDF2-HMAC-SHA256 (200 000 itérations, sel
aléatoire), comparaison à temps constant, et aucune page déclarée côté serveur
avant l'identification.

## L'agent d'ingestion automatique

- **Tâche planifiée** plutôt que service permanent.
- **Journal par empreinte** : un redémarrage ne retélécharge pas tout le dépôt.
- **Quarantaine** : un fichier illisible est mis de côté sans interrompre le traitement.
- **Garde-fou de provenance** : refus d'ingérer un fichier réel pour une station
  marquée « simulée ».
- **Verrou** contre les exécutions simultanées.
- **Transport interchangeable** : dossier local ou serveur FTP/FTPS.
- **Aucune suppression** côté source.

## Validation et résultats

Chaque règle a été soumise à une **double épreuve** : aucune fausse alerte sur des
données saines, et détection effective d'une panne injectée volontairement dans
une copie de la base.

| Règle | Fausses alertes | Détections sur panne injectée |
|---|---|---|
| L6 — rupture de moyenne | 0 / 143 séries | 16 / 16 |
| L7 — pluie sans réponse | 0 / 18 stations | 16 et 21 |
| I1 — faux zéro | 1 / 64 stations (1 an) | 30 |
| I2 — incohérence amont-aval | 0 / 259 paires | 27 |

Ce protocole a aussi permis de **découvrir et corriger deux défauts de conception**
invisibles à la relecture du code.

**Campagne d'ingestion réelle :**

| | |
|---|---:|
| Fichiers traités | 10 880 |
| Fichiers ingérés | 10 872 |
| Doublons / vides / en quarantaine | 5 / 2 / 1 |
| Mesures ajoutées | 129 732 |
| Durée | 678 s |

**Passage à l'échelle** : les règles long terme lisent une fenêtre glissante de
120 jours au lieu de tout l'historique, ce qui divise le temps d'analyse par deux
(16 s → 8 s sur une base de 3 ans), avec des résultats identiques verdict par verdict.

## Choix techniques

| Choix | Pourquoi |
|---|---|
| Agent externe en lecture seule | Aucun risque pour le système en production |
| Règles statistiques plutôt que machine learning | Explicables, et aucune donnée étiquetée disponible |
| SQLite plutôt qu'un serveur | Un seul processus écrit, un seul lit, aucune administration |
| Streamlit | Interface et moteur dans le même langage : une seule technologie à maintenir |
| Graphe plutôt que distances | Seul le tracé réel des cours d'eau dit qui est relié par l'eau |
| Folium plutôt que Power BI | Carte interactive intégrée, sans licence |

## Installation

**Prérequis** : Python 3.11 ou plus récent.

```bash
git clone https://github.com/<utilisateur>/detection-anomalies-dgre.git
cd detection-anomalies-dgre
py -m pip install -r requirements.txt
```

> **Sous Windows**, utilisez toujours `py -m` pour installer **et** lancer. Si deux
> versions de Python sont installées, `pip` et `streamlit` peuvent sinon pointer
> vers deux installations différentes.

## Utilisation

**1. Créer le compte d'accès** (une seule fois) :

```bash
py creer_identifiants.py
```

Le mot de passe n'est jamais stocké : seuls l'identifiant, un sel aléatoire et
l'empreinte sont écrits dans un fichier local, qui n'est pas versionné.

**2. Lancer le tableau de bord :**

```bash
py -m streamlit run site.py
```

Le site s'ouvre dans le navigateur à l'adresse `http://localhost:8501`.

**Configurer les stations** : créez un fichier `config_stations.csv` au format de
`config_stations_exemple.csv` (séparateur `;`). Chaque ligne décrit une station :
code, nom, gouvernorat, type, coordonnées UTM 32N et bornes min/max par capteur.
Ce fichier s'édite directement dans Excel, sans toucher au code.

## Structure du projet

```
.
├── mis_parser.py                 Lecture des fichiers .MIS, règles instantanées R1–R9
├── memoire.py                    Mémoire SQLite, règles long terme L1–L7
├── reseau.py                     Graphe hydrologique amont-aval
├── regles_inter_stations.py      Règles inter-stations I1–I2
├── donnees_propres.py            Chargement des séries nettoyées
│
├── etats_agent.py                Agent d'état : détermine l'état de chaque station
├── propositions.py               Demandes de vérification (boucle de validation)
├── agent_ingestion.py            Agent d'ingestion automatique (dossier local ou FTP)
│
├── site.py                       Point d'entrée du tableau de bord
├── auth.py                       Authentification (PBKDF2)
├── creer_identifiants.py         Création du compte, à lancer une seule fois
├── theme.py                      Système de design (palette, typographie)
├── vues/
│   ├── accueil.py                Accueil
│   ├── inventaire.py             Carte et état du réseau
│   ├── liste.py                  Stations filtrées par état
│   ├── fiche.py                  Détail d'une station, capteur par capteur
│   ├── verifications.py          Boucle de validation terrain
│   └── coherence.py              Dépôt et analyse de fichiers .MIS
├── .streamlit/config.toml        Thème natif de Streamlit
│
├── app.py                        Version d'origine mono-page (secours, sans authentification)
├── preparer_etats.py             Nettoyage de la feuille de suivi du parc
├── carte_stations.py             Carte de contrôle autonome des stations
├── altitudes.py                  Enrichissement des altitudes (OpenTopoData)
│
├── donnees_synthetiques.py       Générateur de la base de test réel / simulé
├── preparer_demo.py              Préparation d'un scénario de démonstration
├── demo_memoire.py               Démonstration des règles long terme
├── verifier_base.py              Audit de la séparation réel / simulé
├── diagnostic_reel.py            Diagnostic des données réelles
├── diagnostic_silences.py        Diagnostic des stations silencieuses
├── observer_evenement.py         Visualisation d'un événement hydrologique
│
├── requirements.txt
└── config_stations_exemple.csv   Deux stations fictives
```

## Données

Les données de mesure, les bases SQLite et les fichiers décrivant les stations
réelles **ne sont pas inclus** : ce sont des données d'exploitation d'un organisme
public. Le code fonctionne avec vos propres exports `.MIS` et votre propre
fichier de configuration.

## Limites et perspectives

**Limites actuelles**

- Données réelles disponibles pour une vingtaine de stations seulement ;
- Seuils encore majoritairement par défaut ;
- Pas de courbes de tarage : la règle I3 reste non implémentée ;
- Transport FTP implémenté mais non éprouvé en conditions réelles.

**Perspectives**

- Recalibrer les seuils sur l'historique réel, désormais conservé sans purge ;
- Ajouter un canal de notification pour les anomalies critiques ;
- Une fois assez de réponses de validation collectées, envisager un modèle non
  supervisé (Isolation Forest) **en complément** des règles.

## Auteur

**Mohamed Raslen Amdouni** — élève-ingénieur en informatique,
École Nationale d'Ingénieurs de Tunis (ENIT).

Stage encadré par **Mme Hedia Foudhaili**, Direction Générale des Ressources en Eau.
