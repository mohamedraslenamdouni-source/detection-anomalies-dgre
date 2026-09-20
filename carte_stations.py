"""
carte_stations.py — Carte des 102 stations DGRE, colorée par état
==================================================================
Version AUTONOME : produit un fichier HTML à ouvrir dans le navigateur.
Sert à VÉRIFIER que les stations tombent au bon endroit avant d'intégrer
la carte au tableau de bord Streamlit.

Lire d'abord : etats_stations.csv (produit par preparer_etats.py)

Lancer :
    py -m pip install folium
    py carte_stations.py
    → ouvre carte_stations.html

Code couleur (identique à celui du tableau de bord) :
    🟢 vert  = en marche  (transmet, aucune panne déclarée)
    🔴 rouge = critique   (transmet, mais panne déclarée)
    🔵 bleu  = en arrêt   (ne transmet plus)
"""

import sys
from pathlib import Path

import pandas as pd

try:
    import folium
except ImportError:
    raise SystemExit("Dépendance manquante.\n  py -m pip install folium")

DOSSIER = Path(__file__).parent
SOURCE  = DOSSIER / 'etats_stations.csv'
SORTIE  = DOSSIER / 'carte_stations.html'

# Centre approximatif de la Tunisie et zoom d'ensemble
CENTRE, ZOOM = (34.0, 9.6), 7

# Couleurs alignées sur theme.py (thème « salle de contrôle »)
COULEURS = {
    'marche':   {'hex': '#00ff9d', 'libelle': 'En marche',  'icone': '🟢'},
    'critique': {'hex': '#ff4d5e', 'libelle': 'Critique',   'icone': '🔴'},
    'arret':    {'hex': '#38bdf8', 'libelle': 'En arrêt',   'icone': '🔵'},
}
COULEUR_INCONNUE = {'hex': '#5f7a74', 'libelle': 'État inconnu', 'icone': '⚪'}


def bulle(r) -> str:
    """Contenu de la fenêtre qui s'ouvre au clic sur une station."""
    c = COULEURS.get(r['etat_dgre'], COULEUR_INCONNUE)
    panne = r['type_panne'] if isinstance(r['type_panne'], str) and r['type_panne'] else '—'
    return f"""
    <div style="font-family:'JetBrains Mono',monospace;font-size:12px;
                min-width:230px;color:#0e1214;">
      <div style="font-weight:700;font-size:13px;margin-bottom:2px;">{r['nom']}</div>
      <div style="color:#5f7a74;margin-bottom:8px;">{r['code_station']}</div>
      <div style="background:{c['hex']}22;border-left:3px solid {c['hex']};
                  padding:4px 8px;margin-bottom:8px;font-weight:600;">
        {c['icone']} {c['libelle']}
      </div>
      <table style="border-collapse:collapse;">
        <tr><td style="color:#5f7a74;padding-right:10px;">Gouvernorat</td><td>{r['gouvernorat']}</td></tr>
        <tr><td style="color:#5f7a74;padding-right:10px;">Type</td><td>{r['type_station']}</td></tr>
        <tr><td style="color:#5f7a74;padding-right:10px;">Capteur</td><td>{r['capteur']}</td></tr>
        <tr><td style="color:#5f7a74;padding-right:10px;">Transmission</td><td>{r['mode_transmission']}</td></tr>
        <tr><td style="color:#5f7a74;padding-right:10px;">Panne</td><td>{panne}</td></tr>
      </table>
    </div>"""


def legende(compte: dict) -> str:
    """Encart fixe en bas à droite, avec le décompte par état."""
    lignes = "".join(
        f"""<div style="margin:5px 0;display:flex;align-items:center;">
              <span style="width:12px;height:12px;border-radius:50%;
                           background:{c['hex']};box-shadow:0 0 8px {c['hex']};
                           display:inline-block;margin-right:9px;"></span>
              <span style="flex:1;">{c['libelle']}</span>
              <b style="color:{c['hex']};margin-left:14px;">{compte.get(k, 0)}</b>
            </div>"""
        for k, c in COULEURS.items())

    return f"""
    <div style="position:fixed;bottom:26px;right:16px;z-index:9999;
                background:#0e1214;border:1px solid #1e2a2d;border-radius:10px;
                padding:14px 18px;color:#d6e2e0;
                font-family:'JetBrains Mono',monospace;font-size:12px;
                box-shadow:0 8px 24px rgba(0,0,0,0.5);min-width:190px;">
      <div style="color:#00ff9d;text-transform:uppercase;letter-spacing:0.08em;
                  font-size:10px;border-bottom:1px solid #1e2a2d;
                  padding-bottom:6px;margin-bottom:8px;">
        Réseau DGRE — {sum(compte.values())} stations
      </div>
      {lignes}
    </div>"""


def construire(source: Path = SOURCE) -> folium.Map:
    if not source.exists():
        sys.exit(f"Fichier introuvable : {source}\n"
                 "Lance d'abord :  py preparer_etats.py")

    df = pd.read_csv(source, sep=';', encoding='utf-8-sig',
                     dtype={'code_station': str})

    manquantes = df['lat'].isna() | df['lon'].isna()
    if manquantes.any():
        print(f"  ⚠ {manquantes.sum()} station(s) sans coordonnées — non affichée(s)")
        df = df[~manquantes]

    carte = folium.Map(location=CENTRE, zoom_start=ZOOM,
                       tiles='CartoDB dark_matter', control_scale=True)

    # Un calque par état : cases à cocher en haut à droite pour isoler un état
    calques = {}
    for etat, c in COULEURS.items():
        n = int((df['etat_dgre'] == etat).sum())
        calques[etat] = folium.FeatureGroup(name=f"{c['icone']} {c['libelle']} ({n})",
                                            show=True)

    for _, r in df.iterrows():
        c = COULEURS.get(r['etat_dgre'], COULEUR_INCONNUE)
        cible = calques.get(r['etat_dgre'])
        if cible is None:
            continue
        folium.CircleMarker(
            location=[r['lat'], r['lon']],
            radius=6, color=c['hex'], weight=2,
            fill=True, fill_color=c['hex'], fill_opacity=0.75,
            tooltip=f"{r['nom']} — {c['libelle']}",
            popup=folium.Popup(bulle(r), max_width=320),
        ).add_to(cible)

    for calque in calques.values():
        calque.add_to(carte)

    folium.LayerControl(collapsed=False).add_to(carte)
    compte = df['etat_dgre'].value_counts().to_dict()
    carte.get_root().html.add_child(folium.Element(legende(compte)))
    return carte


def main():
    carte = construire()
    carte.save(SORTIE)

    df = pd.read_csv(SOURCE, sep=';', encoding='utf-8-sig')
    print(f"\n💾 {SORTIE.name}\n")
    for etat, c in COULEURS.items():
        print(f"  {c['icone']} {c['libelle']:<12} {int((df['etat_dgre'] == etat).sum()):>3}")
    print(f"\n  Emprise : lat {df['lat'].min():.2f}–{df['lat'].max():.2f} · "
          f"lon {df['lon'].min():.2f}–{df['lon'].max():.2f}")
    print("\n  À VÉRIFIER en ouvrant le fichier : les stations hydrométriques")
    print("  doivent tomber SUR un cours d'eau, et aucune station en mer.")


if __name__ == '__main__':
    main()
