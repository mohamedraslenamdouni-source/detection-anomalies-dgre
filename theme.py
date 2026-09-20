"""
theme.py — Système de design du tableau de bord DGRE
=====================================================
Inspiré d'un modèle Canva « aquarelle vintage » : papier crème, verts de
végétation, or chaud, typographie serif fine. Les couleurs ci-dessous ont été
échantillonnées dans l'illustration du modèle, pas choisies à l'œil.

Deux modes : « clair » (par défaut) et « sombre », déclinaison nocturne de la
même palette plutôt qu'un thème étranger.

Principe : rien n'est écrit en dur ailleurs. Toutes les couleurs, espacements,
rayons, ombres et tailles de texte sont déclarés ici, puis injectés comme
VARIABLES CSS que les pages consomment. Changer d'identité visuelle ne demande
donc de toucher qu'à ce fichier.

Usage :
    import theme
    theme.inject_theme()      # dans site.py, après set_page_config
    theme.selecteur_mode()    # dans la barre latérale
    C = theme.couleurs()      # côté Python (carte Folium, graphiques Altair)
"""

import json
from pathlib import Path

import streamlit as st

CLE_MODE = 'mode_affichage'
MODE_DEFAUT = 'clair'

# Le choix clair/sombre est écrit sur le disque : st.session_state disparaît à
# la fermeture de l'onglet, et le site rouvrirait toujours dans le même mode.
PREFERENCES = Path(__file__).parent / '.streamlit' / 'preferences.json'


# ─────────────────────────────────────────────
# PALETTES
# ─────────────────────────────────────────────
PALETTES = {
    'clair': {
        # Surfaces — papier crème, cartes ivoire
        'bg':       '#F2EDE3',
        'panel':    '#FDFBF6',
        'panel2':   '#F6F1E7',
        'border':   '#DFD6C4',

        # Accents — verts de l'illustration
        'accent':   '#254940',   # vert pin profond
        'accent2':  '#4A6046',   # vert sauge

        # Texte
        'text':     '#2E2C28',
        'muted':    '#7C7566',

        # Or du bandeau bas
        'or':       '#A8842A',
        'or_clair': '#C9992E',

        # États — teintes du modèle, conventions préservées
        'marche':   '#4A6046',
        'critique': '#A6453A',
        'arret':    '#5B7089',
        'ambre':    '#C9992E',

        'surlignage_fond':  '#F6E3DE',
        'surlignage_texte': '#7A2E25',

        'tuiles': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/'
                   'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}'),
        'scanlines': False,
    },
    'sombre': {
        'bg':       '#14201C',   # vert pin très sombre
        'panel':    '#1C2B26',
        'panel2':   '#22332D',
        'border':   '#31463E',

        'accent':   '#8FB89B',
        'accent2':  '#6E9C7E',

        'text':     '#E8E3D8',
        'muted':    '#8D9A90',

        'or':       '#D9B45C',
        'or_clair': '#E7CB64',

        'marche':   '#7FB08A',
        'critique': '#D97A6C',
        'arret':    '#8BA3C0',
        'ambre':    '#E7CB64',

        'surlignage_fond':  '#432723',
        'surlignage_texte': '#F0BDB3',

        'tuiles': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/'
                   'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'),
        'scanlines': False,
    },
}

