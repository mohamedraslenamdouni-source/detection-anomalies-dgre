"""
auth.py — Authentification du tableau de bord DGRE
===================================================
Lit les identifiants depuis .streamlit/secrets.toml, créé par
creer_identifiants.py.

Principe : le mot de passe n'est stocké nulle part. Seule son EMPREINTE l'est.
À la connexion, on recalcule l'empreinte de ce qui est tapé et on compare les
deux empreintes — le site ne connaît jamais le mot de passe lui-même.

La comparaison utilise hmac.compare_digest plutôt que « == » : cette fonction
met toujours le même temps à répondre, que les empreintes diffèrent au premier
ou au dernier caractère. Un « == » classique répond plus vite sur une
différence précoce, ce qui laisse deviner le contenu caractère par caractère.
"""

import hashlib
import hmac

import streamlit as st


def _empreinte(mot_de_passe: str, sel_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac('sha256', mot_de_passe.encode('utf-8'),
                               bytes.fromhex(sel_hex), iterations).hex()


def _config():
    """Récupère la section [auth] de secrets.toml, ou None si absente."""
    try:
        return st.secrets['auth']
    except Exception:
        return None


def est_connecte() -> bool:
    return bool(st.session_state.get('connecte'))


def utilisateur() -> str:
    return st.session_state.get('utilisateur', '')


def deconnecter():
    for cle in ('connecte', 'utilisateur'):
        st.session_state.pop(cle, None)


def _verifier(nom: str, mdp: str) -> bool:
    cfg = _config()
    if cfg is None:
        return False
    calcul = _empreinte(mdp, cfg['sel'], int(cfg.get('iterations', 200_000)))
    # Les deux comparaisons sont faites systématiquement (pas de court-circuit)
    # pour ne pas révéler, par le temps de réponse, si le nom seul était bon.
    nom_ok = hmac.compare_digest(nom.strip(), str(cfg['utilisateur']))
    mdp_ok = hmac.compare_digest(calcul, str(cfg['empreinte']))
    return nom_ok and mdp_ok


def page_connexion():
    """Affiche l'écran de connexion. Bloque tant que l'accès n'est pas accordé."""
    import theme

    st.markdown("""
    <style>
      section[data-testid="stSidebar"] { display: none; }
      header[data-testid="stHeader"] { background: transparent !important; }

      /* L'écran de connexion est la première image du site : il applique le
         même système que le reste (variables posées par theme.inject_theme). */
      .bloc-connexion {
          position: relative; overflow: hidden;
          margin: 4vh auto var(--esp-lg) auto;
          border: 1px solid var(--border); border-radius: var(--r-lg);
          background: linear-gradient(135deg, var(--panel), var(--panel2));
          box-shadow: var(--ombre); padding: var(--esp-xl) var(--esp-xl) 88px var(--esp-xl);
      }
      .bloc-connexion::before {
          content: ""; position: absolute; right: -70px; top: -90px;
          width: 260px; height: 260px; border-radius: 50%;
          background: radial-gradient(circle, var(--or), transparent 70%);
          opacity: 0.16;
      }
      .bloc-connexion .tag {
          font-family: var(--f-texte); font-size: var(--t-label);
          letter-spacing: 0.24em; color: var(--muted); text-transform: uppercase;
      }
      .bloc-connexion .titre {
          font-family: var(--f-titre); font-weight: 300;
          font-size: clamp(2.1rem, 4.4vw, 3rem); line-height: 1.05;
          color: var(--text); margin: var(--esp-sm) 0 0 0;
          position: relative; z-index: 1;
      }
      .bloc-connexion .titre .accent {
          color: var(--accent); font-weight: 600; font-style: italic;
      }
      .bloc-connexion .sous {
          font-family: var(--f-texte); font-size: var(--t-petit);
          color: var(--muted); margin-top: var(--esp-md); max-width: 44ch;
          position: relative; z-index: 1;
      }
      .bloc-connexion .filet {
          width: 46px; height: 1px; background: var(--or);
          margin: var(--esp-md) 0; opacity: 0.85;
      }
      /* Le même bandeau de collines que sur les autres pages */
      .bloc-connexion .hero-paysage { height: 90px; }
    </style>
    """, unsafe_allow_html=True)

    _, centre, _ = st.columns([1, 2.2, 1])
    with centre:
        st.markdown("""
        <div class="bloc-connexion">
          <div class="tag">DGRE · République Tunisienne</div>
          <div class="titre"><span class="accent">Hydras</span> Agent</div>
          <div class="filet"></div>
          <div class="sous">Surveillance du réseau hydro-pluviométrique national.
          Identification requise pour accéder au système.</div>
          <div class="hero-paysage"></div>
        </div>
        """, unsafe_allow_html=True)

        if _config() is None:
            st.error("Aucun identifiant enregistré.\n\n"
                     "Lance d'abord dans un terminal :\n\n"
                     "`py creer_identifiants.py`")
            st.stop()

        nom = st.text_input("Nom d'utilisateur", key="saisie_nom")
        mdp = st.text_input("Mot de passe", type="password", key="saisie_mdp")

        if st.button("SE CONNECTER", use_container_width=True):
            if _verifier(nom, mdp):
                st.session_state['connecte'] = True
                st.session_state['utilisateur'] = nom.strip()
                st.rerun()
            else:
                # Message volontairement vague : ne pas indiquer lequel des deux
                # champs est faux, cela aiderait une tentative d'intrusion.
                st.error("Identifiant ou mot de passe incorrect.")

    st.stop()


def barre_laterale():
    """Bloc utilisateur + déconnexion, affiché sur toutes les pages."""
    import theme
    with st.sidebar:
        st.caption(f"Connecté : **{utilisateur()}**")
        theme.selecteur_mode()
        if st.button("← Accueil", use_container_width=True):
            st.switch_page("vues/accueil.py")
        if st.button("Déconnexion", use_container_width=True):
            deconnecter()
            st.rerun()
