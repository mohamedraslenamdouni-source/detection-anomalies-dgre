"""
creer_identifiants.py — Enregistre l'identifiant et le mot de passe du site
===========================================================================
À lancer UNE SEULE FOIS, avant la première utilisation du tableau de bord.

Pourquoi ce script existe :
  Un mot de passe écrit en clair dans le code est lisible par quiconque ouvre
  le fichier — et il finirait aussi dans le rapport de stage, dans une capture
  d'écran, ou sur une clé USB. Ce script n'enregistre donc JAMAIS le mot de
  passe lui-même : il en calcule une EMPREINTE (un hachage), impossible à
  inverser. À la connexion, le site recalcule l'empreinte de ce qui est tapé
  et compare les deux empreintes — sans jamais connaître le mot de passe.

  Le sel (« salt ») est une valeur aléatoire ajoutée avant le hachage : il
  garantit que deux personnes ayant le même mot de passe n'auront pas la même
  empreinte, ce qui rend les attaques par dictionnaire pré-calculé inutiles.

Lancer :
    py creer_identifiants.py

Crée le fichier .streamlit/secrets.toml, que Streamlit lit automatiquement.
⚠ Ne mets JAMAIS ce fichier sur GitHub ni dans une archive envoyée par mail.
"""

import getpass
import hashlib
import os
import sys
from pathlib import Path

DOSSIER = Path(__file__).parent / '.streamlit'
SECRETS = DOSSIER / 'secrets.toml'

ITERATIONS = 200_000        # coût de calcul : ralentit une attaque par force brute


def hacher(mot_de_passe: str, sel: bytes) -> str:
    """Transforme un mot de passe en empreinte hexadécimale irréversible."""
    empreinte = hashlib.pbkdf2_hmac('sha256', mot_de_passe.encode('utf-8'),
                                    sel, ITERATIONS)
    return empreinte.hex()


def main():
    print("\n" + "=" * 58)
    print("  CRÉATION DES IDENTIFIANTS — Tableau de bord DGRE")
    print("=" * 58 + "\n")

    if SECRETS.exists():
        print(f"⚠ {SECRETS} existe déjà.")
        if input("  L'écraser ? (o/N) : ").strip().lower() != 'o':
            sys.exit("  Annulé — rien n'a été modifié.")
        print()

    utilisateur = input("Nom d'utilisateur : ").strip()
    if not utilisateur:
        sys.exit("Nom d'utilisateur vide — abandon.")

    # getpass masque la saisie : rien ne s'affiche pendant que tu tapes.
    # C'est normal, continue de taper puis appuie sur Entrée.
    mdp = getpass.getpass("Mot de passe (rien ne s'affiche, c'est normal) : ")
    if len(mdp) < 6:
        sys.exit("Mot de passe trop court (6 caractères minimum) — abandon.")
    if getpass.getpass("Confirme le mot de passe : ") != mdp:
        sys.exit("Les deux saisies diffèrent — abandon.")

    sel = os.urandom(16)

    DOSSIER.mkdir(exist_ok=True)
    SECRETS.write_text(
        "# Identifiants du tableau de bord DGRE\n"
        "# Généré par creer_identifiants.py — NE PAS PARTAGER CE FICHIER.\n"
        "# Le mot de passe n'est pas stocké ici : seule son empreinte l'est.\n\n"
        "[auth]\n"
        f'utilisateur = "{utilisateur}"\n'
        f'sel = "{sel.hex()}"\n'
        f'empreinte = "{hacher(mdp, sel)}"\n'
        f'iterations = {ITERATIONS}\n',
        encoding='utf-8')

    print(f"\n✅ {SECRETS} créé.")
    print(f"   Utilisateur : {utilisateur}")
    print("   Mot de passe : enregistré sous forme d'empreinte uniquement.\n")
    print("   Pense à ajouter cette ligne à ton .gitignore :")
    print("       .streamlit/secrets.toml\n")


if __name__ == '__main__':
    main()