ATTRIBUTION_TUILES = 'Esri, HERE, Garmin, © OpenStreetMap contributors'
TUILES_SECOURS = ('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                  '© OpenStreetMap contributors')

# ─────────────────────────────────────────────
# ÉCHELLES — un seul jeu de valeurs pour tout le site
# ─────────────────────────────────────────────
# Espacements en progression régulière : deux blocs séparés « d'un cran »
# le sont partout de la même façon.
ESPACES = {'xs': '4px', 'sm': '8px', 'md': '16px',
           'lg': '28px', 'xl': '48px', 'xxl': '80px'}

RAYONS = {'sm': '8px', 'md': '14px', 'lg': '22px', 'pilule': '999px'}

# Tailles de texte fluides : clamp(min, préféré, max) adapte la typographie à
# la largeur de l'écran sans point de rupture brutal.
TEXTES = {
    'display': 'clamp(2.4rem, 5vw, 3.6rem)',
    'h1':      'clamp(1.9rem, 3.6vw, 2.7rem)',
    'h2':      'clamp(1.35rem, 2.2vw, 1.75rem)',
    'h3':      'clamp(1.05rem, 1.5vw, 1.2rem)',
    'corps':   '1rem',
    'petit':   '0.86rem',
    'label':   '0.72rem',
}

POLICE_TITRE = "'Cormorant Garamond', 'Hina Mincho', Georgia, serif"
POLICE_TEXTE = "'Jost', 'Slight', system-ui, sans-serif"
POLICE_MONO  = "'JetBrains Mono', ui-monospace, monospace"


# ─────────────────────────────────────────────
# MODE D'AFFICHAGE
# ─────────────────────────────────────────────
def _lire_preference() -> str:
    try:
        mode = json.loads(PREFERENCES.read_text(encoding='utf-8')).get('mode')
        return mode if mode in PALETTES else MODE_DEFAUT
    except (OSError, ValueError):
        return MODE_DEFAUT


def _ecrire_preference(mode: str):
    """Échouer ici ne doit jamais empêcher le site de fonctionner : une
    préférence non enregistrée est un désagrément, pas une panne."""
    try:
        PREFERENCES.parent.mkdir(exist_ok=True)
        PREFERENCES.write_text(json.dumps({'mode': mode}), encoding='utf-8')
    except OSError:
        pass


def mode_actuel() -> str:
    if CLE_MODE not in st.session_state:
        st.session_state[CLE_MODE] = _lire_preference()
    return st.session_state[CLE_MODE]


def couleurs() -> dict:
    """Palette du mode actif — pour le code Python (Folium, Altair)."""
    return PALETTES[mode_actuel()]


def selecteur_mode():
    actuel = mode_actuel()
    choix = st.sidebar.radio(
        "Affichage", ['clair', 'sombre'],
        index=['clair', 'sombre'].index(actuel),
        format_func=lambda m: "☀️ Clair" if m == 'clair' else "🌙 Sombre",
        horizontal=True, key='choix_mode')
    if choix != actuel:
        st.session_state[CLE_MODE] = choix
        _ecrire_preference(choix)
        st.rerun()


# ─────────────────────────────────────────────
# INJECTION DU STYLE
# ─────────────────────────────────────────────
def inject_theme():
    c = couleurs()
    sombre = mode_actuel() == 'sombre'

    # Dans une URL de données SVG, le « # » d'une couleur hexadécimale doit
    # être encodé en %23, sans quoi le navigateur coupe l'URL à cet endroit.
    accent2_url = c['accent2'].replace('#', '%23')
    or_url      = c['or'].replace('#', '%23')

    # Grain de papier : un bruit très léger en SVG, qui évite l'aplat
    # numérique parfait et rappelle le support du modèle. Aucune image
    # externe à charger.
    grain = ("url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
             "width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence "
             "type='fractalNoise' baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E"
             "%3Crect width='140' height='140' filter='url(%23n)' opacity='"
             + ("0.05" if sombre else "0.09") + "'/%3E%3C/svg%3E\")")

    st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=Jost:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

    :root {{
      --bg:{c['bg']}; --panel:{c['panel']}; --panel2:{c['panel2']};
      --border:{c['border']}; --accent:{c['accent']}; --accent2:{c['accent2']};
      --text:{c['text']}; --muted:{c['muted']};
      --or:{c['or']}; --or-clair:{c['or_clair']};
      --marche:{c['marche']}; --critique:{c['critique']}; --arret:{c['arret']};
      --ambre:{c['ambre']};

      --esp-xs:{ESPACES['xs']}; --esp-sm:{ESPACES['sm']}; --esp-md:{ESPACES['md']};
      --esp-lg:{ESPACES['lg']}; --esp-xl:{ESPACES['xl']}; --esp-xxl:{ESPACES['xxl']};

      --r-sm:{RAYONS['sm']}; --r-md:{RAYONS['md']}; --r-lg:{RAYONS['lg']};
      --r-pilule:{RAYONS['pilule']};

      --t-display:{TEXTES['display']}; --t-h1:{TEXTES['h1']}; --t-h2:{TEXTES['h2']};
      --t-h3:{TEXTES['h3']}; --t-corps:{TEXTES['corps']};
      --t-petit:{TEXTES['petit']}; --t-label:{TEXTES['label']};

      --f-titre:{POLICE_TITRE}; --f-texte:{POLICE_TEXTE}; --f-mono:{POLICE_MONO};

      --ombre: 0 1px 2px rgba(46,44,40,{'0.35' if sombre else '0.04'}),
               0 8px 24px rgba(46,44,40,{'0.30' if sombre else '0.06'});
      --ombre-carte: var(--ombre);
      --ombre-levee: 0 4px 10px rgba(46,44,40,{'0.40' if sombre else '0.07'}),
                     0 18px 44px rgba(46,44,40,{'0.34' if sombre else '0.10'});
      --transition: 260ms cubic-bezier(.22,.61,.36,1);
    }}

    /* ---------- FOND : papier légèrement grainé ---------- */
    .stApp {{
      background:
        {grain},
        radial-gradient(900px 460px at 88% -8%, {c['accent2']}12, transparent 62%),
        radial-gradient(760px 420px at -6% 104%, {c['or']}10, transparent 58%),
        var(--bg);
      color: var(--text);
      font-family: var(--f-texte);
      font-size: var(--t-corps);
    }}

    .block-container {{ padding-top: var(--esp-lg) !important; max-width: 1500px; }}

    /* ---------- BARRE D'OUTILS STREAMLIT ---------- */
    header[data-testid="stHeader"] {{ background: transparent !important; }}
    header[data-testid="stHeader"] *, div[data-testid="stToolbar"] * {{
      color: var(--text) !important;
    }}
    div[data-testid="stDecoration"] {{ display: none; }}

    /* ---------- TYPOGRAPHIE ---------- */
    h1, h2, h3, h4 {{
      font-family: var(--f-titre) !important;
      color: var(--text) !important;
      font-weight: 600; letter-spacing: 0.005em; line-height: 1.15;
    }}
    .stApp h1 {{ font-size: var(--t-h1) !important; margin-bottom: var(--esp-sm); }}
    .stApp h2 {{
      font-size: var(--t-h2) !important; color: var(--accent) !important;
      border: none; padding-left: 0; text-transform: none;
      margin-top: var(--esp-xl);
    }}
    .stApp h3 {{
      font-size: var(--t-h3) !important; color: var(--text) !important;
      font-family: var(--f-texte) !important; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.12em;
      margin-top: var(--esp-lg);
    }}
    .stApp h5 {{
      font-family: var(--f-texte) !important; font-size: var(--t-label) !important;
      text-transform: uppercase; letter-spacing: 0.14em;
      color: var(--muted) !important; font-weight: 600;
    }}
    p, li, label, .stMarkdown {{ font-family: var(--f-texte); line-height: 1.65; }}
    code, kbd, pre {{ font-family: var(--f-mono) !important; font-size: 0.85em; }}
    hr {{ border-color: var(--border) !important; margin: var(--esp-xl) 0 !important; }}

    /* ---------- BARRE LATÉRALE ---------- */
    section[data-testid="stSidebar"] {{
      background: var(--panel); border-right: 1px solid var(--border);
    }}
    section[data-testid="stSidebar"] * {{ color: var(--text); }}
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {{
      color: var(--accent) !important; font-family: var(--f-texte) !important;
      font-size: var(--t-label) !important; text-transform: uppercase;
      letter-spacing: 0.14em; margin-top: var(--esp-lg);
    }}

    /* ---------- MÉTRIQUES ---------- */
    div[data-testid="stMetric"] {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: var(--r-md); padding: var(--esp-md) var(--esp-lg);
      box-shadow: var(--ombre); transition: box-shadow var(--transition),
                                            transform var(--transition);
    }}
    div[data-testid="stMetric"]:hover {{
      box-shadow: var(--ombre-levee); transform: translateY(-2px);
    }}
    div[data-testid="stMetricValue"] {{
      font-family: var(--f-titre) !important; font-weight: 600;
      color: var(--accent) !important; font-size: 2rem !important;
    }}
    div[data-testid="stMetricLabel"],
    div[data-testid="stMetricLabel"] * ,
    div[data-testid="stMetricLabel"] p {{
      font-family: var(--f-texte) !important; text-transform: uppercase;
      font-size: var(--t-label) !important; letter-spacing: 0.14em;
      color: var(--muted) !important; opacity: 1 !important;
    }}

    /* ---------- ALERTES ---------- */
    div[data-testid="stAlert"] {{
      border-radius: var(--r-md); border: 1px solid var(--border);
      font-family: var(--f-texte) !important; box-shadow: var(--ombre);
    }}

    /* ---------- BOUTONS ---------- */
    .stDownloadButton button, .stButton button {{
      font-family: var(--f-texte) !important; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.11em;
      font-size: var(--t-label); padding: 0.6rem 1.2rem;
      color: var(--accent); background: transparent;
      border: 1px solid var(--accent2); border-radius: var(--r-pilule);
      transition: all var(--transition);
    }}
    .stDownloadButton button:hover, .stButton button:hover {{
      background: var(--accent); color: var(--panel) !important;
      border-color: var(--accent); box-shadow: var(--ombre-levee);
      transform: translateY(-1px);
    }}
    .stButton button:focus-visible, .stDownloadButton button:focus-visible {{
      outline: 2px solid var(--or); outline-offset: 3px;
    }}

    /* ---------- CHAMPS ---------- */
    div[data-baseweb="input"], div[data-baseweb="select"] > div {{
      border-radius: var(--r-sm) !important; border-color: var(--border) !important;
      background: var(--panel) !important;
    }}
    section[data-testid="stFileUploaderDropzone"] {{
      background: var(--panel2); border: 1px dashed var(--accent2) !important;
      border-radius: var(--r-md); transition: all var(--transition);
    }}
    section[data-testid="stFileUploaderDropzone"]:hover {{
      border-color: var(--accent) !important; background: var(--panel);
    }}

    /* ---------- TABLEAUX / EXPANDERS ---------- */
    div[data-testid="stDataFrame"] {{
      border: 1px solid var(--border); border-radius: var(--r-md);
      overflow: hidden; box-shadow: var(--ombre);
    }}
    div[data-testid="stExpander"] {{
      border: 1px solid var(--border) !important; border-radius: var(--r-md);
      background: var(--panel); box-shadow: var(--ombre);
    }}
    /* Le titre d'un expander : même couleur que le reste du texte, et
       opacity forcée — Streamlit l'atténue par défaut, ce qui le rend
       presque illisible sur un fond clair. */
    div[data-testid="stExpander"] summary,
    div[data-testid="stExpander"] summary p {{
      font-family: var(--f-texte) !important; font-weight: 600;
    }}
    div[data-testid="stExpander"] summary,
    div[data-testid="stExpander"] summary * {{
      color: var(--text) !important; opacity: 1 !important;
    }}
    /* Les icônes de Streamlit sont des LIGATURES : le mot « arrow_drop_down »
       se dessine comme une flèche grâce à une police dédiée. Lui imposer une
       autre police afficherait le mot en toutes lettres, par-dessus le titre.
       Cette règle la lui rend, quoi qu'on applique au-dessus. */
    span[data-testid="stIconMaterial"],
    div[data-testid="stExpander"] summary span[data-testid="stIconMaterial"],
    .material-icons, .material-symbols-rounded, .material-symbols-outlined {{
      font-family: 'Material Symbols Rounded', 'Material Symbols Outlined',
                   'Material Icons' !important;
      font-weight: normal !important; letter-spacing: normal !important;
      text-transform: none !important;
    }}
    div[data-testid="stExpander"] summary:hover,
    div[data-testid="stExpander"] summary:hover * {{
      color: var(--accent) !important;
    }}
    /* Le corps déplié, lui, repasse sur le fond clair du thème */
    div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {{
      color: var(--text);
    }}

    .stApp [data-testid="stCaptionContainer"] {{
      color: var(--muted) !important; font-family: var(--f-texte) !important;
      font-size: var(--t-petit) !important;
    }}

    /* ---------- COMPOSANTS RÉUTILISABLES ---------- */
    .carte {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: var(--r-lg); padding: var(--esp-lg);
      box-shadow: var(--ombre); transition: all var(--transition);
    }}
    .carte:hover {{ box-shadow: var(--ombre-levee); transform: translateY(-3px); }}

    .etiquette {{
      display: inline-block; font-family: var(--f-texte);
      font-size: var(--t-label); letter-spacing: 0.14em; text-transform: uppercase;
      color: var(--muted); padding: 4px 12px; border-radius: var(--r-pilule);
      border: 1px solid var(--border); background: var(--panel2);
    }}

    .filet {{
      width: 46px; height: 1px; background: var(--or);
      margin: var(--esp-md) 0; opacity: 0.8;
    }}

    /* ---------- BANDEAU HERO ---------- */
    .hero {{
      position: relative; overflow: hidden;
      border: 1px solid var(--border); border-radius: var(--r-lg);
      padding: var(--esp-xl) var(--esp-xl) var(--esp-lg) var(--esp-xl);
      margin-bottom: var(--esp-lg);
      background: linear-gradient(135deg, var(--panel) 0%, var(--panel2) 100%);
      box-shadow: var(--ombre);
    }}
    /* Deux disques très pâles, décalés : la composition asymétrique du modèle,
       sans image à charger. */
    .hero::before {{
      content: ""; position: absolute; right: -90px; top: -120px;
      width: 340px; height: 340px; border-radius: 50%;
      background: radial-gradient(circle, {c['accent2']}1f, transparent 68%);
    }}
    .hero::after {{
      content: ""; position: absolute; right: 130px; bottom: -140px;
      width: 260px; height: 260px; border-radius: 50%;
      background: radial-gradient(circle, {c['or']}1c, transparent 70%);
    }}
    /* Bandeau de collines : SVG inline, dessiné aux couleurs de la palette.
       Aucune image à charger, net à toute résolution, et il se redimensionne
       avec la page — ce qu'une illustration de présentation ne sait pas faire. */
    .hero-paysage {{
      position: absolute; left: 0; right: 0; bottom: 0; height: 78px;
      pointer-events: none; opacity: {0.30 if sombre else 0.42};
      background-repeat: no-repeat; background-position: bottom center;
      background-size: 100% 100%;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 120' preserveAspectRatio='none'%3E%3Cpath d='M0,86 C120,58 220,96 330,74 C440,52 520,88 640,70 C760,52 860,90 980,72 C1080,58 1150,80 1200,68 L1200,120 L0,120 Z' fill='{accent2_url}'/%3E%3Cpath d='M0,104 C160,88 280,112 420,98 C560,84 680,110 820,96 C940,84 1080,106 1200,94 L1200,120 L0,120 Z' fill='{or_url}'/%3E%3C/svg%3E");
    }}

    .hero-tag {{
      font-family: var(--f-texte); font-size: var(--t-label);
      letter-spacing: 0.24em; color: var(--muted); text-transform: uppercase;
    }}
    .hero-title {{
      font-family: var(--f-titre); font-weight: 300;
      font-size: var(--t-display); line-height: 1.05;
      color: var(--text); margin: var(--esp-sm) 0 var(--esp-xs) 0;
      position: relative; z-index: 1;
    }}
    .hero-title .accent {{ color: var(--accent); font-weight: 600; font-style: italic; }}
    .hero-sub {{
      font-family: var(--f-texte); font-size: var(--t-petit);
      color: var(--muted); max-width: 60ch; position: relative; z-index: 1;
    }}
    .cursor {{ display: none; }}

    .meta-bar {{
      display: flex; flex-wrap: wrap; gap: var(--esp-lg);
      font-family: var(--f-texte); font-size: var(--t-label);
      letter-spacing: 0.08em; color: var(--muted); margin-top: var(--esp-md);
      border-top: 1px solid var(--border); padding-top: var(--esp-md);
    }}
    .meta-bar b {{ color: var(--accent2); font-weight: 500; }}
    .dot {{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--marche); margin-right: 6px;
    }}
    .dot.red {{ background: var(--critique); }}

    .chip {{
      display: inline-block; font-family: var(--f-texte);
      font-size: var(--t-label); padding: 4px 12px; border-radius: var(--r-pilule);
      border: 1px solid var(--border); letter-spacing: 0.1em;
    }}
    .chip.ok   {{ color: var(--marche);   border-color: var(--marche);   background: {c['marche']}14; }}
    .chip.warn {{ color: var(--ambre);    border-color: var(--ambre);    background: {c['ambre']}14; }}
    .chip.crit {{ color: var(--critique); border-color: var(--critique); background: {c['critique']}14; }}

    /* ---------- ANIMATIONS ---------- */
    @keyframes apparait {{
      from {{ opacity: 0; transform: translateY(14px); }}
      to   {{ opacity: 1; transform: none; }}
    }}
    .hero, .carte, div[data-testid="stMetric"] {{
      animation: apparait 520ms cubic-bezier(.22,.61,.36,1) both;
    }}

    /* Respect du réglage système : certaines personnes désactivent les
       animations pour raison médicale (vertiges, migraines). */
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        animation-duration: 0.01ms !important; animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important; scroll-behavior: auto !important;
      }}
      .carte:hover, div[data-testid="stMetric"]:hover,
      .stButton button:hover {{ transform: none; }}
    }}

    /* ---------- RESPONSIVE ---------- */
    @media (max-width: 900px) {{
      .block-container {{ padding-left: var(--esp-md) !important;
                          padding-right: var(--esp-md) !important; }}
      .hero {{ padding: var(--esp-lg) var(--esp-md); }}
      .hero::before, .hero::after {{ display: none; }}
      .meta-bar {{ gap: var(--esp-md); }}
    }}
    /* Aucun débordement horizontal, quelle que soit la largeur. */
    .stApp, .block-container {{ overflow-x: hidden; }}
    </style>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# COMPOSANTS
