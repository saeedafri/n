import streamlit as st
st.set_page_config(
    page_title="Sales Forecast Intelligence | Coresight Research",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ══════════════════════════════════════════════════════════════
#  CUSTOM CSS — Coresight Research · Premium Edition
# ══════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,300;0,14..32,400;0,14..32,500;0,14..32,600;0,14..32,700;0,14..32,800;0,14..32,900&display=swap');

/* ══ KEYFRAME ANIMATIONS ══ */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(16px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes fadeIn {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes slideInLeft {
  from { opacity: 0; transform: translateX(-12px); }
  to   { opacity: 1; transform: translateX(0); }
}
@keyframes shimmer {
  0%   { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
@keyframes pulse-red {
  0%, 100% { box-shadow: 0 0 0 0 rgba(200,16,46,0); }
  50%       { box-shadow: 0 0 0 6px rgba(200,16,46,0.08); }
}
@keyframes barGrow {
  from { width: 0; }
  to   { width: 100%; }
}
@keyframes countUp {
  from { opacity: 0; transform: scale(0.92); }
  to   { opacity: 1; transform: scale(1); }
}

/* ══ BASE ══ */
* { box-sizing: border-box; }
html, body, [class*="css"], .stApp {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
  -webkit-font-smoothing: antialiased !important;
}

/* ══ APP BACKGROUND ══ */
.stApp { background: #F4F5F7 !important; }
.main .block-container {
  padding-top: 1.5rem !important;
  max-width: 1280px !important;
  animation: fadeIn 0.4s ease both;
}

/* ══ SIDEBAR ══ */
[data-testid="stSidebar"] {
  background: #FFFFFF !important;
  border-right: 1px solid #E8EAED !important;
  box-shadow: 2px 0 12px rgba(0,0,0,0.04) !important;
}
[data-testid="collapsedControl"] {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
}
[data-testid="stSidebar"] > div:first-child { background: #FFFFFF !important; }
[data-testid="stSidebar"], [data-testid="stSidebar"] p,
[data-testid="stSidebar"] span, [data-testid="stSidebar"] label,
[data-testid="stSidebar"] div { color: #374151 !important; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
  color: #9CA3AF !important; font-size: 10px !important; font-weight: 700 !important;
  letter-spacing: 0.1em !important; text-transform: uppercase !important;
  margin: 1.6rem 0 0.7rem !important; padding-bottom: 0 !important;
  border-bottom: none !important;
}
[data-testid="stSidebar"] label { font-size: 12px !important; font-weight: 500 !important; color: #6B7280 !important; }

/* Sidebar inputs */
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="select"] > div > div {
  background: #F9FAFB !important; border: 1px solid #E8EAED !important;
  border-radius: 8px !important; color: #111827 !important;
  transition: border-color 0.15s !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] > div:hover { border-color: #C8102E !important; }
[data-testid="stSidebar"] [data-baseweb="select"] svg { color: #9CA3AF !important; fill: #9CA3AF !important; }
[data-testid="stSidebar"] [data-baseweb="popover"] { background: #FFFFFF !important; border: 1px solid #E8EAED !important; border-radius: 10px !important; box-shadow: 0 8px 24px rgba(0,0,0,0.1) !important; }
[data-testid="stSidebar"] [role="option"] { background: #FFFFFF !important; color: #374151 !important; padding: 8px 14px !important; }
[data-testid="stSidebar"] [role="option"]:hover { background: #FFF5F6 !important; color: #C8102E !important; }
[data-testid="stSidebar"] input[type="text"],
[data-testid="stSidebar"] input[type="password"],
[data-testid="stSidebar"] input[type="number"],
[data-testid="stSidebar"] textarea {
  background: #F9FAFB !important; border: 1px solid #E8EAED !important;
  border-radius: 8px !important; color: #111827 !important; font-size: 13px !important;
  transition: border-color 0.15s, box-shadow 0.15s !important;
}
[data-testid="stSidebar"] [data-baseweb="input"] {
  background: #F9FAFB !important; border: 1px solid #E8EAED !important; border-radius: 8px !important;
}
[data-testid="stSidebar"] [data-baseweb="input"] > div { background: #F9FAFB !important; }
[data-testid="stSidebar"] [data-baseweb="input"] button,
[data-testid="stSidebar"] [data-baseweb="input"] span { background: #F9FAFB !important; color: #9CA3AF !important; }
[data-testid="stSidebar"] input:focus { border-color: #C8102E !important; box-shadow: 0 0 0 3px rgba(200,16,46,0.08) !important; outline: none !important; }
[data-testid="stSidebar"] [data-testid="stFileUploader"] { background: transparent !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
  background: #F9FAFB !important; border: 1.5px dashed #E8EAED !important;
  border-radius: 10px !important; transition: border-color 0.2s !important;
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]:hover { border-color: #C8102E !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * { color: #9CA3AF !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
  background: #FFFFFF !important; color: #374151 !important;
  border: 1px solid #E8EAED !important; border-radius: 6px !important;
  font-size: 12px !important; font-weight: 500 !important; padding: 4px 12px !important;
  transition: all 0.15s !important;
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button:hover { border-color: #C8102E !important; color: #C8102E !important; }
[data-testid="stSidebar"] details { background: #F9FAFB !important; border: 1px solid #E8EAED !important; border-radius: 8px !important; }
[data-testid="stSidebar"] summary { color: #374151 !important; font-size: 13px !important; font-weight: 500 !important; }
[data-testid="stSidebar"] div[data-baseweb="slider"] div[role="progressbar"] { background: #C8102E !important; }
[data-testid="stSidebar"] div[data-baseweb="slider"] div[role="slider"] {
  background: #C8102E !important; border: 2px solid #FFFFFF !important;
  box-shadow: 0 1px 4px rgba(200,16,46,0.35) !important;
  transition: transform 0.15s !important;
}
[data-testid="stSidebar"] div[data-baseweb="slider"] div[role="slider"]:hover { transform: scale(1.2) !important; }

/* Sidebar Run button */
[data-testid="stSidebar"] .stButton > button {
  background: #C8102E !important; background-color: #C8102E !important;
  color: #FFFFFF !important; border: none !important; outline: none !important;
  box-shadow: 0 2px 8px rgba(200,16,46,0.3) !important;
  font-weight: 700 !important; font-size: 14px !important;
  letter-spacing: 0.03em !important; border-radius: 8px !important;
  transition: background 0.15s, box-shadow 0.15s, transform 0.1s !important;
}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span,
[data-testid="stSidebar"] .stButton > button div {
  color: #FFFFFF !important;
  background: transparent !important;
  border: none !important; outline: none !important; box-shadow: none !important;
}
[data-testid="stSidebar"] .stButton > button:hover,
[data-testid="stSidebar"] .stButton > button:hover p,
[data-testid="stSidebar"] .stButton > button:hover span {
  background: #A50D26 !important; background-color: transparent !important;
  color: #FFFFFF !important; box-shadow: none !important;
  transform: none !important; border: none !important; outline: none !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
  background: #A50D26 !important;
  box-shadow: 0 4px 14px rgba(200,16,46,0.35) !important;
  transform: translateY(-1px) !important;
}
[data-testid="stSidebar"] .stButton > button:active { transform: translateY(0) !important; }
[data-testid="stSidebar"] .stButton > button:focus,
[data-testid="stSidebar"] .stButton > button:focus-visible,
[data-testid="stSidebar"] .stButton > button:focus p {
  background: #A50D26 !important; color: #FFFFFF !important;
  border: none !important; outline: none !important; box-shadow: none !important;
}

/* ══ HERO ══ */
.hero-container {
  background: #FFFFFF;
  border: 1px solid #E8EAED;
  border-radius: 16px;
  padding: 3rem 3.5rem 2.6rem;
  margin-bottom: 1.5rem;
  text-align: center;
  position: relative;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 8px 32px rgba(0,0,0,0.04);
  animation: fadeInUp 0.5s ease both;
}
.hero-container::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 4px;
  background: linear-gradient(90deg, #C8102E 0%, #FF4D6D 50%, #C8102E 100%);
  background-size: 200% 100%;
  animation: shimmer 3s ease infinite;
}
.hero-container::after {
  content: '';
  position: absolute; bottom: 0; left: 10%; right: 10%; height: 1px;
  background: linear-gradient(90deg, transparent, #E8EAED, transparent);
}
.hero-eyebrow {
  font-size: 10px; font-weight: 700; letter-spacing: 0.2em;
  text-transform: uppercase; color: #C8102E; margin-bottom: 14px;
  animation: fadeIn 0.6s ease 0.1s both;
}
.hero-title {
  font-size: 2.6rem; font-weight: 800; color: #0F172A;
  letter-spacing: -1.5px; line-height: 1.05; margin: 0;
  animation: fadeInUp 0.5s ease 0.15s both;
}
.hero-title span { color: #C8102E; }
.hero-sub {
  font-size: 0.92rem; color: #6B7280; margin-top: 12px; font-weight: 400;
  line-height: 1.6; animation: fadeIn 0.6s ease 0.25s both;
}
.hero-divider {
  width: 24px; height: 2px; background: #E8EAED; margin: 16px auto; border-radius: 2px;
}
.hero-badges {
  margin-top: 18px; display: flex; justify-content: center; gap: 6px; flex-wrap: wrap;
  animation: fadeIn 0.6s ease 0.35s both;
}
.badge {
  background: #F9FAFB; border: 1px solid #E8EAED; border-radius: 20px;
  padding: 4px 12px; font-size: 11px; color: #6B7280; font-weight: 500;
  letter-spacing: 0.01em; transition: all 0.2s;
}
.badge:hover { background: #FFF5F6; border-color: #C8102E; color: #C8102E; }

/* ══ SECTION HEADERS ══ */
.section-header {
  display: flex; align-items: flex-start; gap: 12px;
  margin: 2.5rem 0 1.2rem; padding-bottom: 0;
  border-bottom: none;
  animation: slideInLeft 0.4s ease both;
}
.section-marker { width: 3px; height: 22px; background: #C8102E; border-radius: 2px; flex-shrink: 0; margin-top: 2px; }
.section-title { font-size: 15px; font-weight: 700; color: #0F172A; letter-spacing: -0.2px; }
.section-sub   { font-size: 12px; color: #9CA3AF; margin-top: 2px; font-weight: 400; }

/* ══ TABS — underline style ══ */
.stTabs [data-baseweb="tab-list"] {
  background: transparent !important;
  border-radius: 0 !important; padding: 0 !important; gap: 0 !important;
  border: none !important;
  border-bottom: 2px solid #E8EAED !important;
}
.stTabs [data-baseweb="tab"] {
  border-radius: 0 !important; color: #9CA3AF !important;
  font-weight: 500 !important; font-size: 13px !important;
  padding: 10px 18px !important; background: transparent !important;
  border: none !important; border-bottom: 2px solid transparent !important;
  margin-bottom: -2px !important; transition: color 0.15s !important;
}
.stTabs [data-baseweb="tab"]:hover { color: #374151 !important; }
.stTabs [aria-selected="true"] {
  background: transparent !important; color: #C8102E !important;
  font-weight: 600 !important; box-shadow: none !important;
  border-bottom: 2px solid #C8102E !important;
}

/* ══ METRIC CARDS — premium with colored top border ══ */
[data-testid="stMetric"] {
  background: #FFFFFF !important;
  border: 1px solid #E8EAED !important;
  border-top: 3px solid #C8102E !important;
  border-radius: 12px !important;
  padding: 20px !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03) !important;
  transition: box-shadow 0.2s, transform 0.2s !important;
  animation: fadeInUp 0.4s ease both;
}
[data-testid="stMetric"]:hover {
  box-shadow: 0 4px 16px rgba(0,0,0,0.08) !important;
  transform: translateY(-2px) !important;
}
[data-testid="stMetricLabel"] {
  color: #9CA3AF !important; font-size: 10px !important; font-weight: 700 !important;
  text-transform: uppercase !important; letter-spacing: 0.08em !important;
}
[data-testid="stMetricValue"] {
  color: #0F172A !important; font-size: 1.7rem !important;
  font-weight: 800 !important; letter-spacing: -0.5px !important;
  animation: countUp 0.4s ease both;
}
[data-testid="stMetricDelta"] { font-size: 11px !important; font-weight: 600 !important; }

/* ══ EXPANDERS ══ */
div[data-testid="stExpander"] {
  background: #FFFFFF !important; border: 1px solid #E8EAED !important;
  border-radius: 12px !important; overflow: hidden !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
  transition: box-shadow 0.2s !important;
}
div[data-testid="stExpander"]:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.06) !important; }
div[data-testid="stExpander"] > details { background: #FFFFFF !important; }
div[data-testid="stExpander"] > details > summary {
  background: #FAFAFA !important; color: #111827 !important;
  font-weight: 600 !important; font-size: 13px !important;
  padding: 14px 18px !important; border-radius: 12px !important;
  border-bottom: 1px solid #F3F4F6 !important; list-style: none !important;
  cursor: pointer !important; transition: background 0.15s !important;
}
div[data-testid="stExpander"] > details > summary:hover { background: #F3F4F6 !important; }
div[data-testid="stExpander"] > details[open] > summary { border-radius: 12px 12px 0 0 !important; }
div[data-testid="stExpander"] > details > summary svg { color: #9CA3AF !important; fill: #9CA3AF !important; }
div[data-testid="stExpander"] > details > div { background: #FFFFFF !important; padding: 18px !important; }
div[data-testid="stExpander"] * { color: #374151 !important; }
div[data-testid="stExpander"] h1, div[data-testid="stExpander"] h2,
div[data-testid="stExpander"] h3, div[data-testid="stExpander"] h4 { color: #0F172A !important; }
[data-baseweb="accordion"] { background: #FFFFFF !important; }
[data-baseweb="accordion"] > div { background: #FFFFFF !important; color: #111827 !important; }

/* ══ PROGRESS ══ */
.stProgress { border-radius: 6px !important; overflow: hidden !important; }
.stProgress > div { background: #F3F4F6 !important; border-radius: 6px !important; }
.stProgress > div > div {
  background: linear-gradient(90deg, #C8102E, #FF4D6D) !important;
  border-radius: 6px !important;
  animation: barGrow 0.4s ease both;
  box-shadow: 0 0 8px rgba(200,16,46,0.3) !important;
}

/* ══ GLOBAL BUTTONS ══ */
.stButton > button {
  background: #FFFFFF !important; color: #374151 !important;
  border: 1px solid #E8EAED !important; border-radius: 8px !important;
  font-weight: 500 !important; font-size: 13px !important;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
  outline: none !important;
  transition: all 0.15s ease !important;
}
.stButton > button p, .stButton > button span {
  background: transparent !important;
  border: none !important; outline: none !important; box-shadow: none !important;
}
.stButton > button:hover {
  background: #FFF5F6 !important; color: #C8102E !important;
  border-color: #C8102E !important; box-shadow: none !important;
  outline: none !important;
}
.stButton > button:hover p, .stButton > button:hover span { color: #C8102E !important; background: transparent !important; }
.stButton > button:focus, .stButton > button:active, .stButton > button:focus-visible {
  box-shadow: none !important; outline: none !important;
}

/* ══ MAIN BUTTONS (non-sidebar) ══ */
.main .stButton button { padding: 8px 16px !important; }

/* ══ ALERTS ══ */
.stAlert { border-radius: 10px !important; border: none !important; animation: fadeIn 0.3s ease both; }
[data-testid="stInfo"]    { background: #F0F9FF !important; border-left: 3px solid #0EA5E9 !important; }
[data-testid="stSuccess"] { background: #F0FDF4 !important; border-left: 3px solid #22C55E !important; }
[data-testid="stWarning"] { background: #FFFBEB !important; border-left: 3px solid #F59E0B !important; }
[data-testid="stError"]   { background: #FFF1F2 !important; border-left: 3px solid #C8102E !important; }

/* ══ UPLOAD ZONE ══ */
.upload-zone {
  background: #FFFFFF; border: 1.5px dashed #D1D5DB; border-radius: 14px;
  padding: 4rem 2rem; text-align: center; margin: 1.5rem 0;
  transition: all 0.25s ease; animation: fadeIn 0.4s ease both;
}
.upload-zone:hover { border-color: #C8102E; background: #FFF5F6; transform: scale(1.005); }
.upload-title { font-size: 1rem; font-weight: 700; color: #0F172A; margin-bottom: 6px; }
.upload-sub   { font-size: 13px; color: #9CA3AF; margin-top: 6px; line-height: 1.7; }

/* ══ FIX CARDS ══ */
.fix-card {
  background: #FFF5F6; border: 1px solid #FECDD3; border-radius: 10px;
  padding: 14px 18px; margin-bottom: 10px;
  transition: box-shadow 0.2s; animation: fadeInUp 0.3s ease both;
}
.fix-card:hover { box-shadow: 0 4px 12px rgba(200,16,46,0.08); }
.fix-title { font-size: 12px; font-weight: 700; color: #C8102E; margin-bottom: 5px; letter-spacing: 0.01em; text-transform: uppercase; }
.fix-body  { font-size: 13px; color: #374151; line-height: 1.65; }

/* ══ SEVERITY ══ */
.severity-minor    { background: #FFF7ED; border: 1px solid #FED7AA; border-radius: 10px; padding: 12px 16px; color: #9A3412; font-size: 13px; }
.severity-moderate { background: #FEF3C7; border: 1px solid #FDE68A; border-radius: 10px; padding: 12px 16px; color: #92400E; font-size: 13px; }
.severity-critical { background: #FFF1F2; border: 1px solid #FECDD3; border-radius: 10px; padding: 12px 16px; color: #881337; font-size: 13px; font-weight: 600; }

/* ══ GLOBAL TEXT ══ */
h1,h2,h3,h4,h5,h6 { color: #0F172A !important; font-weight: 700 !important; }
p, li { color: #374151 !important; }
.stMarkdown p { color: #374151 !important; line-height: 1.7 !important; }
small, .caption, [data-testid="stCaptionContainer"] { color: #9CA3AF !important; font-size: 12px !important; }
code { background: #FFF1F2 !important; color: #C8102E !important; border-radius: 4px !important; padding: 2px 6px !important; font-size: 12px !important; border: 1px solid #FECDD3 !important; }
hr { border: none !important; border-top: 1px solid #E8EAED !important; margin: 1.5rem 0 !important; }

/* ══ DATAFRAMES ══ */
[data-testid="stDataFrame"] { border: 1px solid #E8EAED !important; border-radius: 12px !important; overflow: hidden !important; }
[data-testid="stDataFrame"] > div { color-scheme: light !important; }
[data-testid="stDataFrame"] * { color-scheme: light !important; }

/* ══ NUMBER INPUT — compact ══ */
[data-testid="stNumberInput"] { max-width: 160px !important; }
[data-testid="stNumberInput"] input {
  background: #F9FAFB !important; color: #111827 !important;
  border: 1px solid #E8EAED !important; border-radius: 8px !important;
  font-size: 13px !important; font-weight: 600 !important;
  height: 36px !important; padding: 0 10px !important;
}
[data-testid="stNumberInput"] button {
  background: #F3F4F6 !important; color: #6B7280 !important;
  border: 1px solid #E8EAED !important; transition: all 0.15s !important;
  min-width: 28px !important; max-width: 32px !important;
}
[data-testid="stNumberInput"] button:hover { background: #FFF5F6 !important; border-color: #C8102E !important; color: #C8102E !important; }

/* ══ FILE UPLOADER — compact & clean font ══ */
[data-testid="stFileUploaderDropzone"] { padding: 12px 14px !important; }
[data-testid="stFileUploaderDropzone"] span,
[data-testid="stFileUploaderDropzone"] p,
[data-testid="stFileUploaderDropzone"] small,
[data-testid="stFileUploaderDropzone"] div {
  font-size: 12px !important;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
  line-height: 1.4 !important;
}
[data-testid="stFileUploaderFileName"],
[data-testid="stFileUploaderFile"],
[data-testid="stFileUploaderFile"] span,
[data-testid="stFileUploaderFile"] p,
[data-testid="stFileUploaderFile"] small {
  font-size: 12px !important;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}

/* ══ CODE BLOCKS ══ */
[data-testid="stCode"], [data-testid="stCode"] > div, [data-testid="stCode"] pre,
.stCode, .stCode > div, .stCode pre, [class*="stCodeBlock"] {
  background: #F8FAFC !important; border: 1px solid #E8EAED !important;
  border-radius: 10px !important; color: #1e293b !important;
}
[data-testid="stCode"] code, [data-testid="stCode"] span, pre code, pre span {
  color: #1e293b !important; background: transparent !important;
}

/* ══ GLOBAL WIDGET OVERRIDES ══ */
[data-baseweb="select"] > div:first-child {
  background: #F9FAFB !important; border-color: #E8EAED !important; color: #111827 !important;
  transition: border-color 0.15s !important;
}
[data-baseweb="select"] > div:first-child:hover { border-color: #C8102E !important; }
[data-baseweb="select"] * { color: #111827 !important; }
[data-baseweb="select"] svg { fill: #9CA3AF !important; color: #9CA3AF !important; }
[data-baseweb="popover"] > div, [data-baseweb="menu"], [role="listbox"] {
  background: #FFFFFF !important; border: 1px solid #E8EAED !important;
  border-radius: 10px !important; box-shadow: 0 8px 24px rgba(0,0,0,0.1) !important;
}
[role="option"], li[role="option"] { background: #FFFFFF !important; color: #374151 !important; }
[role="option"]:hover, li[role="option"]:hover { background: #FFF5F6 !important; color: #C8102E !important; }
[data-baseweb="input"] > div, [data-baseweb="base-input"] {
  background: #F9FAFB !important; border-color: #E8EAED !important; color: #111827 !important;
}
[data-baseweb="input"] button { background: #F9FAFB !important; }
[data-baseweb="input"] svg { fill: #9CA3AF !important; }

/* ══ POPOVER / EXPORT BUTTON ══ */
[data-testid="stPopover"] > button,
[data-testid="stPopover"] button[kind="secondary"] {
  background: #FFFFFF !important; color: #374151 !important;
  border: 1px solid #E8EAED !important; border-radius: 8px !important;
  font-weight: 500 !important; font-size: 13px !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
}
[data-testid="stPopover"] > button:hover {
  border-color: #C8102E !important; color: #C8102E !important;
  background: #FFF5F6 !important;
}
[data-testid="stPopover"] > button p,
[data-testid="stPopover"] > button span { color: inherit !important; background: transparent !important; }
[data-testid="stPopoverBody"] { background: #FFFFFF !important; }
[data-testid="stPopoverBody"] button {
  background: #FFFFFF !important; color: #374151 !important;
  border: 1px solid #E8EAED !important; border-radius: 8px !important;
  font-size: 13px !important; font-weight: 500 !important;
  width: 100% !important; margin-bottom: 4px !important;
}
[data-testid="stPopoverBody"] button:hover {
  border-color: #C8102E !important; color: #C8102E !important;
  background: #FFF5F6 !important;
}
[data-testid="stPopoverBody"] button p,
[data-testid="stPopoverBody"] button span { color: inherit !important; background: transparent !important; }

/* ══ CHAT ══ */
[data-testid="stChatInput"] > div, [data-testid="stChatInputContainer"],
[data-testid="stChatInputContainer"] > div, [data-testid="stChatInputContainer"] textarea {
  background: #FFFFFF !important; border: 1px solid #E8EAED !important;
  border-radius: 12px !important; color: #111827 !important;
}
[data-testid="stChatInputContainer"] button { background: #C8102E !important; border-radius: 8px !important; }
[data-testid="stBottom"], [data-testid="stBottom"] > div {
  background: #F4F5F7 !important; border-top: 1px solid #E8EAED !important;
}
[data-testid="stChatMessage"] {
  background: #FFFFFF !important; border: 1px solid #F3F4F6 !important;
  border-radius: 12px !important; animation: fadeInUp 0.3s ease both;
  box-shadow: 0 1px 4px rgba(0,0,0,0.04) !important;
}

/* ══ SCROLLBAR ══ */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #E8EAED; border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: #C8102E; }

/* ══ PROGRESS LABEL ══ */
[data-testid="stProgressLabel"], .stProgress ~ p { color: #6B7280 !important; font-size: 12px !important; font-weight: 500 !important; }
[data-testid="stBottom"] { backdrop-filter: blur(8px) !important; }

/* ══ HIDE STREAMLIT CHROME ══ */
[data-testid="stToolbar"] { display: none !important; }
[data-testid="stHeader"] { background: transparent !important; height: 0 !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stSidebarCollapseButton"] { display: none !important; }
#MainMenu { display: none !important; }
header { display: none !important; }
footer { display: none !important; }

/* ══ PREVENT SIDEBAR COLLAPSE ══ */
button[kind="header"] { display: none !important; }

/* ══ FILE UPLOADER BROWSE BUTTON — premium style ══ */
[data-testid="stFileUploaderDropzone"] button,
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
    background: #FFFFFF !important;
    color: #374151 !important;
    border: 1px solid #E8EAED !important;
    border-radius: 8px !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    padding: 6px 16px !important;
    transition: all 0.15s !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important;
}
[data-testid="stFileUploaderDropzone"] button:hover {
    border-color: #C8102E !important;
    color: #C8102E !important;
    background: #FFF5F6 !important;
}
</style>
""", unsafe_allow_html=True)

# ── Helper: render HTML safely ────────────────────────────────
def html(s): st.markdown(s, unsafe_allow_html=True)

# ── Premium matplotlib style ─────────────────────────────────
def _make_xlsx(df):
    """Convert DataFrame to xlsx bytes for download."""
    import io, openpyxl
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Forecast')
        ws = writer.sheets['Forecast']
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(
                len(str(col[0].value or '')),
                max(len(str(c.value or '')) for c in col)
            ) + 3
    return buf.getvalue()

def style_ax(ax, title="", xlabel="", ylabel=""):
    """Apply premium styling to a matplotlib axis."""
    ax.set_facecolor('#FFFFFF')
    ax.figure.patch.set_facecolor('#FFFFFF')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#E8EAED')
    ax.spines['bottom'].set_color('#E8EAED')
    ax.tick_params(colors='#9CA3AF', labelsize=10, length=0)
    ax.yaxis.label.set_color('#6B7280')
    ax.xaxis.label.set_color('#6B7280')
    ax.grid(True, axis='y', color='#F3F4F6', linewidth=1, linestyle='-')
    ax.grid(False, axis='x')
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, fontsize=12, fontweight='600', color='#0F172A',
                     pad=12, loc='left')
    if xlabel: ax.set_xlabel(xlabel, fontsize=10, color='#9CA3AF', labelpad=8)
    if ylabel: ax.set_ylabel(ylabel, fontsize=10, color='#9CA3AF', labelpad=8)

# ── Styled HTML table — replaces st.dataframe to avoid dark iframe ──────────
def render_table(df, fmt=None):
    """Render a pandas DataFrame as a clean styled HTML table."""
    import pandas as _pd
    df = df.copy()
    if fmt:
        if isinstance(fmt, str):
            for col in df.select_dtypes(include='number').columns:
                df[col] = df[col].apply(lambda x: fmt.format(x) if _pd.notna(x) else '')
        elif isinstance(fmt, dict):
            for col, f in fmt.items():
                if col in df.columns:
                    df[col] = df[col].apply(lambda x: f.format(x) if _pd.notna(x) else '')
    rows = ""
    for _, row in df.iterrows():
        cells = "".join(f"<td>{v}</td>" for v in row)
        rows += f"<tr>{cells}</tr>"
    headers = "".join(f"<th>{c}</th>" for c in df.columns)
    index_col = ""
    if df.index.name or not isinstance(df.index, _pd.RangeIndex):
        headers = f"<th>{df.index.name or ''}</th>" + headers
        rows_with_idx = ""
        for idx, row in df.iterrows():
            cells = "".join(f"<td>{v}</td>" for v in row)
            rows_with_idx += f"<tr><td style='font-weight:500;color:#6B7280'>{idx}</td>{cells}</tr>"
        rows = rows_with_idx
    import time as _time
    _tid = f"t{abs(hash(str(df.columns.tolist()) + str(_time.time())))}"
    html(f"""
    <div style="overflow-x:auto;border-radius:10px;border:1px solid #E5E7EB;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
    <table id="{_tid}" style="width:100%;border-collapse:collapse;font-size:13px;font-family:Inter,-apple-system,sans-serif">
      <thead>
        <tr style="background:#F9FAFB;border-bottom:2px solid #E5E7EB">
          {headers}
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table></div>
    <style>
    #{_tid} td,#{_tid} th{{padding:10px 16px;text-align:left;border-bottom:1px solid #F3F4F6;color:#374151;white-space:nowrap}}
    #{_tid} th{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#6B7280;background:#F9FAFB}}
    #{_tid} tbody tr:nth-child(even) td{{background:#FAFAFA}}
    #{_tid} tbody tr:last-child td{{border-bottom:none}}
    #{_tid} tbody tr:hover td{{background:#FFF5F6;color:#111827}}
    </style>
    """)

# ── Metric card renderer ──────────────────────────────────────
def metric_card(label, value, delta=None, delta_type="good"):
    delta_html = ""
    if delta:
        cls = f"delta-{delta_type}"
        icon = "▲" if delta_type=="good" else ("▼" if delta_type=="bad" else "●")
        delta_html = f'<div class="metric-delta {cls}">{icon} {delta}</div>'
    return f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value">{value}</div>
        {delta_html}
    </div>"""

# ── Section header renderer ───────────────────────────────────
def section_header(icon, title, sub="", color="#C8102E"):
    html(f"""
    <div class="section-header">
        <div class="section-marker"></div>
        <div>
            <div class="section-title">{title}</div>
            {"" if not sub else f'<div class="section-sub">{sub}</div>'}
        </div>
    </div>""")

# ── Diagnostic card renderer ──────────────────────────────────
def diag_cards(lb10, lb20, sw_p, dw):
    _d1,_d2,_d3,_d4 = st.columns(4)
    _d1.metric('Ljung-Box lag 10', f'p={lb10:.4f}',
               'Pass' if lb10>0.05 else 'Fail')
    _d2.metric('Ljung-Box lag 20', f'p={lb20:.4f}',
               'Pass' if lb20>0.05 else 'Fail')
    _d3.metric('Shapiro-Wilk',     f'p={sw_p:.4f}',
               'Normal' if sw_p>0.05 else 'Non-normal')
    _d4.metric('Durbin-Watson',    f'{dw:.4f}',
               'OK' if 1.5<dw<2.5 else 'Serial corr.')
    return lb10, lb20, sw_p, dw


import os, warnings, requests
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from datetime import datetime
from itertools import combinations
from pandas.tseries.offsets import MonthEnd
from scipy import stats
from scipy.stats import linregress
from sklearn.metrics import mean_squared_error
from sklearn.linear_model import LinearRegression

from statsmodels.tsa.stattools      import adfuller, grangercausalitytests, kpss
from statsmodels.tsa.seasonal       import seasonal_decompose
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.stats.diagnostic   import acorr_ljungbox, het_arch
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.stats.stattools    import durbin_watson

warnings.filterwarnings('ignore')

# ── Config file for persistent FRED key ──────────────────────
CONFIG_FILE = 'config.txt'

def load_fred_key():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return f.read().strip()
    return ''

def save_fred_key(key):
    with open(CONFIG_FILE, 'w') as f:
        f.write(key.strip())

# ── FRED variable list ────────────────────────────────────────
FRED_VARS = {
    'UNRATENSA'      : 'Unemployment Rate',
    'LNU01300000'    : 'Labor Force Participation Rate',
    'HOUSTNSA'       : 'New Privately-Owned Housing Units Started: Total Units',
    'HSN1FNSA'       : 'New One Family Houses Sold: United States',
    'GASREGCOVM'     : 'US Regular Conventional Gas Price',
    'UMCSENT'        : 'University of Michigan: Consumer Sentiment',
    'CSUSHPINSA'     : 'S&P/Case-Shiller U.S. National Home Price Index',
    'PPIACO'         : 'Producer Price Index by Commodity: All Commodities',
    'T10YIEM'        : '10-Year Breakeven Inflation Rate',
    'TOTALNS'        : 'Total Consumer Credit Owned and Securitized',
    'CCLACBM027NBOG' : 'Consumer Loans: Credit Cards and Other Revolving Plans',
    'MICH'           : 'University of Michigan: Inflation Expectation',
    'CPALTT01USM657N': 'Consumer Price Index: Total All Items for the United States',
    'CONSUMERNSA'    : 'Consumer Loans, All Commercial Banks',
    'IPB51000N'      : 'Industrial Production: Consumer Goods',
    'RETAILIMNSA'    : 'Retailers Inventories',
    'MNFCTRIMNSA'    : 'Manufacturers Inventories',
    'TOTBUSIRNSA'    : 'Total Business: Inventories to Sales Ratio',
    'RETAILIRNSA'    : 'Retailers: Inventories to Sales Ratio',
    'TOTBUSIMNSA'    : 'Total Business Inventories',
    'UCOGNO'         : 'Manufacturers New Orders: Consumer Goods',
    'UCDGNO'         : 'Manufacturers New Orders: Consumer Durable Goods',
    'UMNMNO'         : 'Manufacturers New Orders: Nondurable Goods',
    'UMDMNO'         : 'Manufacturers New Orders: Durable Goods',
    'UMTMVS'         : 'Manufacturers Value of Shipments: Total Manufacturing',
    'WHLSLRIRNSA'    : 'Merchant Wholesalers: Inventories to Sales Ratio',
    'MNFCTRIRNSA'    : 'Manufacturers: Inventories to Sales Ratio',
    'CEU4349200001'  : 'All Employees, Couriers and Messengers',
    'IR'             : 'Import Price Index (End Use): All Commodities',
    'CES0500000003'  : 'Average Hourly Earnings of All Employees, Total Private',
    'FEDFUNDS'       : 'Federal Funds Effective Rate',
    'A229RX0'        : 'Real Disposable Personal Income: Per Capita',
}

# ═══════════════════════════════════════════════════════════
#  FREQUENCY DETECTION & CONFIG
# ═══════════════════════════════════════════════════════════

# COVID period — defined globally so all model sections can access it
COVID_START = pd.Timestamp('2020-03-01')
COVID_END   = pd.Timestamp('2020-06-30')
covid_months = pd.date_range('2020-03-31', '2020-06-30', freq='ME')

FREQ_CONFIGS = {
    'Weekly': {
        'freq': 'W',  'alias': 'W',
        's': 52,
        'max_lag': 52,
        'pred_default': 4,
        'cv_initial': '730 days',
        'cv_period':  '90 days',
        'cv_horizon': '30 days',
        'decomp_period': 52,
        'covid_rule': 'weekly',   # flag individual weeks
        'resample': 'ffill',      # how to bring monthly FRED to this freq
        'label': 'Weekly',
    },
    'Monthly': {
        'freq': 'ME', 'alias': 'ME',
        's': 12,
        'max_lag': 12,
        'pred_default': 2,
        'cv_initial': '1095 days',
        'cv_period':  '182 days',
        'cv_horizon': '92 days',
        'decomp_period': 12,
        'covid_rule': 'monthly',
        'resample': None,         # FRED is already monthly
        'label': 'Monthly',
    },
    'Quarterly': {
        'freq': 'QE', 'alias': 'QE',
        's': 4,
        'max_lag': 8,
        'pred_default': 2,
        'cv_initial': '1095 days',
        'cv_period':  '182 days',
        'cv_horizon': '92 days',
        'decomp_period': 4,
        'covid_rule': 'quarterly',
        'resample': 'mean',       # resample monthly FRED to quarterly mean
        'label': 'Quarterly',
    },
    'Annual': {
        'freq': 'YE', 'alias': 'YE',
        's': 1,
        'max_lag': 5,
        'pred_default': 1,
        'cv_initial': '1825 days',
        'cv_period':  '365 days',
        'cv_horizon': '365 days',
        'decomp_period': None,    # skip decomposition
        'covid_rule': 'annual',
        'resample': 'mean',
        'label': 'Annual',
    },
}

def detect_frequency(date_index):
    """Auto-detect data frequency from date index."""
    if len(date_index) < 2:
        return 'Monthly'
    diffs = pd.Series(date_index).diff().dropna().dt.days
    median_gap = diffs.median()
    if median_gap <= 10:
        return 'Weekly'
    elif median_gap <= 45:
        return 'Monthly'
    elif median_gap <= 100:
        return 'Quarterly'
    else:
        return 'Annual'

def normalize_dates(series):
    """Robustly convert a column of possibly-messy date labels to real dates.

    Handles, in addition to standard date strings:
      • plain 4-digit year integers/strings (2020 -> 2020-12-31)
      • quarter formats: 'Q1 2020', '2020Q1', '2020-Q1', 'Q1-2020'
    Anything unrecognised falls back to pandas' normal parser.
    Returns a datetime Series (unparseable entries become NaT).
    """
    import re

    s = series.copy()

    # ── Case 1: plain year values (int or string like 2020) ──
    # Guard against pandas misreading a bare year integer as nanoseconds.
    def _looks_like_year(v):
        try:
            iv = int(float(v))
            return 1900 <= iv <= 2100 and float(v) == iv
        except (ValueError, TypeError):
            return False

    non_null = s.dropna()
    if len(non_null) > 0 and non_null.apply(_looks_like_year).mean() >= 0.9:
        # Treat every value as a calendar year -> map to Dec 31 of that year
        return pd.to_datetime(
            s.apply(lambda v: f"{int(float(v))}-12-31" if _looks_like_year(v) else None),
            errors='coerce')

    # ── Case 2: quarter formats ──
    q_map = {'1': '-03-31', '2': '-06-30', '3': '-09-30', '4': '-12-31'}
    def _parse_quarter(v):
        if not isinstance(v, str):
            return None
        txt = v.strip().upper().replace('-', ' ').replace('_', ' ')
        m = re.search(r'Q([1-4])\s*(\d{4})', txt) or re.search(r'(\d{4})\s*Q([1-4])', txt)
        if not m:
            m2 = re.search(r'Q([1-4])(\d{4})', txt) or re.search(r'(\d{4})Q([1-4])', txt)
            if m2:
                g = m2.groups()
                q, y = (g[0], g[1]) if len(g[0]) == 1 else (g[1], g[0])
                return f"{y}{q_map[q]}"
            return None
        g = m.groups()
        if len(g[0]) == 1:
            q, y = g[0], g[1]
        else:
            y, q = g[0], g[1]
        return f"{y}{q_map[q]}"

    if non_null.astype(str).apply(lambda v: _parse_quarter(v) is not None).mean() >= 0.9:
        return pd.to_datetime(s.apply(_parse_quarter), errors='coerce')

    # ── Case 3: everything else — standard parser ──
    return pd.to_datetime(s, errors='coerce')

# ═══════════════════════════════════════════════════════════
#  SMART COLUMN DETECTION — find the date + value columns
# ═══════════════════════════════════════════════════════════

def _score_date_column(series):
    """Fraction of values in a column that parse as valid dates."""
    try:
        return normalize_dates(series).notna().mean()
    except Exception:
        return 0.0

def guess_date_column(df):
    """Pick the most likely date column: name hints first, then best-parsing."""
    name_hints = ['date', 'month', 'period', 'time', 'day', 'week',
                  'quarter', 'year', 'ds', 'timestamp', 'dt']
    for col in df.columns:
        cl = str(col).strip().lower()
        if any(h in cl for h in name_hints):
            if _score_date_column(df[col]) >= 0.6:
                return col
    best_col, best_score = None, 0.0
    for col in df.columns:
        score = _score_date_column(df[col])
        if score > best_score:
            best_col, best_score = col, score
    return best_col if best_score >= 0.6 else None

def guess_value_column(df, date_col):
    """Pick the most likely sales/value column: name hints first, then most-numeric."""
    name_hints = ['sales', 'revenue', 'value', 'amount', 'demand', 'units',
                  'volume', 'qty', 'quantity', 'y', 'target', 'total', 'count']
    candidates = [c for c in df.columns if c != date_col]
    for col in candidates:
        cl = str(col).strip().lower()
        if any(h == cl or h in cl for h in name_hints):
            if pd.to_numeric(df[col], errors='coerce').notna().mean() >= 0.6:
                return col
    best_col, best_score = None, 0.0
    for col in candidates:
        score = pd.to_numeric(df[col], errors='coerce').notna().mean()
        if score > best_score:
            best_col, best_score = col, score
    return best_col if best_score >= 0.6 else None

def get_covid_mask(index, rule):
    """Return covid dummy appropriate for the data frequency."""
    if rule == 'weekly':
        covid_start = pd.Timestamp('2020-03-01')
        covid_end   = pd.Timestamp('2020-06-30')
        return ((index >= covid_start) & (index <= covid_end)).astype(float)
    elif rule == 'monthly':
        covid_months = pd.date_range('2020-03-31', '2020-06-30', freq='ME')
        return index.isin(covid_months).astype(float)
    elif rule == 'quarterly':
        covid_qtrs = pd.date_range('2020-03-31', '2020-09-30', freq='QE')
        return index.isin(covid_qtrs).astype(float)
    elif rule == 'annual':
        return (index.year == 2020).astype(float)
    return pd.Series(0.0, index=index)

def resample_fred_to_freq(fred_monthly_series, target_index, method):
    """Bring monthly FRED data to target frequency."""
    if method is None:
        return fred_monthly_series.reindex(target_index)
    elif method == 'ffill':
        # Upsample monthly → target freq via forward fill
        combined = fred_monthly_series.reindex(
            fred_monthly_series.index.union(target_index)).ffill()
        return combined.reindex(target_index)
    elif method == 'mean':
        # Downsample monthly → lower freq via mean
        return fred_monthly_series.resample(target_index.freq).mean().reindex(target_index)

# ═══════════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════════

with st.sidebar:
    html("""
    <div style="padding:20px 20px 18px;margin-bottom:0">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
        <img src="data:image/png;base64,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" style="height:44px;width:auto;object-fit:contain" alt="Coresight Research">
      </div>
      <div style="height:1px;background:linear-gradient(90deg,#E8EAED 60%,transparent);margin:0 -20px 0 -20px"></div>
    </div>
    """)

    # ── File upload ──────────────────────────────────────────
    st.subheader("Data")
    uploaded = st.file_uploader(
        "Upload Sales Excel file", type=['xlsx','xls'],
        help=(
            "File needs a date column and a sales/value column — "
            "column names are auto-detected.\n\n"
            "**Accepted date formats:**\n"
            "- Full date: `2020-12-31` (recommended — never ambiguous)\n"
            "- Month-year: `2020-01` or `Jan 2020`\n"
            "- Year only: `2020`\n"
            "- Quarter: `Q1 2020`, `2020Q1`, `2020-Q1`\n\n"
            "**Tips:**\n"
            "- Use one consistent format for every row\n"
            "- Prefer `YYYY-MM-DD` to avoid day/month confusion\n"
            "- No gaps or duplicate periods"
        ))

    # ── Smart column detection + overrides ───────────────────
    date_col  = None
    value_col = None
    if uploaded:
        try:
            _raw = pd.read_excel(uploaded)
            _raw.columns = [str(c).strip() for c in _raw.columns]
            uploaded.seek(0)
            _guess_date  = guess_date_column(_raw)
            _guess_value = guess_value_column(_raw, _guess_date)
            _cols = list(_raw.columns)

            if _guess_date is None or _guess_value is None:
                st.error(
                    "Couldn't find a date column and a numeric value column in "
                    f"this file.\n\nColumns found: {_cols}\n\n"
                    "Your file needs one column of dates and one column of "
                    "numeric sales values."
                )
                uploaded = None
            else:
                st.subheader("Columns")
                _date_opts = [c for c in _cols if c != _guess_value] or _cols
                _d_index = _date_opts.index(_guess_date) if _guess_date in _date_opts else 0
                date_col = st.selectbox(
                    "Date column", _date_opts, index=_d_index,
                    help="Auto-detected — override if wrong")
                _val_opts = [c for c in _cols if c != date_col] or _cols
                _v_index = _val_opts.index(_guess_value) if _guess_value in _val_opts else 0
                value_col = st.selectbox(
                    "Sales / value column", _val_opts, index=_v_index,
                    help="Auto-detected — override if wrong")
                st.caption(f"Auto-detected: **{_guess_date}** (date) · "
                           f"**{_guess_value}** (value)")
        except Exception as e:
            st.error(f"Couldn't read the file: {e}")
            uploaded = None

    # ── Frequency detection ─────────────────────────────────
    st.subheader("Data Frequency")
    if uploaded and date_col:
        try:
            _tmp = pd.read_excel(uploaded)
            _tmp.columns = [str(c).strip() for c in _tmp.columns]
            uploaded.seek(0)
            _dates = normalize_dates(_tmp[date_col]).dropna()
            _detected = detect_frequency(pd.DatetimeIndex(_dates))
        except Exception:
            _detected = 'Monthly'
    else:
        _detected = 'Monthly'

    freq_override = st.selectbox(
        "Frequency (auto-detected — override if wrong)",
        ['Weekly','Monthly','Quarterly','Annual'],
        index=['Weekly','Monthly','Quarterly','Annual'].index(_detected),
        help="Auto-detected from your date column gaps"
    )
    if uploaded:
        st.caption(f"Auto-detected: **{_detected}** | Using: **{freq_override}**")
    FC = FREQ_CONFIGS[freq_override]   # shorthand for all frequency params

    # ── FRED API key ─────────────────────────────────────────
    st.subheader("FRED API Key")
    saved_key = load_fred_key()
    fred_key  = st.text_input("FRED API Key", value=saved_key,
                               type="password",
                               help="Get a free key at fred.stlouisfed.org")
    if fred_key != saved_key and fred_key:
        save_fred_key(fred_key)
        st.success("Key saved")

    # ── Model selection ──────────────────────────────────────
    st.subheader("Models")
    run_sarimax = st.checkbox("SARIMAX  (FRED exog)", value=True)
    run_sarima  = st.checkbox("SARIMA", value=True)
    run_prophet = st.checkbox("Prophet", value=True)

    # ── Shared parameters ────────────────────────────────────
    st.subheader("Parameters")
    horizon_label = {'Weekly':'weeks','Monthly':'months',
                     'Quarterly':'quarters','Annual':'years'}[freq_override]
    pred_periods = st.slider(
        f"Forecast horizon ({horizon_label})", 1,
        {'Weekly':260,'Monthly':60,'Quarterly':20,'Annual':5}[freq_override],
        FC['pred_default']
    )
    max_lag = st.slider(
        "Max Granger lag", 2, FC['max_lag'], min(FC['max_lag'], 12)
    )

    with st.expander("SARIMAX settings"):
        max_exog  = st.slider("Top N exog variables", 3, 10, 10)
        max_combo = st.slider("Max combo size", 1, 3, 1,
                               help="1=single vars only (fast), 2=pairs, 3=triples (slow)")

    with st.expander("Prophet settings"):
        cp_scale  = st.slider("Changepoint prior scale", 0.01, 0.5, 0.05, step=0.01,
                               help="Higher = more flexible trend")
        s_prior   = st.slider("Seasonality prior scale", 1, 20, 10,
                               help="Higher = stronger seasonality")
        season_mode = st.selectbox("Seasonality mode",
                                    ["auto-detect","additive","multiplicative"])

    run_btn = st.button("Run Analysis", type="primary", use_container_width=True)

    # Persist detected columns so the main body can use them
    if uploaded and date_col and value_col:
        st.session_state['fc_date_col']  = date_col
        st.session_state['fc_value_col'] = value_col

# ═══════════════════════════════════════════════════════════
#  MAIN PANEL — before run
# ═══════════════════════════════════════════════════════════

html("""
<style>
@keyframes fadeInUp {
  from { opacity:0; transform:translateY(18px); }
  to   { opacity:1; transform:translateY(0); }
}
@keyframes fadeIn {
  from { opacity:0; }
  to   { opacity:1; }
}
@keyframes shimmer {
  0%   { background-position:-200% 0; }
  100% { background-position:200% 0; }
}
@keyframes countUp {
  from { opacity:0; transform:scale(0.94); }
  to   { opacity:1; transform:scale(1); }
}
@keyframes slideInLeft {
  from { opacity:0; transform:translateX(-14px); }
  to   { opacity:1; transform:translateX(0); }
}
@keyframes barIn {
  from { transform:scaleX(0); transform-origin:left; }
  to   { transform:scaleX(1); transform-origin:left; }
}
.hero-container { animation: fadeInUp 0.55s cubic-bezier(0.16,1,0.3,1) both; }
.hero-eyebrow   { animation: fadeIn 0.5s ease 0.1s both; }
.hero-title     { animation: fadeInUp 0.5s cubic-bezier(0.16,1,0.3,1) 0.12s both; }
.hero-sub       { animation: fadeIn 0.5s ease 0.22s both; }
.hero-badges    { animation: fadeIn 0.5s ease 0.32s both; }
.section-header { animation: slideInLeft 0.4s cubic-bezier(0.16,1,0.3,1) both; }
.upload-zone    { animation: fadeInUp 0.5s ease 0.15s both; }
[data-testid="stMetric"] { animation: fadeInUp 0.45s cubic-bezier(0.16,1,0.3,1) both; }
[data-testid="stMetric"]:nth-child(2) { animation-delay:0.06s; }
[data-testid="stMetric"]:nth-child(3) { animation-delay:0.12s; }
[data-testid="stMetric"]:nth-child(4) { animation-delay:0.18s; }
[data-testid="stChatMessage"] { animation: fadeInUp 0.3s ease both; }
.stAlert { animation: fadeIn 0.3s ease both; }
.fix-card { animation: fadeInUp 0.3s ease both; }
</style>
""")

# Inject JS counter animation for metric values
html("""
<script>
function animateCounters() {
  const metrics = document.querySelectorAll('[data-testid="stMetricValue"]');
  metrics.forEach(el => {
    const text = el.innerText;
    const num = parseFloat(text.replace(/[^0-9.]/g,''));
    if (isNaN(num) || num === 0) return;
    const suffix = text.replace(/[0-9.,]/g,'');
    const prefix = text.match(/^[^0-9]*/)?.[0] || '';
    let start = 0; const duration = 800;
    const startTime = performance.now();
    function update(now) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const ease = 1 - Math.pow(1 - progress, 3);
      const current = start + (num - start) * ease;
      el.innerText = prefix + current.toLocaleString('en-US', {maximumFractionDigits: text.includes('.') ? 2 : 0}) + suffix;
      if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
  });
}
// Run on load and after Streamlit rerenders
setTimeout(animateCounters, 600);
const observer = new MutationObserver(() => setTimeout(animateCounters, 400));
observer.observe(document.body, {childList: true, subtree: true});
</script>
""")

html("""
<div class="hero-container">
    <div class="hero-eyebrow">CORESIGHT RESEARCH &nbsp;·&nbsp; ANALYTICS PLATFORM</div>
    <div class="hero-title">Sales Forecast <span>Intelligence</span></div>
    <div class="hero-sub">Retail sales forecasting — SARIMAX, SARIMA & Prophet with full diagnostic suite</div>
    <div class="hero-badges">
        <span class="badge">SARIMAX</span>
        <span class="badge">SARIMA</span>
        <span class="badge">Prophet</span>
        <span class="badge">33 FRED Variables</span>
        <span class="badge">Auto-Diagnostics</span>
        <span class="badge">Auto-Fix</span>
    </div>
</div>
""")

if not uploaded:
    st.session_state.pop('analysis_run', None)
    st.session_state.pop('fc_results', None)
    st.session_state.pop('fc_sales_series', None)
    st.session_state.pop('fc_freq', None)
    html("""
    <div class="upload-zone">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#D1D5DB" stroke-width="1.5" style="margin:0 auto 12px;display:block">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        <div class="upload-title">Drop your Excel file here</div>
        <div class="upload-sub">Use the file uploader in the sidebar &nbsp;·&nbsp; Supports Monthly, Weekly, Quarterly & Annual data</div>
    </div>""")
    st.stop()

# Mark analysis as run in session state so reruns don't reset to preview
if run_btn:
    st.session_state['analysis_run'] = True

if not st.session_state.get('analysis_run'):
    # Show data preview only
    df_preview = pd.read_excel(uploaded)
    df_preview.columns = [str(c).strip() for c in df_preview.columns]
    uploaded.seek(0)
    _dc = st.session_state.get('fc_date_col')
    _vc = st.session_state.get('fc_value_col')
    st.subheader("Data Preview")
    render_table(df_preview.tail(10))
    st.caption(f"{len(df_preview)} rows | Columns: {list(df_preview.columns)}")
    _p1,_p2,_p3,_p4 = st.columns(4)
    _p1.metric("Rows Loaded",  f"{len(df_preview):,}",              "Ready to analyse")
    _p2.metric("Date column",  str(_dc) if _dc else "—",            "Detected")
    _p3.metric("Value column", str(_vc) if _vc else "—",            "Detected")
    _p4.metric("Status",       "Ready",                             "Configure & run →")
    st.stop()

# ═══════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════

TARGET_COL = 'Sales'

# Load data only once — cache in session_state to avoid reloading on every rerun
if run_btn or 'fc_dfsales' not in st.session_state:
    with st.spinner("Loading data..."):
        uploaded.seek(0)
        dfsales = pd.read_excel(uploaded)
        dfsales.columns = [str(c).strip() for c in dfsales.columns]
        _dc = st.session_state.get('fc_date_col')
        _vc = st.session_state.get('fc_value_col')
        # Fallback if detection didn't run for some reason
        if _dc is None or _dc not in dfsales.columns:
            _dc = guess_date_column(dfsales)
        if _vc is None or _vc not in dfsales.columns:
            _vc = guess_value_column(dfsales, _dc)
        if _dc is None or _vc is None:
            st.error(
                "Could not identify a date column and a numeric value column "
                f"in this file. Columns found: {list(dfsales.columns)}"
            )
            st.stop()
        # Standardise to internal names: Date + Sales
        dfsales = dfsales[[_dc, _vc]].rename(columns={_dc: 'Date', _vc: TARGET_COL})
        dfsales[TARGET_COL] = pd.to_numeric(dfsales[TARGET_COL], errors='coerce')
        dfsales['Date']     = normalize_dates(dfsales['Date'])
        dfsales = dfsales.dropna(subset=['Date'])   # drop unparseable date rows
        # Guard: duplicate dates break downstream reindexing. Aggregate any
        # repeated periods (sum) and tell the user it happened.
        _dupes = int(dfsales['Date'].duplicated().sum())
        if _dupes > 0:
            dfsales = dfsales.groupby('Date', as_index=False)[TARGET_COL].sum()
            st.info(f"Found {_dupes} duplicate date(s) — rows sharing the same "
                    f"period were combined (summed) so the series has one value "
                    f"per period.")
        dfsales.set_index('Date', inplace=True)
        dfsales.index       = pd.to_datetime(dfsales.index)
        dfsales.sort_index(inplace=True)
        try:
            dfsales.index.freq = FC['freq']
        except Exception:
            dfsales = dfsales.asfreq(FC['freq'])
        st.session_state['fc_dfsales'] = dfsales
else:
    dfsales = st.session_state['fc_dfsales']

last_sales_dt  = dfsales.index[-1]
sales_series   = dfsales[TARGET_COL].dropna()

_unit_map = {'Weekly':'weeks','Monthly':'months','Quarterly':'quarters','Annual':'years'}
st.info(f"Frequency: **{freq_override}** | Seasonal period s=**{FC['s']}** | Forecast unit: **{_unit_map[freq_override]}**")

enddate = datetime.now().date()
df = pd.DataFrame(index=pd.date_range(
    dfsales.index[0], enddate, freq=FC['freq']))
df = df.join(dfsales)

# Initialise so they're always defined even on cached reruns
fred_status  = st.session_state.get('fc_fred_status', {})
fred_monthly = {}

# FRED pull + model fitting — only on fresh Run Analysis click
if run_btn:
  fred_status  = {}
  fred_monthly = {}   # store raw monthly FRED series for resampling
  if run_sarimax:
    if not fred_key:
        st.error("FRED API key required for SARIMAX. Add it in the sidebar.")
        st.stop()
    fred_status_txt = st.empty()
    fred_status_txt.markdown(
        '<p style="color:#6B7280;font-size:12px;font-weight:500;margin:4px 0 6px">Pulling FRED macro data — this takes ~30 seconds...</p>',
        unsafe_allow_html=True)
    fred_bar = st.progress(0)
    for i, (k, v) in enumerate(FRED_VARS.items()):
        try:
            resp = requests.get(
                f'https://api.stlouisfed.org/fred/series/observations'
                f'?series_id={k}&api_key={fred_key}&file_type=json', timeout=10)
            tmp = pd.DataFrame(resp.json()['observations'])[['date','value']]
            tmp['value'] = pd.to_numeric(tmp['value'], errors='coerce')
            tmp['date']  = pd.to_datetime(tmp['date']) + MonthEnd(1)
            tmp.set_index('date', inplace=True)
            tmp.index.freq = 'ME'
            tmp.columns = [v]
            fred_monthly[v] = tmp[v]   # keep raw monthly
            # Resample to target frequency
            resampled = resample_fred_to_freq(tmp[v], df.index, FC['resample'])
            df[v] = resampled
            fred_status[v] = '✓'
        except Exception as e:
            fred_status[v] = f'✗ {e}'
        fred_bar.progress((i+1)/len(FRED_VARS))
    fred_bar.empty()
    fred_status_txt.empty()
    st.session_state['fc_fred_status'] = fred_status
    if FC['resample']:
        st.caption(f"FRED variables (monthly) resampled to **{freq_override}** "
                   f"using **{'forward-fill' if FC['resample']=='ffill' else 'mean'}**")

df = df.fillna(df.mean())
df[TARGET_COL] = dfsales[TARGET_COL].reindex(df.index)

# COVID dummy — frequency-aware (covid_months defined globally above)
df['covid_shock'] = get_covid_mask(df.index, FC['covid_rule'])

# ── Seasonality mode ─────────────────────────────────────────
monthly_avg = dfsales.groupby(dfsales.index.month)[TARGET_COL].mean()
seasonal_strength = (monthly_avg.max()-monthly_avg.min())/sales_series.mean()*100
if season_mode == "auto-detect":
    SEASONALITY_MODE = 'multiplicative' if seasonal_strength > 20 else 'additive'
else:
    SEASONALITY_MODE = season_mode

# ═══════════════════════════════════════════════════════════
#  DIAGNOSTICS TAB  (always shown)
# ═══════════════════════════════════════════════════════════

# On a fresh Run Analysis click reset results; on rerun keep existing ones
if run_btn:
    results = {}
    st.session_state['fc_results'] = {}

else:
    results = st.session_state.get('fc_results', {})

# Drop results for models that are no longer selected, so stale entries from a
# previous run (e.g. SARIMAX after it's been unchecked) don't linger in the
# comparison table or get passed to the AI assistant.
_active_models = {m for m, on in [('SARIMAX', run_sarimax),
                                  ('SARIMA',  run_sarima),
                                  ('Prophet', run_prophet)] if on}
results = {k: v for k, v in results.items() if k in _active_models}
st.session_state['fc_results'] = results

# Build tab list
tab_labels = ["Diagnostics"]
if run_sarimax: tab_labels.append("SARIMAX")
if run_sarima:  tab_labels.append("SARIMA")
if run_prophet: tab_labels.append("Prophet")
if sum([run_sarimax, run_sarima, run_prophet]) > 1:
    tab_labels.append("Comparison")

tabs = st.tabs(tab_labels)
tab_idx = 0

# ── DIAGNOSTICS TAB ─────────────────────────────────────────
with tabs[tab_idx]:
    tab_idx += 1
    section_header("🔍", "Pre-Modelling Diagnostics", "Stationarity · Seasonality · Outliers · Decomposition", "#C8102E")

    _c1,_c2,_c3,_c4 = st.columns(4)
    _c1.metric("Total Records",  f"{len(sales_series):,}",
               f"{dfsales.index[0].strftime('%b %Y')} → {last_sales_dt.strftime('%b %Y')}")
    _c2.metric("Mean Sales",     f"{sales_series.mean():,.0f}", "Monthly average")
    _c3.metric("Std Deviation",  f"{sales_series.std():,.0f}",  "Volatility measure")
    _c4.metric("Peak Month",     f"Month {monthly_avg.idxmax()}", f"Avg {monthly_avg.max():,.0f}")

    st.divider()

    # Stationarity
    adf_stat, adf_p, *_ = adfuller(sales_series, autolag='AIC')
    kpss_stat, kpss_p, *_ = kpss(sales_series, regression='c', nlags='auto')

    _s1, _s2 = st.columns(2)
    _s1.metric("ADF Test p-value",  f"{adf_p:.4f}",
               "Stationary" if adf_p<=0.05 else "Non-stationary")
    _s2.metric("KPSS Test p-value", f"{kpss_p:.4f}",
               "✓ Stationary" if kpss_p>0.05 else "⚠ Non-stationary")

    st.divider()

    # Seasonality
    c1, c2, c3 = st.columns(3)
    c1.metric("Peak month", f"Month {monthly_avg.idxmax()} ({monthly_avg.max():,.0f})")
    c2.metric("Trough month", f"Month {monthly_avg.idxmin()} ({monthly_avg.min():,.0f})")
    c3.metric("Seasonal strength", f"{seasonal_strength:.1f}%",
              delta=f"Mode: {SEASONALITY_MODE}", delta_color="off")

    # Outliers
    z_scores = np.abs(stats.zscore(sales_series))
    outliers = sales_series[z_scores > 3]
    if len(outliers):
        st.warning(f"{len(outliers)} outlier(s) detected (|z|>3):")
        render_table(outliers.rename("Sales").to_frame())
    else:
        st.success("No outliers detected (|z|>3)")

    st.divider()

    # Decomposition
    st.markdown("**Seasonal Decomposition**")
    if FC['decomp_period'] and len(sales_series) > FC['decomp_period']*2:
        decomp = seasonal_decompose(sales_series, model='additive',
                                    period=FC['decomp_period'])
        fig, axes = plt.subplots(4, 1, figsize=(12, 9), facecolor='#FFFFFF')
        series_cfg = [
            (decomp.observed, 'Observed',  '#0F172A'),
            (decomp.trend,    'Trend',     '#C8102E'),
            (decomp.seasonal, 'Seasonal',  '#0EA5E9'),
            (decomp.resid,    'Residual',  '#9CA3AF'),
        ]
        for ax, (series, label, clr) in zip(axes, series_cfg):
            ax.plot(series.index, series.values, color=clr, lw=1.5)
            ax.fill_between(series.index, series.values, alpha=0.06, color=clr)
            style_ax(ax, title=label)
            ax.tick_params(labelsize=9)
        axes[-1].axhline(0, color='#E8EAED', lw=1)
        plt.tight_layout(h_pad=1.2)
        st.pyplot(fig, use_container_width=True)
        plt.close()
    else:
        st.info("Not enough data for seasonal decomposition (need at least 2 full seasonal cycles).")

    # FRED status if SARIMAX
    if run_sarimax and fred_status:
        with st.expander("FRED data pull status"):
            ok  = [k for k,v in fred_status.items() if v=='✓']
            bad = [k for k,v in fred_status.items() if v!='✓']
            st.success(f"{len(ok)} variables loaded")
            if bad:
                st.warning(f"✗ {len(bad)} failed: {bad}")

# ═══════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════

def diag_block(resid, label="", model_type="SARIMAX"):
    """Display residual diagnostics with auto-fix suggestions.

    Adapts to short series: Ljung-Box lags are capped to what the data can
    support, and each test is guarded so tiny datasets degrade gracefully
    instead of crashing.
    """
    _r = pd.Series(resid).dropna()
    n  = len(_r)

    if n < 4:
        st.warning(
            f"Not enough residuals ({n}) to run diagnostics. "
            "This usually means the dataset is very short — try a longer "
            "history for reliable diagnostics."
        )
        return

    # Ljung-Box lags must be < n. Use the usual 10/20 when possible, else
    # scale down to the largest lags this series can support.
    _lag_a = min(10, max(1, n // 2))
    _lag_b = min(20, max(_lag_a, n - 2))
    _lags  = sorted({_lag_a, _lag_b})

    try:
        lb   = acorr_ljungbox(_r, lags=_lags, return_df=True)
        lb10 = float(lb["lb_pvalue"].iloc[0])
        lb20 = float(lb["lb_pvalue"].iloc[-1])
    except Exception:
        lb10 = lb20 = np.nan

    try:
        _, sw_p = stats.shapiro(_r) if n >= 3 else (None, np.nan)
    except Exception:
        sw_p = np.nan
    try:
        dw = float(durbin_watson(_r))
    except Exception:
        dw = np.nan

    if len(_lags) < 2 or _lags != [10, 20]:
        st.caption(f"Short series ({n} residuals) — Ljung-Box tested at "
                   f"lag {_lags[0]}" + (f" and {_lags[-1]}" if len(_lags) > 1 else ""))

    def _fmt(v, fmt="{:.4f}"):
        return "n/a" if (v is None or (isinstance(v, float) and np.isnan(v))) else fmt.format(v)

    def _flag(v, good, bad):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "n/a"
        return good if v > 0.05 else bad

    # ── Metric cards ─────────────────────────────────────────
    c1,c2,c3,c4 = st.columns(4)
    c1.metric(f"Ljung-Box (lag {_lags[0]})", f"p={_fmt(lb10)}",
              delta=_flag(lb10, "✅ PASS", "❌ FAIL"), delta_color="off")
    c2.metric(f"Ljung-Box (lag {_lags[-1]})", f"p={_fmt(lb20)}",
              delta=_flag(lb20, "✅ PASS", "❌ FAIL"), delta_color="off")
    c3.metric("Shapiro-Wilk", f"p={_fmt(sw_p)}",
              delta=_flag(sw_p, "Normal", "Non-normal"), delta_color="off")
    c4.metric("Durbin-Watson", _fmt(dw),
              delta=("n/a" if np.isnan(dw) else ("OK" if 1.5<dw<2.5 else "Serial corr.")),
              delta_color="off")

    # ── Residual plots ────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.5), facecolor='#FFFFFF')
    # Use integer index so all models plot the same way (avoids 1970 issue)
    resid_vals = resid.values if hasattr(resid, 'values') else resid
    resid_idx  = range(len(resid_vals))
    axes[0].plot(resid_idx, resid_vals, color='#374151', linewidth=1, alpha=0.8)
    axes[0].fill_between(resid_idx, resid_vals, alpha=0.08, color='#374151')
    axes[0].axhline(0, color='#C8102E', ls='--', lw=1.2, alpha=0.7)
    style_ax(axes[0], title=f"{label} Residuals over time", xlabel="Observation index")
    n, bins, patches = axes[1].hist(resid, bins=28, edgecolor='white', linewidth=0.5)
    for patch in patches:
        patch.set_facecolor('#C8102E')
        patch.set_alpha(0.7)
    style_ax(axes[1], title="Residual distribution")
    plt.tight_layout(pad=1.5)
    st.pyplot(fig, use_container_width=True)
    plt.close()

    # ── Auto-fix panel ────────────────────────────────────────
    def _is_nan(v):
        return v is None or (isinstance(v, float) and np.isnan(v))

    fails = []
    if not _is_nan(lb10) and lb10 <= 0.05: fails.append("lb10")
    if not _is_nan(lb20) and lb20 <= 0.05: fails.append("lb20")
    if not _is_nan(sw_p) and sw_p <= 0.05: fails.append("normality")
    if not _is_nan(dw)   and not (1.5 < dw < 2.5): fails.append("dw")

    if not fails:
        st.success("All diagnostics pass — model is well-specified.")
        return

    with st.expander("Auto-Fix Suggestions", expanded=True):

        # ── Context: is MAPE good despite failures? ───────────
        st.markdown("#### What failed and why")

        if "lb10" in fails and "lb20" not in fails:
            st.warning(
                "**Ljung-Box lag 10 fails, lag 20 passes** — mild short-term "
                "autocorrelation only. This is common and usually has minimal "
                "impact on forecast accuracy. Check your MAPE first — if it's "
                "under 3%, this is likely acceptable."
            )
        if "lb10" in fails and "lb20" in fails:
            st.error(
                "**Both Ljung-Box lags fail** — the model is missing a systematic "
                "pattern. Forecasts may be biased. Action is recommended."
            )
        if "normality" in fails:
            st.warning(
                "**Non-normal residuals** — usually caused by COVID-period outliers "
                "creating fat tails. Affects confidence interval width more than "
                "point forecast accuracy."
            )
        if "dw" in fails:
            st.error(
                f"**Durbin-Watson = {dw:.4f}** — "
                + ("positive serial correlation detected (DW < 1.5). "
                   if dw < 1.5 else
                   "negative serial correlation detected (DW > 2.5). ")
                + "Consecutive residuals are correlated."
            )

        st.markdown("---")
        st.markdown("#### Recommended fixes")

        # ── Model-specific fixes ──────────────────────────────
        if model_type == "SARIMAX":
            if "lb10" in fails or "dw" in fails:
                st.markdown("""
**Fix 1 — Try a different combo rank (most likely fix)**
In the combo rank selector above, increase IDX by 1 and re-run.
A different variable combination may fit the short-term pattern better.
""")
                st.markdown("""
**Fix 2 — Increase Granger max lag**
In the sidebar, increase *Max Granger lag* to capture longer lead-time relationships.
Then re-run — new variables may emerge that explain the short-term autocorrelation.
""")
                st.markdown("""
**Fix 3 — Add MA term**
The current order may be missing a moving average component.
Try changing the SARIMAX order manually to include q=1 if it currently has q=0.
""")
            if "normality" in fails:
                st.markdown("""
**Fix 4 — Extend COVID dummy window**
The 4-month COVID dummy (Mar–Jun 2020) may not fully capture the shock.
Consider extending to Sep 2020 to absorb the recovery period residuals.
*(Update `covid_months` in app.py line ~95 to extend the date range)*
""")

        elif model_type == "SARIMA":
            if "lb10" in fails or "dw" in fails:
                st.markdown("""
**Fix 1 — Grid search already ran — order may need widening**
The current grid searches p,q in (0–2). Try expanding to (0–3) for a richer model.
*(Update `p_r=(0,3)` and `q_r=(0,3)` in the SARIMA grid search section of app.py)*
""")
                st.markdown("""
**Fix 2 — Switch to SARIMAX**
Adding even one exogenous variable (e.g. the covid_shock dummy) can resolve
residual autocorrelation that pure SARIMA cannot capture.
Enable SARIMAX in the sidebar and compare results.
""")
            if "normality" in fails:
                st.markdown("""
**Fix 3 — Non-normal residuals in SARIMA**
Without a COVID regressor, SARIMA absorbs the 2020 shock into residuals.
This is expected. Switch to SARIMAX with covid_shock to reduce the fat tails.
""")

        elif model_type == "Prophet":
            if "lb10" in fails or "dw" in fails:
                st.markdown("""
**Fix 1 — Increase changepoint prior scale**
In the sidebar → Prophet settings → move *Changepoint prior scale* from 0.05 to 0.10 or 0.15.
A more flexible trend absorbs short-term patterns that currently leak into residuals.
Re-run Prophet only to check if Ljung-Box improves.
""")
                st.markdown("""
**Fix 2 — Increase seasonality prior scale**
In the sidebar → Prophet settings → move *Seasonality prior scale* from 10 to 15 or 20.
Stronger seasonality fitting can eliminate periodic residual patterns.
""")
                st.markdown("""
**Fix 3 — Switch seasonality mode**
If currently on *additive*, try *multiplicative* (or vice versa).
A mismatch between the true seasonality structure and the mode
causes systematic residual patterns at seasonal lags.
""")
                st.markdown("""
**Fix 4 — Context check**
Prophet at lag 10 failing but lag 20 passing with MAPE < 1% is a very common
and acceptable result. Prophet's curve-fitting approach leaves small systematic
wobbles that Ljung-Box detects but that don't materially affect forecasts.
**If your MAPE is under 2%, consider this acceptable and proceed.**
""")
            if "normality" in fails:
                st.markdown("""
**Fix 5 — Non-normal Prophet residuals**
Almost always caused by COVID-period residuals. The covid_shock regressor
partially handles this but the recovery months (Jul–Dec 2020) may still
have non-normal residuals. This affects CI width slightly — report forecasts
with a ±1–2% manual buffer as a conservative adjustment.
""")

        # ── Severity assessment ───────────────────────────────
        st.markdown("---")
        st.markdown("#### Overall severity")
        n_critical = sum(1 for f in fails if f in ["lb20","dw"])
        n_minor    = sum(1 for f in fails if f in ["lb10","normality"])

        if n_critical == 0 and n_minor > 0:
            st.success(
                "**Minor issues only** — your model is usable. "
                "Apply fixes if MAPE is above 3%, otherwise proceed."
            )
        elif n_critical == 1:
            st.warning(
                "**Moderate issue** — at least one fix is recommended "
                "before using this model in production."
            )
        else:
            st.error(
                "**Multiple critical failures** — review model specification "
                "before using forecasts. Try a different model or parameter set."
            )

def forecast_chart(tail_idx, tail_vals, fc_idx, fc_mean,
                   fc_lower, fc_upper, title, color):
    fig, ax = plt.subplots(figsize=(12, 4))
    fig.patch.set_facecolor('#FFFFFF')
    # Actual line
    ax.plot(tail_idx, tail_vals, color='#374151', lw=2, label='Actual', zorder=3)
    ax.scatter(tail_idx[-1:], tail_vals[-1:], color='#374151', s=40, zorder=4)
    # Forecast
    ax.plot(fc_idx, fc_mean, color=color, lw=2.5, ls='--', label='Forecast', zorder=3)
    ax.scatter(fc_idx, fc_mean, color=color, s=30, zorder=4, alpha=0.8)
    # CI band
    ax.fill_between(fc_idx, fc_lower, fc_upper, alpha=0.12, color=color, label='95% CI')
    ax.fill_between(fc_idx, fc_lower, fc_upper, alpha=0.06, color=color)
    # Vertical separator
    ax.axvline(tail_idx[-1], color='#E8EAED', lw=1.5, ls=':', zorder=1)
    # Style
    style_ax(ax, title=title, ylabel='Sales')
    legend = ax.legend(frameon=True, framealpha=1, edgecolor='#E8EAED',
                       fontsize=10, loc='upper left')
    legend.get_frame().set_facecolor('#FFFFFF')
    for text in legend.get_texts(): text.set_color('#6B7280')
    plt.tight_layout()
    st.pyplot(fig, use_container_width=True)
    plt.close()

# ═══════════════════════════════════════════════════════════
#  SARIMAX TAB
# ═══════════════════════════════════════════════════════════

if run_sarimax:
    with tabs[tab_idx]:
        tab_idx += 1
        section_header("", "SARIMAX", "Granger causality · F-stat ranking · Combo search · 33 FRED variables", "#C8102E")

        # Guard: SARIMAX needs a reasonably long history. With very short series
        # (e.g. a handful of annual points) the ADF test and repeated
        # differencing can't run. Show a message in THIS tab only — SARIMA and
        # Prophet still run below.
        _SX_MIN_OBS = 24
        _sx_n = len(df.dropna())
        if _sx_n < _SX_MIN_OBS:
            st.warning(
                f"SARIMAX needs at least {_SX_MIN_OBS} data points "
                f"(this dataset has {_sx_n}). It relies on macro variables and "
                f"repeated differencing, which require a longer history.\n\n"
                f"**SARIMA and Prophet will still run in their tabs** — they're "
                f"better suited to short series like this."
            )
            _sarimax_runnable = False
        else:
            _sarimax_runnable = True

        # Weekly data: FRED is monthly and gets forward-filled, so each macro
        # value repeats across the weeks in a month. Be upfront that the macro
        # signal therefore operates at monthly resolution.
        if _sarimax_runnable and freq_override == 'Weekly':
            st.info(
                "FRED macro variables are monthly and have been forward-filled "
                "to weekly, so each value repeats across the weeks in a month. "
                "The macro signal therefore operates at monthly resolution — "
                "**SARIMA and Prophet may capture week-level patterns better.**"
            )

        if _sarimax_runnable:
          # ── Stationarity + Granger + Combo — only on Run Analysis ──
          if run_btn or 'fc_dflagg' not in st.session_state:
            with st.spinner("Running stationarity loop..."):
              dfsta  = df.dropna().copy()
              cols   = dfsta.columns

              def _safe_adf_p(series):
                  """ADF p-value, tolerant of short/degenerate series."""
                  s = pd.Series(series).dropna()
                  if len(s) < 6 or s.nunique() <= 1:
                      return 1.0   # treat as non-stationary if untestable
                  try:
                      return adfuller(s, autolag='AIC')[1]
                  except Exception:
                      return 1.0

              diffs  = 0
              while True:
                  sc = [c for c in dfsta.columns if _safe_adf_p(dfsta[c]) <= 0.05]
                  if set(sc) == set(dfsta.columns):
                      break
                  # Stop differencing before the series gets too short to model
                  if len(dfsta) - 1 < max(FC['s'] * 2, 12):
                      break
                  dfsta = dfsta.diff().dropna()
                  diffs += 1
            st.session_state['fc_sx_diffs'] = diffs
          else:
            diffs = st.session_state['fc_sx_diffs']
            dfsta = df.dropna().copy()
            cols  = dfsta.columns
          st.info(f"All variables stationary after **{diffs}** difference(s)")

          # ── Granger + dflagg — only on Run Analysis, else load from cache ──
          if run_btn or 'fc_dflagg' not in st.session_state:
            with st.spinner(f"Running Granger causality (33 vars × {max_lag} lags)..."):
              grangerlist = []
              for x in cols[1:]:
                  try:
                      gc  = grangercausalitytests(dfsta[[TARGET_COL, x]],
                                                 maxlag=max_lag, verbose=False)
                      tmp = [{'x':x,'lag':l,'ftest_stat':gc[l][0]['ssr_ftest'][0],
                              'ftest_pval':gc[l][0]['ssr_ftest'][1],
                              'xlabel':f'{x} ({l}m ago)'}
                             for l in range(1,max_lag+1)
                             if gc[l][0]['ssr_ftest'][1]<=0.05]
                      if tmp:
                          grangerlist.append(max(tmp, key=lambda r: r['ftest_stat']))
                  except Exception:
                      pass
              grangerdf = pd.DataFrame(grangerlist)
              if not grangerdf.empty:
                  # Hybrid forecast: a variable only needs a positive lag (≥1) to
                  # contribute real exog data within the lag window. Periods beyond
                  # the lag fall back to a neutral baseline, so we no longer require
                  # lag ≥ full horizon (which would filter out everything for long
                  # horizons like 5 years).
                  grangerdf = grangerdf[grangerdf['lag']>=1].reset_index(drop=True)

              if grangerdf.empty:
                  # No Granger-significant variables. Build empty structures so the
                  # "no variables found" message below can render instead of this
                  # block raising a KeyError on missing columns.
                  grangerdf = pd.DataFrame(
                      columns=['x','lag','ftest_stat','ftest_pval','xlabel','rmse'])
                  exog_ftest = []
                  dflagg = df[[TARGET_COL]].dropna().copy()
                  ix = dflagg.index.union(pd.date_range(
                      last_sales_dt+dflagg.index.freq,
                      periods=pred_periods+1, freq=dflagg.index.freq))
                  dflagg = dflagg.reindex(ix)
              else:
                  grangerdf['rmse'] = np.nan
                  for idx, row in grangerdf.iterrows():
                      X = df.dropna()[[row['x']]].values
                      Y = df.dropna()[TARGET_COL].values
                      lr = LinearRegression(); lr.fit(X,Y)
                      grangerdf.loc[idx,'rmse'] = np.sqrt(mean_squared_error(Y, lr.predict(X)))
                  exog_ftest = list(grangerdf.sort_values('ftest_stat', ascending=False)
                                             .iloc[:max_exog]['xlabel'].values)
                  dflagg = df[[TARGET_COL]].dropna().copy()
                  ix = dflagg.index.union(pd.date_range(
                      last_sales_dt+dflagg.index.freq,
                      periods=pred_periods+1, freq=dflagg.index.freq))
                  dflagg = dflagg.reindex(ix)
                  for _, row in grangerdf.iterrows():
                      # Shift by the dataset's own frequency (not hardcoded monthly)
                      # so weekly / quarterly / annual data lag correctly and don't
                      # produce colliding (duplicate) dates.
                      dflagg[row['xlabel']] = df[row['x']].shift(
                          periods=row['lag'], freq=FC['freq'])
              st.session_state['fc_grangerdf'] = grangerdf
              st.session_state['fc_dflagg']    = dflagg
              st.session_state['fc_exog_ftest']= exog_ftest
          else:
            grangerdf   = st.session_state['fc_grangerdf']
            dflagg      = st.session_state['fc_dflagg']
            exog_ftest  = st.session_state['fc_exog_ftest']

          section_header("🔗", "Granger-Significant Variables", "Variables with predictive power over Sales", "#C8102E")
          _sarimax_ok = not grangerdf.empty
          if not _sarimax_ok:
              st.warning("No Granger-significant variables found. Try increasing Max Granger lag in the sidebar. SARIMA and Prophet will still run normally in their tabs.")
          else:
            render_table(grangerdf[['xlabel','lag','ftest_stat','ftest_pval']].reset_index(drop=True),
                       fmt={'ftest_stat':'{:.3f}','ftest_pval':'{:.4f}'})

            # ── Combo search — only on Run Analysis ─────────────
            total_combos = sum(1 for i in range(1,max_combo+1)
                               for _ in combinations(exog_ftest, i))
            section_header("🔄", f"Combo Search — {total_combos} combinations", "Exhaustive SARIMAX variable combination ranking by RMSE", "#C8102E")

            if run_btn or 'fc_res_df' not in st.session_state:
                prog = st.progress(0, text="Starting combo search...")
                done = 0
                res_list = []
                dftmp = dflagg[exog_ftest+[TARGET_COL]].iloc[max_lag:]
                # Cap the ranking window so combos are compared on a sensible recent
                # window rather than the full (possibly multi-year) forecast horizon.
                _combo_win = min(pred_periods, max(FC['s'], 12), len(dftmp.dropna())//3)
                _combo_win = max(_combo_win, 1)
                train_c = dftmp.dropna()[:-_combo_win]
                test_c  = dftmp.dropna()[-_combo_win:]
                for size in range(1, max_combo+1):
                    for combo in combinations(exog_ftest, size):
                        combo = list(combo)
                        done += 1
                        prog.progress(done/total_combos,
                                      text=f"Combo {done}/{total_combos}: {combo[0][:30]}...")
                        try:
                            mf  = SARIMAX(train_c[TARGET_COL], exog=train_c[combo],
                                          order=(1,1,1), seasonal_order=(1,1,1,FC['s'])).fit(disp=False)
                            yp  = mf.forecast(steps=_combo_win, exog=test_c[combo])
                            rmse= np.sqrt(mean_squared_error(test_c[TARGET_COL].values, yp))
                            mape= np.mean(np.abs((test_c[TARGET_COL].values-yp)
                                                 /test_c[TARGET_COL].values))*100
                            res_list.append({'model':mf,'rmse':rmse,'mape':mape,'predvars':combo})
                        except Exception:
                            pass
                prog.empty()
                res_df = pd.DataFrame(res_list).sort_values('rmse').reset_index(drop=True)
                st.session_state['fc_res_df'] = res_df
            else:
                res_df = st.session_state['fc_res_df']

            render_table(res_df[['predvars','rmse','mape']].head(10).reset_index(drop=True),
                         fmt={'rmse':'{:.0f}','mape':'{:.2f}%'})

            # ── IDX selector — reads from session_state so works on rerun ──
            section_header("🎯", "Model Selection", "Choose combo rank — 0 = best RMSE", "#C8102E")
            # Always use cached res_df so combo rank works on rerun
            if 'fc_res_df' not in st.session_state and not run_btn:
                st.info("Click Run Analysis to see results")
                st.stop()
            _res_df = st.session_state.get('fc_res_df', res_df)
            # Use key only — let Streamlit manage the value so it persists across reruns
            if 'combo_rank_input' not in st.session_state:
                st.session_state['combo_rank_input'] = 0
            idx = int(st.number_input("Select combo rank (0 = best RMSE)", 0,
                                  max(0, len(_res_df)-1),
                                  step=1, key='combo_rank_input'))
            predvars = _res_df.iloc[idx]['predvars']
            sxorder  = _res_df.iloc[idx]['model'].model.order
            sxsorder = _res_df.iloc[idx]['model'].model.seasonal_order

            html(f"""
            <div style="background:#F9FAFB;border:1px solid #E5E7EB;border-radius:10px;padding:16px 20px;margin:8px 0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;line-height:1.8">
            <div><span style="color:#6B7280;font-weight:600;display:inline-block;width:110px">Variables</span><span style="color:#111827">{list(predvars)}</span></div>
            <div><span style="color:#6B7280;font-weight:600;display:inline-block;width:110px">Order</span><span style="color:#111827">ARIMA{sxorder}x{sxsorder}</span></div>
            <div style="margin-top:8px;padding-top:8px;border-top:1px solid #E5E7EB">
                <span style="color:#6B7280;font-weight:600;display:inline-block;width:110px">RMSE</span>
                <span style="color:#111827;font-weight:700">{res_df.iloc[idx]['rmse']:.0f}</span>
            </div>
            <div><span style="color:#6B7280;font-weight:600;display:inline-block;width:110px">MAPE</span>
                <span style="color:#C8102E;font-weight:700">{res_df.iloc[idx]['mape']:.2f}%</span>
            </div>
            </div>""")

            # ── Walk-forward ─────────────────────────────────────
            section_header("🔁", "Walk-Forward Validation", "Out-of-sample accuracy test", "#C8102E")
            with st.spinner("Running walk-forward validation..."):
                data_clean = dflagg[[TARGET_COL]+list(predvars)].iloc[max_lag:].dropna()
                # Validation window: cap at the seasonal period (or 12) so accuracy
                # stays meaningful even when the forecast horizon is very long (e.g.
                # 5 years). Holding out 60 months for testing would gut the training
                # set — instead we always validate on a sensible recent window.
                val_window = min(pred_periods, max(FC['s'], 12), len(data_clean)//3)
                val_window = max(val_window, 1)
                mf_wf = SARIMAX(data_clean[:-val_window][TARGET_COL],
                                exog=data_clean[:-val_window][list(predvars)],
                                order=sxorder, seasonal_order=sxsorder).fit(disp=False)
                yp_wf = mf_wf.forecast(steps=val_window,
                                        exog=data_clean[-val_window:][list(predvars)])
                sx_mape = np.mean(np.abs((data_clean[-val_window:][TARGET_COL].values
                                          -yp_wf.values)
                                         /data_clean[-val_window:][TARGET_COL].values))*100
                sx_rmse = np.sqrt(mean_squared_error(
                    data_clean[-val_window:][TARGET_COL].values, yp_wf.values))
                ff_wf = mf_wf.get_forecast(
                    steps=val_window,
                    exog=data_clean[-val_window:][list(predvars)]).summary_frame(alpha=0.05)
                ff_wf.index = data_clean[-val_window:].index

            _a,_b,_c = st.columns(3)
            _a.metric("MAPE",     f"{sx_mape:.2f}%",     "Walk-forward error")
            _b.metric("RMSE",     f"{sx_rmse:,.0f}",     "Root mean sq. error")
            _c.metric("Accuracy", f"{100-sx_mape:.1f}%", "1 - MAPE")

            ff_wf['bounds'] = ((ff_wf['mean_ci_upper']-ff_wf['mean_ci_lower'])
                               /ff_wf['mean']*100).round(0)
            render_table(ff_wf[['mean_ci_lower','mean','mean_ci_upper','bounds']].round(0).reset_index())

            # ── Residual diagnostics ─────────────────────────────
            section_header("🧪", "Residual Diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson", "#C8102E")
            diag_block(mf_wf.resid, "SARIMAX", model_type="SARIMAX")

            # ── Model summary ────────────────────────────────────
            with st.expander("Full Model Summary"):
                st.text(str(mf_wf.summary()))

            # ── Final forecast ───────────────────────────────────
            st.subheader("Forecast")
            with st.spinner("Generating final forecast..."):
                actual_a    = dflagg[[TARGET_COL]+list(predvars)].iloc[max_lag:]
                actual_data = actual_a[actual_a.index <= last_sales_dt].dropna()
                future_data = actual_a[actual_a.index > last_sales_dt].dropna(subset=list(predvars))
                sx_full = SARIMAX(actual_data[TARGET_COL],
                                  exog=actual_data[list(predvars)],
                                  order=sxorder, seasonal_order=sxsorder).fit(disp=0)

                # ── Hybrid future exog ───────────────────────────
                # Phase 1: use real lagged FRED exog for as many periods as available.
                # Phase 2: beyond the lag window, FRED data runs out — fill remaining
                # periods with each variable's historical mean (a neutral baseline) so
                # the exog terms contribute ~0 and SARIMAX falls back to its own
                # trend + seasonality. This lets SARIMAX reach the full horizon.
                future_real = future_data.iloc[:pred_periods]
                n_real      = len(future_real)

                # Build a full future date index for the requested horizon
                full_future_idx = pd.date_range(
                    last_sales_dt + dflagg.index.freq,
                    periods=pred_periods, freq=dflagg.index.freq)

                exog_future = pd.DataFrame(index=full_future_idx, columns=list(predvars), dtype=float)
                # Phase 1 — real lagged exog where we have it
                if n_real > 0:
                    exog_future.iloc[:n_real] = future_real[list(predvars)].values
                # Phase 2 — neutral baseline (historical mean) for the rest
                if n_real < pred_periods:
                    exog_means = actual_data[list(predvars)].mean()
                    exog_future.iloc[n_real:] = exog_means.values

                sx_fc = sx_full.get_forecast(
                    steps=pred_periods,
                    exog=exog_future[list(predvars)].values).summary_frame()
                sx_fc.index = full_future_idx

            render_table(sx_fc[['mean_ci_lower','mean','mean_ci_upper']].round(0).reset_index())
            _dl = pd.DataFrame({
                'Date'    : pd.to_datetime(sx_fc.index).strftime('%Y-%m-%d'),
                'Forecast': sx_fc['mean'].round(0).astype(int),
                'Lower_CI': sx_fc['mean_ci_lower'].round(0).astype(int),
                'Upper_CI': sx_fc['mean_ci_upper'].round(0).astype(int),
            })
            with st.popover("\u2b07 Export Forecast", use_container_width=False):
                st.download_button("Download CSV",
                    st.session_state.get('dl_sarimax_csv', b''),
                    file_name="sarimax_forecast.csv", mime="text/csv",
                    use_container_width=True, disabled=not st.session_state.get('dl_sarimax_csv'))
                st.download_button("Download Excel",
                    st.session_state.get('dl_sarimax_xlsx', b''),
                    file_name="sarimax_forecast.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True, disabled=not st.session_state.get('dl_sarimax_xlsx'))
            forecast_chart(
                actual_data[TARGET_COL].iloc[-24:].index,
                actual_data[TARGET_COL].iloc[-24:].values,
                sx_fc.index, sx_fc['mean'].values,
                sx_fc['mean_ci_lower'].values, sx_fc['mean_ci_upper'].values,
                f"SARIMAX Forecast — {pred_periods} {_unit_map[freq_override]}", '#D85A30')

            results['SARIMAX'] = {
                'mape': sx_mape, 'rmse': sx_rmse,
                'fc_idx': sx_fc.index, 'fc_mean': sx_fc['mean'].values,
                'fc_lower': sx_fc['mean_ci_lower'].values,
                'fc_upper': sx_fc['mean_ci_upper'].values,
                'tail_idx': actual_data[TARGET_COL].iloc[-24:].index,
                'tail_vals': actual_data[TARGET_COL].iloc[-24:].values,
                'label': f"SARIMAX{sxorder}x{sxsorder}",
            }
            # Cache download data in session_state
            _sx_dl = pd.DataFrame({
                'Date'    : pd.to_datetime(sx_fc.index).strftime('%Y-%m-%d'),
                'Forecast': sx_fc['mean'].round(0).astype(int),
                'Lower_CI': sx_fc['mean_ci_lower'].round(0).astype(int),
                'Upper_CI': sx_fc['mean_ci_upper'].round(0).astype(int),
            })
            st.session_state['dl_sarimax_csv']  = _sx_dl.to_csv(index=False).encode('utf-8')
            st.session_state['dl_sarimax_xlsx'] = _make_xlsx(_sx_dl)

# ═══════════════════════════════════════════════════════════
#  SARIMA TAB
# ═══════════════════════════════════════════════════════════

if run_sarima:
    with tabs[tab_idx]:
        tab_idx += 1
        section_header("📊", "SARIMA", "Pure time series · No exogenous variables · Grid search order selection", "#38a169")
        st.caption("Grid search selects the best order by AIC automatically")


        # Validation window capped so accuracy stays meaningful for long
        # horizons (the final forecast further down still uses the full
        # pred_periods on the complete series).
        _sm_win  = min(pred_periods, max(FC['s'], 12), len(sales_series)//3)
        _sm_win  = max(_sm_win, 1)
        sm_train = sales_series[:-_sm_win]
        sm_test  = sales_series[-_sm_win:]

        with st.spinner("Grid searching SARIMA orders (takes ~3 min)..."):
            best_aic, SM_ORDER, SM_S_ORDER = np.inf, (1,1,1), (0,1,1,12)
            p_r,d_r,q_r = (0,2),(1,1),(0,2)
            P_r,D_r,Q_r = (0,1),(1,1),(0,1)
            total_g = ((p_r[1]-p_r[0]+1)*(d_r[1]-d_r[0]+1)*(q_r[1]-q_r[0]+1)*
                       (P_r[1]-P_r[0]+1)*(D_r[1]-D_r[0]+1)*(Q_r[1]-Q_r[0]+1))
            done_g  = 0
            gprog   = st.progress(0, text="Grid searching...")
            for p in range(p_r[0],p_r[1]+1):
                for d in range(d_r[0],d_r[1]+1):
                    for q in range(q_r[0],q_r[1]+1):
                        for P in range(P_r[0],P_r[1]+1):
                            for D in range(D_r[0],D_r[1]+1):
                                for Q in range(Q_r[0],Q_r[1]+1):
                                    done_g += 1
                                    try:
                                        mf = SARIMAX(sm_train,order=(p,d,q),
                                                     seasonal_order=(P,D,Q,FC['s'])).fit(disp=False)
                                        if mf.aic < best_aic:
                                            best_aic,SM_ORDER,SM_S_ORDER = mf.aic,(p,d,q),(P,D,Q,12)
                                    except Exception:
                                        pass
                                    gprog.progress(done_g/total_g,
                                                   text=f"Checking ARIMA({p},{d},{q})x({P},{D},{Q},12)...")
            gprog.empty()

        st.success(f"Best order: **ARIMA{SM_ORDER}x{SM_S_ORDER}**  |  AIC: {best_aic:.2f}")

        with st.spinner("Fitting and evaluating..."):
            sm_fit  = SARIMAX(sm_train, order=SM_ORDER,
                              seasonal_order=SM_S_ORDER).fit(disp=False)
            sm_pred = sm_fit.forecast(steps=_sm_win)
            sm_mape = np.mean(np.abs((sm_test.values-sm_pred.values)/sm_test.values))*100
            sm_rmse = np.sqrt(mean_squared_error(sm_test.values, sm_pred.values))
            sm_ff   = sm_fit.get_forecast(steps=_sm_win).summary_frame(alpha=0.05)
            sm_ff.index = sm_test.index

        st.subheader("Walk-Forward Validation")
        _a,_b,_c = st.columns(3)
        _a.metric("MAPE",     f"{sm_mape:.2f}%",     "Walk-forward error")
        _b.metric("RMSE",     f"{sm_rmse:,.0f}",     "Root mean sq. error")
        _c.metric("Accuracy", f"{100-sm_mape:.1f}%", "1 - MAPE")
        sm_ff['bounds'] = ((sm_ff['mean_ci_upper']-sm_ff['mean_ci_lower'])
                           /sm_ff['mean']*100).round(0)
        render_table(sm_ff[['mean_ci_lower','mean','mean_ci_upper','bounds']].round(0).reset_index())

        section_header("🧪", "Residual Diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson", "#38a169")
        diag_block(sm_fit.resid, "SARIMA", model_type="SARIMA")

        with st.expander("Full Model Summary"):
            st.text(str(sm_fit.summary()))

        section_header("🚀", "Forecast", f"Next {pred_periods} {_unit_map[freq_override]} ahead", "#38a169")
        sm_full   = SARIMAX(sales_series, order=SM_ORDER,
                            seasonal_order=SM_S_ORDER).fit(disp=False)
        sm_fc_all = sm_full.get_forecast(steps=pred_periods+1).summary_frame(alpha=0.05)
        # Generate future dates on the dataset's OWN frequency. Hardcoding
        # month-end made weekly/quarterly/annual data forecast the wrong span
        # (e.g. 260 weeks became 260 months = 21 years).
        _sm_offset = pd.tseries.frequencies.to_offset(FC['freq'])
        sm_fc_all.index = pd.date_range(last_sales_dt + _sm_offset,
                                        periods=pred_periods+1, freq=FC['freq'])
        sm_fc = sm_fc_all[sm_fc_all.index > last_sales_dt].head(pred_periods)
        render_table(sm_fc[['mean_ci_lower','mean','mean_ci_upper']].round(0).reset_index())
        with st.popover("⬇ Export Forecast", use_container_width=False):
            st.download_button("Download CSV",
                st.session_state.get('dl_sarima_csv', b''),
                file_name="sarima_forecast.csv", mime="text/csv",
                use_container_width=True, disabled=not st.session_state.get('dl_sarima_csv'))
            st.download_button("Download Excel",
                st.session_state.get('dl_sarima_xlsx', b''),
                file_name="sarima_forecast.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, disabled=not st.session_state.get('dl_sarima_xlsx'))
        forecast_chart(
            sales_series.iloc[-24:].index, sales_series.iloc[-24:].values,
            sm_fc.index, sm_fc['mean'].values,
            sm_fc['mean_ci_lower'].values, sm_fc['mean_ci_upper'].values,
            f"SARIMA Forecast — {pred_periods} {_unit_map[freq_override]}", '#1D9E75')

        _sm_dl = pd.DataFrame({
            'Date'    : pd.to_datetime(sm_fc.index).strftime('%Y-%m-%d'),
            'Forecast': sm_fc['mean'].round(0).astype(int),
            'Lower_CI': sm_fc['mean_ci_lower'].round(0).astype(int),
            'Upper_CI': sm_fc['mean_ci_upper'].round(0).astype(int),
        })
        st.session_state['dl_sarima_csv']  = _sm_dl.to_csv(index=False).encode('utf-8')
        st.session_state['dl_sarima_xlsx'] = _make_xlsx(_sm_dl)
        results['SARIMA'] = {
            'mape': sm_mape, 'rmse': sm_rmse,
            'fc_idx': sm_fc.index, 'fc_mean': sm_fc['mean'].values,
            'fc_lower': sm_fc['mean_ci_lower'].values,
            'fc_upper': sm_fc['mean_ci_upper'].values,
            'tail_idx': sales_series.iloc[-24:].index,
            'tail_vals': sales_series.iloc[-24:].values,
            'label': f"SARIMA{SM_ORDER}x{SM_S_ORDER}",
        }

# ═══════════════════════════════════════════════════════════
#  PROPHET TAB
# ═══════════════════════════════════════════════════════════

if run_prophet:
    with tabs[tab_idx]:
        tab_idx += 1
        section_header("🔮", "Prophet", "Trend + seasonality · COVID structural break · Cross-validation", "#4299e1")
        st.caption("No FRED data used. Uses covid_shock regressor (Mar–Jun 2020) only.")


        try:
            from prophet import Prophet
            from prophet.diagnostics import cross_validation, performance_metrics
        except ImportError:
            st.error("Prophet not installed. Run: pip install prophet")
            st.stop()

        prophet_df = dfsales[[TARGET_COL]].copy()
        prophet_df.index.name = 'ds'
        prophet_df = prophet_df.reset_index()
        prophet_df.columns = ['ds','y']
        prophet_df['ds'] = pd.to_datetime(prophet_df['ds'])
        # Frequency-aware COVID flag (the global covid_months list is month-end
        # only, so it never matches weekly/quarterly/annual dates).
        prophet_df['covid_shock'] = get_covid_mask(
            pd.DatetimeIndex(prophet_df['ds']), FC['covid_rule']).astype(float)

        # Future dates on the dataset's OWN frequency (was hardcoded month-end,
        # which made weekly data forecast 260 months instead of 260 weeks).
        _pb_offset = pd.tseries.frequencies.to_offset(FC['freq'])
        future_dates = pd.date_range(last_sales_dt + _pb_offset,
                                     periods=pred_periods, freq=FC['freq'])
        future_only  = pd.DataFrame({'ds': future_dates, 'covid_shock': 0.0})

        def make_prophet(regressor=True):
            m = Prophet(growth='linear', seasonality_mode=SEASONALITY_MODE,
                        changepoint_prior_scale=cp_scale,
                        seasonality_prior_scale=s_prior,
                        yearly_seasonality=True,
                        weekly_seasonality=False,
                        daily_seasonality=False)
            if regressor:
                m.add_regressor('covid_shock')
            return m

        with st.spinner("Fitting Prophet models..."):
            m_prophet = make_prophet(True)
            m_prophet.fit(prophet_df)
            m_base = make_prophet(False)
            m_base.fit(prophet_df[['ds','y']])

        # Changepoints
        section_header("📍", "Changepoint Analysis", "Detected trend shifts with significance and direction", "#4299e1")
        deltas = m_prophet.params['delta'].mean(axis=0)
        sig_cp = np.where(np.abs(deltas) > 0.01)[0]
        st.metric("Significant changepoints", len(sig_cp),
                  delta=f"{len(m_prophet.changepoints)} total detected")

        if len(sig_cp):
            cp_data = pd.DataFrame({
                'Date':      [m_prophet.changepoints.iloc[i].date() for i in sig_cp],
                'Delta':     [f"{deltas[i]:+.4f}" for i in sig_cp],
                'Direction': ['↑ Upward' if deltas[i]>0 else '↓ Downward' for i in sig_cp]
            })
            render_table(cp_data)

        # Cross-validation
        section_header("🔬", "Cross-Validation", "Rolling window accuracy across multiple cutpoints", "#4299e1")
        _cv_ok = False
        pb_cv_mape = pb_base_mape = float('nan')
        df_perf = None
        try:
            with st.spinner("Running cross-validation (12 cutpoints × 2 models)..."):
                df_cv    = cross_validation(m_prophet, initial=FC['cv_initial'],
                                            period=FC['cv_period'], horizon=FC['cv_horizon'], parallel=None)
                df_perf  = performance_metrics(df_cv)
                pb_cv_mape = df_perf['mape'].mean()*100
                df_cv_b  = cross_validation(m_base, initial=FC['cv_initial'],
                                             period=FC['cv_period'], horizon=FC['cv_horizon'], parallel=None)
                pb_base_mape = performance_metrics(df_cv_b)['mape'].mean()*100
            _cv_ok = True
        except Exception as _cv_err:
            st.info(
                "Cross-validation needs a longer history than this dataset "
                f"provides ({len(prophet_df)} points), so it was skipped. "
                "The walk-forward validation below still runs."
            )

        if _cv_ok:
            _a,_b,_c = st.columns(3)
            _a.metric("CV MAPE (with regressor)", f"{pb_cv_mape:.2f}%",  "Rolling window accuracy")
            _b.metric("CV MAPE (baseline)",       f"{pb_base_mape:.2f}%","Without covid_shock")
            _c.metric("Regressor improvement",    f"{pb_base_mape-pb_cv_mape:+.2f}pp",
                      "✓ Helps" if pb_cv_mape<pb_base_mape else "No improvement")
            render_table(df_perf[['horizon','mape','rmse','mae']].reset_index(drop=True),
                         fmt={'mape':'{:.4f}','rmse':'{:.0f}','mae':'{:.0f}'})

        # Walk-forward
        section_header("🔁", "Walk-Forward Validation", "Out-of-sample accuracy on last periods", "#4299e1")
        # Cap validation window so accuracy stays meaningful for long horizons
        # (the final Prophet forecast further down still projects full pred_periods).
        _pb_win  = min(pred_periods, max(FC['s'], 12), len(prophet_df)//3)
        _pb_win  = max(_pb_win, 1)
        train_wf = prophet_df[:-_pb_win].copy()
        test_wf  = prophet_df[-_pb_win:].copy()
        m_wf     = make_prophet(True)
        m_wf.fit(train_wf)
        pb_fc_wf  = m_wf.predict(test_wf[['ds','covid_shock']])
        # Align forecast to test dates only (Prophet may return extra rows)
        pb_fc_wf  = pb_fc_wf[pb_fc_wf['ds'].isin(test_wf['ds'].values)].reset_index(drop=True)
        test_wf   = test_wf.reset_index(drop=True)
        pb_mape   = np.mean(np.abs((test_wf['y'].values - pb_fc_wf['yhat'].values)
                                    / test_wf['y'].values)) * 100
        pb_rmse   = np.sqrt(mean_squared_error(test_wf['y'].values, pb_fc_wf['yhat'].values))

        _a,_b,_c = st.columns(3)
        _a.metric("MAPE",     f"{pb_mape:.2f}%",     "Walk-forward error")
        _b.metric("RMSE",     f"{pb_rmse:,.0f}",     "Root mean sq. error")
        _c.metric("Accuracy", f"{100-pb_mape:.1f}%", "1 - MAPE")
        _n = min(len(test_wf), len(pb_fc_wf))
        wf_out = pd.DataFrame({
            'Date':    test_wf['ds'].iloc[:_n].dt.strftime('%Y-%m'),
            'Actual':  test_wf['y'].values[:_n].astype(int),
            'Prophet': pb_fc_wf['yhat'].values[:_n].round(0).astype(int),
            'Error%':  ((test_wf['y'].values[:_n] - pb_fc_wf['yhat'].values[:_n])
                        / test_wf['y'].values[:_n] * 100).round(2)
        })
        render_table(wf_out)

        # Residuals
        section_header("🧪", "Residual Diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson", "#4299e1")
        insample = m_prophet.predict(prophet_df[['ds','covid_shock']])
        pb_resid = prophet_df['y'].values - insample['yhat'].values
        diag_block(pb_resid, "Prophet", model_type="Prophet")

        # Components plot
        with st.expander("Trend & Seasonality Components"):
            fig2 = m_prophet.plot_components(m_prophet.predict(
                pd.concat([prophet_df[['ds','covid_shock']], future_only]).reset_index(drop=True)))
            st.pyplot(fig2, use_container_width=True)
            plt.close()

        # Forecast
        section_header("🚀", "Forecast", f"Next {pred_periods} months ahead", "#4299e1")
        m_final_pb = make_prophet(True)
        m_final_pb.fit(prophet_df)
        pb_fc = m_final_pb.predict(future_only)
        pb_fcast = pd.DataFrame({
            'Period':   pb_fc['ds'].dt.strftime(
                            '%Y-%m-%d' if freq_override == 'Weekly' else '%Y-%m'),
            'Lower':    pb_fc['yhat_lower'].round(0).astype(int),
            'Forecast': pb_fc['yhat'].round(0).astype(int),
            'Upper':    pb_fc['yhat_upper'].round(0).astype(int),
            'CI%':      ((pb_fc['yhat_upper']-pb_fc['yhat_lower'])
                         /pb_fc['yhat']*100).round(1)
        })
        render_table(pb_fcast)
        with st.popover("⬇ Export Forecast", use_container_width=False):
            st.download_button("Download CSV",
                st.session_state.get('dl_prophet_csv', b''),
                file_name="prophet_forecast.csv", mime="text/csv",
                use_container_width=True, disabled=not st.session_state.get('dl_prophet_csv'))
            st.download_button("Download Excel",
                st.session_state.get('dl_prophet_xlsx', b''),
                file_name="prophet_forecast.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, disabled=not st.session_state.get('dl_prophet_xlsx'))
        forecast_chart(
            prophet_df.set_index('ds')['y'].iloc[-24:].index,
            prophet_df.set_index('ds')['y'].iloc[-24:].values,
            pb_fc['ds'].values, pb_fc['yhat'].values,
            pb_fc['yhat_lower'].values, pb_fc['yhat_upper'].values,
            f"Prophet Forecast — {pred_periods} {_unit_map[freq_override]}", '#7B5EA7')

        _pb_dl = pd.DataFrame({
            'Date'    : pb_fc['ds'].dt.strftime('%Y-%m-%d'),
            'Forecast': pb_fc['yhat'].round(0).astype(int),
            'Lower_CI': pb_fc['yhat_lower'].round(0).astype(int),
            'Upper_CI': pb_fc['yhat_upper'].round(0).astype(int),
        })
        st.session_state['dl_prophet_csv']  = _pb_dl.to_csv(index=False).encode('utf-8')
        st.session_state['dl_prophet_xlsx'] = _make_xlsx(_pb_dl)
        results['Prophet'] = {
            'mape': pb_mape, 'rmse': pb_rmse,
            'fc_idx': pb_fc['ds'].values, 'fc_mean': pb_fc['yhat'].values,
            'fc_lower': pb_fc['yhat_lower'].values,
            'fc_upper': pb_fc['yhat_upper'].values,
            'tail_idx': prophet_df.set_index('ds')['y'].iloc[-24:].index,
            'tail_vals': prophet_df.set_index('ds')['y'].iloc[-24:].values,
            'label': f"Prophet ({SEASONALITY_MODE})",
        }

# ═══════════════════════════════════════════════════════════
#  COMPARISON TAB
# ═══════════════════════════════════════════════════════════

if sum([run_sarimax, run_sarima, run_prophet]) > 1 and results:
    with tabs[tab_idx]:
        section_header("⚖️", "Model Comparison", "Side-by-side performance · Forecast alignment · Agreement analysis", "#C8102E")

        # Metric table
        colors = {'SARIMAX':'#D85A30','SARIMA':'#1D9E75','Prophet':'#7B5EA7'}
        metric_df = pd.DataFrame({
            m: {'Walk-fwd MAPE': f"{r['mape']:.2f}%",
                'Walk-fwd RMSE': f"{r['rmse']:,.0f}",
                'Accuracy':      f"{100-r['mape']:.1f}%",
                'Uses FRED':     'Yes' if m=='SARIMAX' else 'No',
                'Exog/Regressor':('Granger sel.' if m=='SARIMAX'
                                  else 'None' if m=='SARIMA'
                                  else 'covid_shock')}
            for m, r in results.items()
        }).T
        render_table(metric_df)

        # Winner banner
        winner = min(results, key=lambda m: results[m]['mape'])
        st.success(f"**{winner}** wins with **{results[winner]['mape']:.2f}% MAPE** — lowest walk-forward error across all models")

        # Side-by-side forecast chart
        section_header("📊", "Forecast Comparison", "All models overlaid on 24-month actual history", "#C8102E")
        n_models = len(results)
        fig, axes = plt.subplots(1, n_models, figsize=(6*n_models, 4.5), sharey=True, facecolor='#FFFFFF')
        if n_models == 1:
            axes = [axes]
        for ax, (model_name, r) in zip(axes, results.items()):
            color = colors.get(model_name, '#374151')
            ax.plot(r['tail_idx'], r['tail_vals'], color='#374151', lw=2, label='Actual', zorder=3)
            ax.plot(r['fc_idx'], r['fc_mean'], color=color, lw=2.5, ls='--', label='Forecast', zorder=3)
            ax.scatter(r['fc_idx'], r['fc_mean'], color=color, s=28, zorder=4)
            ax.fill_between(r['fc_idx'], r['fc_lower'], r['fc_upper'], alpha=0.12, color=color)
            ax.axvline(r['tail_idx'][-1], color='#E8EAED', lw=1.5, ls=':', zorder=1)
            style_ax(ax, title=f"{r['label']}  ·  MAPE {r['mape']:.2f}%", ylabel='Sales')
            legend = ax.legend(frameon=True, framealpha=1, edgecolor='#E8EAED', fontsize=9)
            legend.get_frame().set_facecolor('#FFFFFF')
            for t in legend.get_texts(): t.set_color('#6B7280')
        plt.tight_layout(pad=1.5)
        st.pyplot(fig, use_container_width=True)
        plt.close()

        # Month-by-month table
        section_header("📅", "Month-by-Month Forecast", "Exact numbers from each model for reporting", "#C8102E")
        # Label format must match the data frequency — using '%Y-%m' for weekly
        # data collapses several weeks onto the same label (duplicate index).
        _lbl_fmt = '%Y-%m-%d' if freq_override == 'Weekly' else '%Y-%m'
        fc_table = {}
        for model_name, r in results.items():
            fc_table[model_name] = pd.Series(
                r['fc_mean'].round(0).astype(int),
                index=pd.to_datetime(r['fc_idx']).strftime(_lbl_fmt)
            )
        fc_df = pd.DataFrame(fc_table)
        render_table(fc_df, fmt='{:,.0f}')

        # ── Download forecast CSV ─────────────────────────────
        dl_frames = []
        for model_name, r in results.items():
            _df = pd.DataFrame({
                'Date'    : pd.to_datetime(r['fc_idx']).strftime('%Y-%m-%d'),
                'Model'   : model_name,
                'Forecast': r['fc_mean'].round(0).astype(int),
                'Lower_CI': r['fc_lower'].round(0).astype(int),
                'Upper_CI': r['fc_upper'].round(0).astype(int),
            })
            dl_frames.append(_df)
        _all_df = pd.concat(dl_frames)
        st.session_state['dl_all_csv']  = _all_df.to_csv(index=False).encode('utf-8')
        st.session_state['dl_all_xlsx'] = _make_xlsx(_all_df)
        with st.popover("⬇ Export Forecast", use_container_width=False):
            st.download_button("Download CSV",
                st.session_state.get('dl_all_csv', b''),
                file_name="coresight_forecast.csv", mime="text/csv",
                use_container_width=True, disabled=not st.session_state.get('dl_all_csv'))
            st.download_button("Download Excel",
                st.session_state.get('dl_all_xlsx', b''),
                file_name="coresight_forecast.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, disabled=not st.session_state.get('dl_all_xlsx'))

        # Agreement check
        if len(results) >= 2:
            vals = [r['fc_mean'] for r in results.values()]
            min_len = min(len(v) for v in vals)
            vals = [np.asarray(v[:min_len], dtype=float) for v in vals]
            # Measure each period's gap against that period's own level, then
            # take the worst. (Dividing every gap by the first period's value
            # distorts the result when the forecast trends up or down.)
            pct_spread = 0.0
            for i in range(len(vals)):
                for j in range(i+1, len(vals)):
                    _base = (vals[i] + vals[j]) / 2.0
                    _base = np.where(_base == 0, np.nan, _base)   # avoid /0
                    _pct  = np.abs(vals[i] - vals[j]) / _base * 100
                    if np.isfinite(_pct).any():
                        pct_spread = max(pct_spread, float(np.nanmax(_pct)))
            if pct_spread < 5:
                st.success(f"Models agree within **{pct_spread:.1f}%** — confidence is high")
            else:
                st.warning(f"Models diverge by up to **{pct_spread:.1f}%** — "
                           f"investigate structural drivers")

        st.info("""
        **How to use this comparison:**
        - If all models agree within 5% → use the midpoint as your working forecast
        - SARIMAX driven by FRED macro signals → good for scenario analysis
        - SARIMA → stable baseline, no external dependencies
        - Prophet → captures trend shifts and seasonality without macro data
        - Report the range as a band rather than a single number
        """)
# ═══════════════════════════════════════════════════════════
#  AI CHATBOT — save full tool context to session state
# ═══════════════════════════════════════════════════════════

if results:
    st.session_state['fc_results']      = results
    st.session_state['fc_sales_series'] = sales_series
    st.session_state['fc_freq']         = freq_override

    # ── Parameters ───────────────────────────────────────────
    st.session_state['fc_params'] = {
        'models_run'         : [m for m, f in [('SARIMAX', run_sarimax), ('SARIMA', run_sarima), ('Prophet', run_prophet)] if f],
        'forecast_horizon'   : pred_periods,
        'frequency'          : freq_override,
        'seasonal_period_s'  : FC['s'],
        'max_granger_lag'    : max_lag,
        'seasonality_mode'   : SEASONALITY_MODE,
        'seasonal_strength_pct': round(float(seasonal_strength), 1),
    }
    if run_sarimax:
        st.session_state['fc_params'].update({
            'sarimax_top_n_exog' : max_exog,
            'sarimax_max_combo'  : max_combo,
        })
    if run_prophet:
        st.session_state['fc_params'].update({
            'prophet_cp_prior'      : cp_scale,
            'prophet_season_prior'  : s_prior,
            'prophet_season_mode_setting': season_mode,
        })

    # ── Diagnostics (data-level) ─────────────────────────────
    adf_stat2, adf_p2, *_ = adfuller(sales_series, autolag='AIC')
    kpss_stat2, kpss_p2, *_ = kpss(sales_series, regression='c', nlags='auto')
    z_scores2  = np.abs(stats.zscore(sales_series))
    outliers2  = sales_series[z_scores2 > 3]
    monthly_avg2 = dfsales.groupby(dfsales.index.month)[TARGET_COL].mean()

    st.session_state['fc_diagnostics'] = {
        'adf_p'              : round(float(adf_p2), 4),
        'adf_stationary'     : bool(adf_p2 <= 0.05),
        'kpss_p'             : round(float(kpss_p2), 4),
        'kpss_stationary'    : bool(kpss_p2 > 0.05),
        'seasonal_strength'  : round(float(seasonal_strength), 1),
        'seasonality_mode'   : SEASONALITY_MODE,
        'peak_month'         : int(monthly_avg2.idxmax()),
        'peak_month_avg'     : round(float(monthly_avg2.max()), 0),
        'trough_month'       : int(monthly_avg2.idxmin()),
        'trough_month_avg'   : round(float(monthly_avg2.min()), 0),
        'outlier_count'      : int(len(outliers2)),
        'outlier_dates'      : [str(d.date()) for d in outliers2.index.tolist()] if len(outliers2) else [],
        'outlier_values'     : [round(float(v), 0) for v in outliers2.values.tolist()] if len(outliers2) else [],
        'total_records'      : int(len(sales_series)),
        'date_start'         : str(dfsales.index[0].date()),
        'date_end'           : str(last_sales_dt.date()),
        'mean_sales'         : round(float(sales_series.mean()), 0),
        'std_sales'          : round(float(sales_series.std()), 0),
        'min_sales'          : round(float(sales_series.min()), 0),
        'max_sales'          : round(float(sales_series.max()), 0),
        'monthly_avg_by_month': {int(k): round(float(v), 0) for k, v in monthly_avg2.items()},
    }

    # ── Model-specific extended info ─────────────────────────
    model_detail = {}

    def _safe_lb(resid):
        """Ljung-Box that adapts lags to short series; returns a 2-row-like df."""
        _r = pd.Series(resid).dropna()
        n  = len(_r)
        _la = min(10, max(1, n // 2))
        _lb = min(20, max(_la, n - 2))
        try:
            return acorr_ljungbox(_r, lags=sorted({_la, _lb}), return_df=True)
        except Exception:
            return pd.DataFrame({'lb_pvalue': [float('nan'), float('nan')]})

    if run_sarimax and 'SARIMAX' in results:
        lb_sx   = _safe_lb(mf_wf.resid)
        _, sw_sx = stats.shapiro(mf_wf.resid)
        dw_sx   = float(durbin_watson(mf_wf.resid))
        model_detail['SARIMAX'] = {
            'order'            : str(sxorder),
            'seasonal_order'   : str(sxsorder),
            'selected_variables': list(predvars),
            'combo_rank_used'  : int(idx),
            'granger_vars_found': len(grangerdf),
            'differencing'     : int(diffs),
            'ljung_box_lag10_p': round(float(lb_sx['lb_pvalue'].iloc[0]), 4),
            'ljung_box_lag20_p': round(float(lb_sx['lb_pvalue'].iloc[-1]), 4),
            'shapiro_wilk_p'   : round(float(sw_sx), 4),
            'durbin_watson'    : round(dw_sx, 4),
            'lb10_pass'        : bool(lb_sx['lb_pvalue'].iloc[0] > 0.05),
            'lb20_pass'        : bool(lb_sx['lb_pvalue'].iloc[-1] > 0.05),
            'normality_pass'   : bool(sw_sx > 0.05),
            'dw_pass'          : bool(1.5 < dw_sx < 2.5),
            'wf_actual'        : [round(float(v), 0) for v in data_clean[-pred_periods:][TARGET_COL].values],
            'wf_predicted'     : [round(float(v), 0) for v in yp_wf.values],
            'wf_dates'         : [str(d.date()) for d in data_clean[-pred_periods:].index],
        }

    if run_sarima and 'SARIMA' in results:
        lb_sm   = _safe_lb(sm_fit.resid)
        _, sw_sm = stats.shapiro(sm_fit.resid)
        dw_sm   = float(durbin_watson(sm_fit.resid))
        model_detail['SARIMA'] = {
            'order'            : str(SM_ORDER),
            'seasonal_order'   : str(SM_S_ORDER),
            'best_aic'         : round(float(best_aic), 2),
            'ljung_box_lag10_p': round(float(lb_sm['lb_pvalue'].iloc[0]), 4),
            'ljung_box_lag20_p': round(float(lb_sm['lb_pvalue'].iloc[-1]), 4),
            'shapiro_wilk_p'   : round(float(sw_sm), 4),
            'durbin_watson'    : round(dw_sm, 4),
            'lb10_pass'        : bool(lb_sm['lb_pvalue'].iloc[0] > 0.05),
            'lb20_pass'        : bool(lb_sm['lb_pvalue'].iloc[-1] > 0.05),
            'normality_pass'   : bool(sw_sm > 0.05),
            'dw_pass'          : bool(1.5 < dw_sm < 2.5),
            'wf_actual'        : [round(float(v), 0) for v in sm_test.values],
            'wf_predicted'     : [round(float(v), 0) for v in sm_pred.values],
            'wf_dates'         : [str(d.date()) for d in sm_test.index],
        }

    if run_prophet and 'Prophet' in results:
        lb_pb   = _safe_lb(pb_resid)
        _, sw_pb = stats.shapiro(pb_resid)
        dw_pb   = float(durbin_watson(pb_resid))
        cp_list = []
        if len(sig_cp):
            cp_list = [
                {'date': str(m_prophet.changepoints.iloc[i].date()),
                 'delta': round(float(deltas[i]), 4),
                 'direction': 'Upward' if deltas[i] > 0 else 'Downward'}
                for i in sig_cp
            ]
        model_detail['Prophet'] = {
            'seasonality_mode'     : SEASONALITY_MODE,
            'changepoint_prior'    : cp_scale,
            'seasonality_prior'    : s_prior,
            'significant_changepoints': len(sig_cp),
            'total_changepoints'   : len(m_prophet.changepoints),
            'changepoint_details'  : cp_list,
            'cv_mape_with_regressor': (None if np.isnan(pb_cv_mape) else round(float(pb_cv_mape), 2)),
            'cv_mape_baseline'     : (None if np.isnan(pb_base_mape) else round(float(pb_base_mape), 2)),
            'regressor_improvement_pp': (None if (np.isnan(pb_cv_mape) or np.isnan(pb_base_mape))
                                          else round(float(pb_base_mape - pb_cv_mape), 2)),
            'ljung_box_lag10_p'    : round(float(lb_pb['lb_pvalue'].iloc[0]), 4),
            'ljung_box_lag20_p'    : round(float(lb_pb['lb_pvalue'].iloc[-1]), 4),
            'shapiro_wilk_p'       : round(float(sw_pb), 4),
            'durbin_watson'        : round(dw_pb, 4),
            'lb10_pass'            : bool(lb_pb['lb_pvalue'].iloc[0] > 0.05),
            'lb20_pass'            : bool(lb_pb['lb_pvalue'].iloc[-1] > 0.05),
            'normality_pass'       : bool(sw_pb > 0.05),
            'dw_pass'              : bool(1.5 < dw_pb < 2.5),
            'wf_actual'            : [round(float(v), 0) for v in test_wf['y'].values[:_n]],
            'wf_predicted'         : [round(float(v), 0) for v in pb_fc_wf['yhat'].values[:_n]],
            'wf_dates'             : list(test_wf['ds'].iloc[:_n].dt.strftime('%Y-%m')),
            'wf_error_pct'         : [round(float(e), 2) for e in
                                       ((test_wf['y'].values[:_n] - pb_fc_wf['yhat'].values[:_n])
                                        / test_wf['y'].values[:_n] * 100)],
        }

    st.session_state['fc_model_detail'] = model_detail

    # ── Save results to JSON for standalone dashboard ────────
    import json as _json, os as _os
    _out = {
        'params'      : st.session_state.get('fc_params', {}),
        'diagnostics' : st.session_state.get('fc_diagnostics', {}),
        'model_detail': model_detail,
        'results'     : {
            mn: {
                'mape'    : round(float(r['mape']), 4),
                'rmse'    : round(float(r['rmse']), 2),
                'fc_idx'  : [str(d)[:10] for d in r['fc_idx']],
                'fc_mean' : [round(float(v), 2) for v in r['fc_mean']],
                'fc_lower': [round(float(v), 2) for v in r['fc_lower']],
                'fc_upper': [round(float(v), 2) for v in r['fc_upper']],
            }
            for mn, r in results.items()
        },
        'sales_history': {
            'dates' : [str(d.date()) for d in sales_series.index],
            'values': [round(float(v), 2) for v in sales_series.values],
        }
    }
    _results_path = _os.path.join(_os.path.dirname(__file__), 'results.json')
    with open(_results_path, 'w') as _f:
        _json.dump(_out, _f)

from chatbot import render_chatbot
render_chatbot()
