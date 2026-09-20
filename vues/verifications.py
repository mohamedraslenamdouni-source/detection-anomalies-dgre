"""
vues/verifications.py — Traiter les demandes de vérification de l'agent
========================================================================
Chaque demande présente ce que l'agent a constaté, capteur par capteur, avec
la règle qui a parlé. Trois réponses possibles après contrôle sur le terrain :

  ✅ Panne confirmée      la panne est réelle et toujours là → l'état change
  🔧 Réparée entre-temps  la panne était réelle, elle est corrigée → pas de
                          changement, mais la règle avait raison
  ❌ Fausse alerte        il n'y avait rien → pas de changement, la règle a
                          eu tort

Distinguer les deux dernières est essentiel. Toutes deux donnent « pas de
changement d'état », mais l'une dit que le système fonctionne et l'autre
qu'un seuil est à revoir. C'est cette distinction qui construira, réponse
après réponse, la vérité terrain dont le projet manque aujourd'hui.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import theme

try:
    import propositions
except ImportError:
    propositions = None

auth.barre_laterale()

BASE   = Path(__file__).parent.parent / 'historique.db'
SOURCE = Path(__file__).parent.parent / 'etats_stations.csv'

C = theme.couleurs()

ICONES = {'marche': '🟢', 'critique': '🔴', 'arret': '🔵'}
LIBELLES = {'marche': 'En marche', 'critique': 'Critique', 'arret': 'En arrêt'}

CHOIX = [
    ('confirme',     "✅ Panne confirmée",     "L'état de la station va changer"),
    ('repare',       "🔧 Réparée entre-temps", "Aucun changement — la règle avait vu juste"),
    ('faux_positif', "❌ Fausse alerte",       "Aucun changement — la règle a eu tort"),
]


@st.cache_data
def noms_stations() -> dict:
    if not SOURCE.exists():
        return {}
    df = pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig',
                     dtype={'code_station': str})
    return dict(zip(df['code_station'].str.strip(),
                    df['nom'] + ' (' + df['gouvernorat'].fillna('') + ')'))


def bloc_demande(con, p: dict, noms: dict):
    nom = noms.get(p['station'], '— hors liste —')
    depuis = p['cree_le'][:16] if p.get('cree_le') else '?'

    st.markdown(
        f"#### {ICONES.get(p['etat_actuel'], '⚪')} → {ICONES.get(p['etat_propose'], '⚪')} "
        f"&nbsp; {p['station']} — {nom}")
    st.caption(f"Demande ouverte le {depuis} · provenance : "
               f"{p.get('provenance') or 'inconnue'}"
               + (f" · règles : {p['regles']}" if p.get('regles') else ""))

    st.markdown(
        f"<div style='border-left:3px solid {C['ambre']};padding:8px 14px;"
        f"background:{C['ambre']}12;border-radius:0 6px 6px 0;'>"
        f"L'agent propose de passer la station de "
        f"<b>{LIBELLES.get(p['etat_actuel'], p['etat_actuel'])}</b> à "
        f"<b>{LIBELLES.get(p['etat_propose'], p['etat_propose'])}</b>.<br>"
        f"{p['motif']}</div>", unsafe_allow_html=True)

    if p.get('capteurs'):
        with st.expander("Détail capteur par capteur"):
            for code, motif in p['capteurs'].items():
                st.markdown(f"- `{code}` — {motif}")

    commentaire = st.text_input(
        "Observation (facultatif)", key=f"com_{p['id']}",
        placeholder="Ce qui a été constaté sur place…")

    colonnes = st.columns(3)
    for (valeur, libelle, effet), col in zip(CHOIX, colonnes):
        with col:
            if st.button(libelle, key=f"rep_{p['id']}_{valeur}",
                         use_container_width=True):
                res = propositions.repondre(con, p['id'], valeur,
                                            par=auth.utilisateur(),
                                            commentaire=commentaire or None)
                if res['etat_change']:
                    st.success(f"État mis à jour : {p['station']} → "
                               f"{LIBELLES.get(res['nouvel_etat'])}")
                else:
                    st.info("Réponse enregistrée — l'état de la station ne change pas.")
                # L'état affiché sur la carte dépend de cette réponse :
                # on force le recalcul au prochain passage sur l'Inventaire.
                st.session_state.pop('signature_agent', None)
                st.rerun()
            st.caption(effet)

    st.divider()


def main():
    theme.hero_banner("VÉRIFICATIONS", "EN ATTENTE",
                      "Changements d'état proposés par l'agent, à confirmer "
                      "après contrôle sur le terrain",
                      tag="DGRE // BOUCLE DE VALIDATION")

    if propositions is None or not BASE.exists():
        st.error("`propositions.py` ou `historique.db` introuvable.")
        return

    con = propositions.connexion(BASE)
    demandes = propositions.en_attente(con)
    noms = noms_stations()

    s = propositions.statistiques(con)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("En attente",      s['en_attente'])
    c2.metric("Confirmées",      s['confirmees'])
    c3.metric("Fausses alertes", s['faux_positifs'])
    c4.metric("Justesse",
              f"{s['taux_justesse']:.0f} %" if s['taux_justesse'] is not None else "—",
              help="Part des alertes qui se sont révélées fondées, une fois "
                   "vérifiées sur le terrain. Ce compteur mesure la qualité "
                   "réelle des règles à l'usage.")

    st.divider()

    if not demandes:
        st.success("✅ Aucune vérification en attente.")
        if st.button("← Retour à l'inventaire"):
            st.switch_page("vues/inventaire.py")
        return

    anciennete = propositions.anciennete_max(con)
    if anciennete >= 7:
        st.warning(f"⏳ La plus ancienne demande attend depuis "
                   f"{anciennete:.0f} jours.")

    for p in demandes:
        bloc_demande(con, p, noms)

    with st.expander("📜 Historique des demandes traitées"):
        traitees = [j for j in propositions.journal(con)
                    if j['statut'] != 'en_attente']
        if not traitees:
            st.caption("Aucune demande traitée pour l'instant.")
        else:
            st.dataframe(pd.DataFrame([{
                'Station':  j['station'],
                'Nom':      noms.get(j['station'], ''),
                'Proposé':  f"{j['etat_actuel']} → {j['etat_propose']}",
                'Réponse':  j['libelle_statut'],
                'Par':      j.get('reponse_par') or '',
                'Le':       (j.get('reponse_le') or '')[:16],
                'Note':     j.get('commentaire') or '',
            } for j in traitees]), use_container_width=True, hide_index=True)


main()