# ─────────────────────────────────────────────
def hero_banner(title_accent: str, title_rest: str, subtitle: str,
                tag: str = "DGRE // RÉSEAU HYDRO-PLUVIOMÉTRIQUE",
                paysage: bool = True):
    """Grand bandeau d'en-tête, une « slide de titre » par page.

    `paysage` ajoute le bandeau de collines. Le laisser à True partout : il est
    assez discret pour ne pas concurrencer une carte ou un tableau, et c'est
    lui qui donne au site son unité visuelle d'une page à l'autre.
    """
    bande = '<div class="hero-paysage"></div>' if paysage else ''
    st.markdown(f"""
    <div class="hero">
      <div class="hero-tag">{tag}</div>
      <div class="hero-title"><span class="accent">{title_accent}</span> {title_rest}</div>
      <div class="filet"></div>
      <div class="hero-sub">{subtitle}</div>
      {bande}
    </div>""", unsafe_allow_html=True)


def illustration_village(hauteur: int = 210) -> str:
    """L'aquarelle du modèle Canva, encodée dans la page.

    Réservée à la page d'accueil : c'est le visuel de couverture, comme la
    première slide d'une présentation. Sur les pages de travail, une
    illustration décorative volerait l'attention de la carte et des tableaux.

    Encodée en base64 plutôt que servie comme fichier : Streamlit ne publie
    pas les fichiers locaux à une adresse utilisable depuis du CSS.
    Retourne une chaîne vide si l'image manque — l'accueil reste affichable.
    """
    import base64
    chemin = Path(__file__).parent / 'assets' / 'village.png'
    try:
        donnees = base64.b64encode(chemin.read_bytes()).decode('ascii')
    except OSError:
        return ""
    return f"""
    <div style="height:{hauteur}px;margin:-{hauteur // 3}px 0 var(--esp-lg) 0;
                background-image:url('data:image/png;base64,{donnees}');
                background-size:contain;background-repeat:no-repeat;
                background-position:bottom center;pointer-events:none;"></div>"""


def signal_meta(items: dict, network_ok: bool = True):
    """Barre de métadonnées sous le hero."""
    dot = "dot" if network_ok else "dot red"
    cells = "".join(f"<span><b>{k}</b> {v}</span>" for k, v in items.items())
    st.markdown(
        f'<div class="meta-bar"><span><span class="{dot}"></span>'
        f'{"LIAISON ACTIVE" if network_ok else "LIAISON DÉGRADÉE"}</span>{cells}</div>',
        unsafe_allow_html=True)
