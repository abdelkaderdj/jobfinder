import os
import json
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import smtplib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from email.message import EmailMessage

from groq import Groq


# ============================================================
# SETTINGS
# ============================================================

# Portable:
# Works on Windows now and later on Linux/cloud
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

JOBS_FILE = os.path.join(
    PROJECT_DIR,
    "jobs.jsonl"
)

RESULTS_FILE = os.path.join(
    PROJECT_DIR,
    "auto_results.jsonl"
)

SENT_FILE = os.path.join(
    PROJECT_DIR,
    "sent_applications.jsonl"
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")

# Gmail SMTP STARTTLS
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

if not GROQ_API_KEY and not GEMINI_API_KEY and not OPENROUTER_API_KEY:
    raise RuntimeError(
        "At least one AI provider key is required: GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY"
    )

if not GMAIL_APP_PASSWORD:
    raise RuntimeError("GMAIL_APP_PASSWORD is not set")

if not GMAIL_ADDRESS:
    raise RuntimeError("GMAIL_ADDRESS is not set")


# Groq is an optional fallback provider. A valid Gemini-only configuration must
# still be able to import this module and analyze jobs when Groq is unavailable.
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Optional callbacks wired by the cloud runner.
JOB_EVENT_CALLBACK = None
JOB_PRE_SEND_GUARD = None
JOB_AI_RATE_LIMIT_CALLBACK = None
JOB_AI_PROVIDER_STATUS_CALLBACK = None
JOB_DECISION_AUDIT_CALLBACK = None


def set_job_ai_rate_limit_callback(callback):
    """Register a cloud-runner callback to persist provider cooldowns securely."""
    global JOB_AI_RATE_LIMIT_CALLBACK
    JOB_AI_RATE_LIMIT_CALLBACK = callback


def set_job_ai_provider_status_callback(callback):
    """Register a callback for non-sensitive provider health/route status."""
    global JOB_AI_PROVIDER_STATUS_CALLBACK
    JOB_AI_PROVIDER_STATUS_CALLBACK = callback


def set_job_event_callback(callback):
    global JOB_EVENT_CALLBACK
    JOB_EVENT_CALLBACK = callback


def set_job_decision_audit_callback(callback):
    """Register the encrypted per-offer audit checkpoint callback."""
    global JOB_DECISION_AUDIT_CALLBACK
    JOB_DECISION_AUDIT_CALLBACK = callback


def set_job_pre_send_guard(callback):
    """Install a fail-closed guard against jobs the owner has manually rejected."""
    global JOB_PRE_SEND_GUARD
    JOB_PRE_SEND_GUARD = callback


# ============================================================
# REAL CANDIDATE
# ============================================================

CANDIDATE = os.getenv("JOBFINDER_CANDIDATE_PROFILE", "")
CANDIDATE_NAME = os.getenv("JOBFINDER_CANDIDATE_NAME", "")

if not CANDIDATE.strip() or not CANDIDATE_NAME.strip():
    raise RuntimeError("Encrypted candidate profile was not loaded")



# ============================================================
# VERIFIED FACT LIBRARY
#
# AI NEVER WRITES THESE SENTENCES.
# AI ONLY SELECTS FACT IDs.
# ============================================================

FACTS = {

    "EDUCATION_MECHANICAL_ENERGY": {
        "evidence_type": "education",

        "fr": (
            "J’ai une Licence en Génie Mécanique "
            "et Énergétique"
        ),

        "en": (
            "I have a Bachelor’s degree in Mechanical "
            "and Energy Engineering"
        )
    },

    "KNOWLEDGE_MECHANICAL_SYSTEMS": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en systèmes mécaniques"
        ),

        "en": (
            "I have knowledge of mechanical systems"
        )
    },

    "KNOWLEDGE_INDUSTRIAL_MAINTENANCE": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en maintenance industrielle"
        ),

        "en": (
            "I have knowledge of industrial maintenance"
        )
    },

    "KNOWLEDGE_MECHANICAL_EQUIPMENT": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances sur les équipements mécaniques"
        ),

        "en": (
            "I have knowledge of mechanical equipment"
        )
    },

    "KNOWLEDGE_CENTRIFUGAL_PUMPS": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances sur les pompes centrifuges"
        ),

        "en": (
            "I have knowledge of centrifugal pumps"
        )
    },

    "KNOWLEDGE_FLUID_SYSTEMS": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en systèmes fluidiques"
        ),

        "en": (
            "I have knowledge of fluid systems"
        )
    },

    "KNOWLEDGE_TECHNICAL_DRAWINGS": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en interprétation "
            "de dessins techniques"
        ),

        "en": (
            "I have knowledge of technical drawings"
        )
    },

    "KNOWLEDGE_TROUBLESHOOTING": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances de base en diagnostic "
            "de problèmes mécaniques"
        ),

        "en": (
            "I have basic knowledge of mechanical troubleshooting"
        )
    },

    "KNOWLEDGE_TECHNICAL_DOCUMENTATION": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en documentation technique"
        ),

        "en": (
            "I have knowledge of technical documentation"
        )
    },

    "TRAINING_SOLIDWORKS": {
        "evidence_type": "training",

        "fr": (
            "J’ai suivi une formation en conception mécanique "
            "avec SolidWorks"
        ),

        "en": (
            "I completed training in mechanical design "
            "with SolidWorks"
        )
    },

    "KNOWLEDGE_SOLIDWORKS": {
        "evidence_type": "knowledge",

        "fr": (
            "J’ai des connaissances en SolidWorks"
        ),

        "en": (
            "I have knowledge of SolidWorks"
        )
    }
}


VALID_FACT_IDS = set(
    FACTS.keys()
)


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(text):

    text = str(
        text or ""
    ).lower()

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = "".join(
        ch
        for ch in text
        if not unicodedata.combining(ch)
    )

    text = text.replace(
        "’",
        "'"
    )

    text = re.sub(
        r"[^a-z0-9\u0600-\u06ff]+",
        " ",
        text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


UNRELATED_ROLE_TITLE_PATTERNS = (
    r"\bcommercial\b", r"\btechnico commercial\b", r"\bsales?\b",
    r"\bvente\b", r"\bmarketing\b", r"\bbusiness developer\b",
    r"\breceptionniste\b", r"\breceptionist\b",
    r"\bassistant(e)? de direction\b", r"\bassistant e de direction\b",
    r"\bsecretaire\b", r"\bagent de securite\b", r"\bsecurity guard\b",
    r"\bsecurity officer\b", r"\bgardien\b",
    r"\btoxicologie\b", r"\btoxicologue\b", r"\btoxicology\b",
    r"\btoxicologist\b", r"\blaborantin\b",
    r"\btechnicien de laboratoire\b", r"\blaboratory technician\b",
    r"\btechnologist\b.*\blaboratory\b", r"\blaboratory\b.*\btechnologist\b",
    r"\btechnologue\b.*\blaboratoire\b", r"\blaboratoire\b.*\btechnologue\b",
    r"\bbiologiste\b", r"\bmicrobiologiste\b", r"\bbiologist\b",
    r"\bmicrobiologist\b", r"\bchimiste\b", r"\bchemist\b",
    r"\bpharmacien\b", r"\binfirmier\b", r"\bnurse\b",
    r"\bcomptable\b", r"\baccountant\b", r"\bressources humaines\b",
    r"\bhuman resources\b", r"\bhr officer\b", r"\bassistant administratif\b",
    r"\bserveur\b", r"\bcuisinier\b", r"\bchef de cuisine\b",
    r"\b(electrician|electricien\w*|electricite|electrical|electrique)\b",
    r"\b(electromechanical|electromec\w*)\b",
    r"\b(instrumentation|instrumentiste|automation|automatisation|automatisme)\b",
    r"\bgestionnaire des stocks\b", r"\bstock manager\b",
    r"\binventory\b", r"\bwarehouse\b", r"\bstorekeeper\b",
    r"\blogistics\b", r"\blogistique\b", r"\bsupply chain\b",
    r"\baircraft\b", r"\baviation\b", r"\bairframe\b",
    r"\bcivil engineer\b", r"\bgenie civil\b",
    r"\bhse engineer\b", r"\bingenieur hse\b",
    r"\bتجاري\b", r"\bمبيعات\b", r"\bتسويق\b", r"\bاستقبال\b",
    r"\bحارس\b", r"\bامن\b", r"\bأمن\b", r"\bسموم\b",
    r"\bمختبر\b", r"\bمخبر\b", r"\bبيولوج\b", r"\bميكروبيولوج\b",
    r"\bكيميائي\b", r"\bمحاسب\b", r"\bموارد بشرية\b",
    r"\bسكرتير\b", r"\bسكرتيرة\b", r"\bكهربائي\b", r"\bكهرباء\b",
    r"\bكهروميكانيك\b", r"\bكهروميكانيكي\b", r"\bمخازن\b",
    r"\bلوجستيك\b", r"\bمخزون\b", r"\bطيران\b", r"\bصيانة طائرات\b",
)

# The candidate holds a Licence/Bac+3, not an engineering diploma.
ENGINEERING_GRADE_ROLE_TITLE_PATTERNS = (
    r"\bingenieur\b",
    r"\bengineer\b",
    r"\bingenieur d etat\b",
    r"\bمهندس\b",
)

STRONG_RELEVANT_ROLE_PATTERNS = (
    r"\bmechanic\w*\b", r"\bmechanical\b", r"\bmecanicien\w*\b",
    r"\bmecanique\b", r"\bmaintenance\b", r"\bmaintenancier\w*\b",
    r"\bfroid\b", r"\bfrigoriste\w*\b", r"\brefrigeration\b", r"\brefroidissement\b",
    r"\bthermique\b", r"\btransfert de chaleur\b", r"\bheat transfer\b",
    r"\bthermodynamique\b", r"\bthermal systems\b", r"\bcooling\b", r"\bhvac\b", r"\bclimatisation\b",
    r"\bpump\w*\b", r"\bpomp\w*\b", r"\bcompressor\w*\b", r"\bcompresseur\w*\b",
    r"\bturbine\b", r"\brotating equipment\b", r"\bequipment maintenance\b",
    r"\bequipement mecanique\b", r"\bequipements mecaniques\b",
    r"\bsolidworks\b", r"\bconception mecanique\b", r"\bmechanical design\b",
    r"\bdessinateur industriel\b", r"\bprojeteur mecanique\b",
    r"\bhydraulic systems\b", r"\bsystemes fluidiques\b",
    r"\bميكانيك\b", r"\bصيانة ميكانيكية\b", r"\bمضخات\b",
    r"\bمعدات ميكانيكية\b", r"\bتصميم ميكانيكي\b",
    r"\bالصيانة الصناعية\b", r"\bصيانة صناعية\b",
    r"\bصيانة المعدات الصناعية\b", r"\bصيانة معدات صناعية\b",
)

GENERIC_TECHNICAL_ROLE_PATTERNS = (
    r"\btechnicien\w*\b", r"\btechnician\w*\b",
    r"\btechnical\b", r"\btechnique\w*\b",
    r"\boperateur\w*\b", r"\boperator\w*\b",
    r"\bتقني\w*\b", r"\bفني\w*\b", r"\bمشغل\b",
)

CV_MATCH_SKILL_GROUPS = (
    (
        "industrial_maintenance",
        15,
        (
            r"\bmaintenance\b", r"\bmainten\w*\b", r"\bpreventive maintenance\b",
            r"\bcorrective maintenance\b", r"\bصيانة\b", r"\bالصيانة\b",
            r"\bالصيانة الصناعية\b", r"\bصيانة صناعية\b",
            r"\bصيانة المعدات الصناعية\b", r"\bصيانة معدات صناعية\b",
            r"\bdiagnostic\b", r"\bdepannage\b",
        ),
    ),
    (
        "mechanical_systems_equipment",
        12,
        (
            r"\bmechanical system\w*\b", r"\bsystemes mecaniques\b",
            r"\bfroid\b", r"\bfrigoriste\w*\b", r"\brefrigeration\b", r"\brefroidissement\b",
            r"\bthermique\b", r"\btransfert de chaleur\b", r"\bheat transfer\b",
            r"\bthermodynamique\b", r"\bthermal systems\b", r"\bcooling\b", r"\bhvac\b", r"\bclimatisation\b",
            r"\bequipement\w*\b", r"\bmechanical equipment\b",
            r"\brotating equipment\b", r"\bequipements mecaniques\b",
            r"\bمعدات ميكانيكية\b", r"\bأنظمة ميكانيكية\b",
        ),
    ),
    (
        "pumps_fluids_hydraulics",
        15,
        (
            r"\bpump\w*\b", r"\bpomp\w*\b", r"\bcentrifug\w*\b",
            r"\bfluid\w*\b", r"\bhydraulic\w*\b", r"\bhydraulique\b",
            r"\bcompresseur\w*\b", r"\bcompressor\w*\b",
            r"\btuyauterie\b", r"\bpiping\b", r"\bمضخ\w*\b",
            r"\bأنظمة الموائع\b", r"\bالهيدروليك\b",
        ),
    ),
    (
        "solidworks_cad_design",
        15,
        (
            r"\bsolidworks\b", r"\bcad\b", r"\bconception mecanique\b",
            r"\bmechanical design\b", r"\bmodelisation\b",
            r"\bassemblage\w*\b", r"\bassembly\b", r"\bassemblies\b",
            r"\bdessinateur\b", r"\bprojeteur\b", r"\bdesign industriel\b",
            r"\bتصميم ميكانيكي\b", r"\bسوليدووركس\b",
        ),
    ),
    (
        "technical_drawings_documentation",
        10,
        (
            r"\btechnical drawing\w*\b", r"\bdessin\w* technique\w*\b",
            r"\bmise en plan\b", r"\bplan technique\w*\b",
            r"\btechnical documentation\b", r"\bdocumentation technique\b",
            r"\bdrawing interpretation\b", r"\blecture de plan\w*\b",
            r"\bقراءة المخططات\b", r"\bالتوثيق الفني\b",
        ),
    ),
    (
        "industrial_production_process",
        10,
        (
            r"\bindustr\w*\b", r"\bproduction\b", r"\bmanufactur\w*\b",
            r"\bfabrication\b", r"\bprocess\w*\b", r"\busine\b",
            r"\batelier\b", r"\bmachine\b", r"\bcnc\b", r"\bassembly line\b",
            r"\bإنتاج\b", r"\bانتاج\b", r"\bصناعة\b", r"\bمصنع\b",
            r"\bمعدات صناعية\b",
        ),
    ),
    (
        "troubleshooting_reliability",
        8,
        (
            r"\btroubleshoot\w*\b", r"\bdiagnostic mecanique\b",
            r"\breliability\b", r"\bfiabilite\b", r"\bdepan\w*\b",
            r"\brepair\b", r"\bmaintenance corrective\b",
            r"\bاستكشاف الاعطال\b", r"\bتشخيص الاعطال\b",
        ),
    ),
    (
        "energy",
        5,
        (
            r"\benergy\b", r"\benergie\b", r"\benergetique\b",
            r"\bpower plant\b", r"\bcentrale\b", r"\bطاقة\b",
        ),
    ),
)


def calculate_candidate_cv_match(job, advertisement=""):
    """
    Explainable overlap against this specific position, not the whole Telegram post.

    The caller must pass only the position-specific block. Explicitly excluded
    disciplines and Ingénieur/Engineer titles cannot become a match due to
    coincidental words in a combined advertisement.
    """
    job = job if isinstance(job, dict) else {}
    title = str(
        job.get("job_title")
        or job.get("application_title_fr")
        or job.get("application_title_en")
        or ""
    ).strip()
    normalized_title = normalize_text(title)

    if not normalized_title:
        return 0, []

    if any(re.search(pattern, normalized_title) for pattern in UNRELATED_ROLE_TITLE_PATTERNS):
        return 0, []

    if (
        any(re.search(pattern, normalized_title) for pattern in ENGINEERING_GRADE_ROLE_TITLE_PATTERNS)
        and not _advertisement_explicitly_accepts_licence_bac3(advertisement)
    ):
        return 0, []

    # A purely industrial word is not enough: title alignment must begin with
    # mechanics, maintenance, pumps/fluids, or mechanical design.
    title_direct = (
        r"\bmechanic\w*\b", r"\bmechanical\b", r"\bmecanicien\w*\b",
        r"\bfroid\b", r"\bfrigoriste\w*\b", r"\brefrigeration\b", r"\brefroidissement\b",
        r"\bthermique\b", r"\btransfert de chaleur\b", r"\bheat transfer\b",
        r"\bthermodynamique\b", r"\bthermal systems\b", r"\bcooling\b", r"\bhvac\b", r"\bclimatisation\b",
        r"\bmecanique\b", r"\bmaintenance\b", r"\bmaintenancier\w*\b",
        r"\bpump\w*\b", r"\bpomp\w*\b", r"\bcompressor\w*\b",
        r"\bcompresseur\w*\b", r"\bsolidworks\b", r"\bcad\b",
        r"\bconception mecanique\b", r"\bmechanical design\b",
        r"\bdessinateur industriel\b", r"\bprojeteur mecanique\b",
        r"\bhydraulic systems\b", r"\bsystemes fluidiques\b",
        r"\btechnicien hydraulique\b", r"\btechnicien des pompes\b",
        r"\bميكانيك\b", r"\bصيانة ميكانيكية\b", r"\bمضخات\b",
        r"\bمعدات ميكانيكية\b", r"\bتصميم ميكانيكي\b",
        r"\bالصيانة الصناعية\b", r"\bصيانة صناعية\b",
        r"\bصيانة المعدات الصناعية\b", r"\bصيانة معدات صناعية\b",
    )
    title_adjacent = (
        r"\btechnicien\w*\b", r"\btechnician\w*\b",
        r"\boperator\b", r"\boperateur\b",
        r"\boperateur de ligne\b", r"\bproduction operator\b",
        r"\bمشغل\b", r"\bتقني ميكانيك\b", r"\bتقني صيانة\b",
    )

    if any(re.search(pattern, normalized_title) for pattern in title_direct):
        score = 30
    elif any(re.search(pattern, normalized_title) for pattern in title_adjacent):
        score = 20
    else:
        score = 0

    # Only job-specific structured fields and the caller-provided role block
    # may contribute. Never pass the entire multi-position Telegram post here.
    text_parts = [title]
    for field in (
        "job_description", "job_summary", "job_specific_requirements",
        "responsibilities", "duties", "requirements", "qualifications", "skills",
    ):
        value = job.get(field)
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, (list, tuple)):
            text_parts.extend(str(item) for item in value if isinstance(item, str))

    if isinstance(advertisement, str) and advertisement.strip():
        text_parts.append(advertisement)

    normalized = normalize_text(" ".join(text_parts))
    matched_groups = []
    for group_name, points, patterns in CV_MATCH_SKILL_GROUPS:
        if any(re.search(pattern, normalized) for pattern in patterns):
            matched_groups.append(group_name)
            score += points

    return min(100, score), matched_groups

def _is_senior_role_title(normalized_title: str) -> bool:
    """Titles that are plainly above the candidate's junior/entry-level scope."""
    senior_markers = (
        "senior", "manager", "management", "team leader", "leader",
        "supervisor", "superviseur", "foreman", "chef d atelier",
        "chef atelier", "chef d equipe", "head of", "director",
        "directeur", "chief", "responsable", "مدير", "رئيس", "مشرف",
    )
    normalized = normalize_text(normalized_title)
    return any(marker in normalized for marker in senior_markers)



def _is_explicitly_not_required(text: str) -> bool:
    """Detect negated/optional qualification language after text normalization."""
    normalized = normalize_text(str(text or ""))
    return bool(re.search(
        r"(?:\bnot required\b|\bnot mandatory\b|\bnot necessary\b|"
        r"\bnot needed\b|\bnot a requirement\b|\bis not required\b|"
        r"\bis not mandatory\b|\bpas requis(?:e)?\b|\bnon requis(?:e)?\b|"
        r"\bpas exige(?:e)?\b|\bnon exige(?:e)?\b|"
        r"\bpas obligatoire\b|\bnon obligatoire\b|"
        r"\bpas necessaire\b|\bnon necessaire\b|"
        r"\bn est pas requis(?:e)?\b|\bn est pas exige(?:e)?\b|"
        r"\bغير مطلوب(?:ة)?\b|\bليس مطلوب(?:ا|ة)?\b|"
        r"\bلا يشترط\b|\bغير إجباري\b|\bليس إلزامي(?:ا|ة)?\b|"
        r"\bلا يلزم\b)",
        normalized,
        flags=re.IGNORECASE,
    ))


def _advertisement_explicitly_accepts_licence_bac3(advertisement: str) -> bool:
    """True only when the position-specific ad explicitly accepts/requires Licence/Bac+3."""
    text = normalize_text(str(advertisement or ""))
    degree_pattern = re.compile(
        r"(?:\blicence\b|\bbachelor(?: degree)?\b|"
        r"\bbac ?\+? ?3\b|\bbac3\b|"
        r"شهادة ليسانس|ليسانس|شهادة البكالوريوس|بكالوريوس)",
        flags=re.IGNORECASE,
    )
    acceptance_pattern = re.compile(
        r"(?:\baccepted\b|\baccept(?:ed|able)\b|\baccepte(?:e|es|s)?\b|"
        r"\badmissible\b|\beligible\b|\brequired\b|\brequis(?:e)?\b|"
        r"\bexige(?:e)?\b|\bobligatoire\b|\badmis(?:e)?\b|"
        r"\bequivalent\b|\bminimum\b|\bsuffisant(?:e)?\b|"
        r"مقبول(?:ة)?|مطلوب(?:ة)?|مؤهل|معادل|لازم)",
        flags=re.IGNORECASE,
    )

    # A generic mention such as "Diplôme Licence/Bac+3 en mécanique" is not
    # proof that an Ingénieur-grade role accepts this qualification. Require
    # an explicit positive requirement/acceptance statement near the degree.
    explicitly_accepted = False
    for degree in degree_pattern.finditer(text):
        window_start = max(0, degree.start() - 45)
        window_end = min(len(text), degree.end() + 55)
        window = text[window_start:window_end]
        for marker in acceptance_pattern.finditer(window):
            marker_prefix = window[max(0, marker.start() - 25):marker.start()]
            if re.search(
                r"(?:\bnot|\bpas|\bnon|\bn est pas|\bis not|"
                r"\bisnt|\bne pas|\bغير|\bليس|\bلا)\s*$",
                marker_prefix,
                flags=re.IGNORECASE,
            ):
                continue
            explicitly_accepted = True
            break
        if explicitly_accepted:
            break

    holder_patterns = (
        r"\bholders? of (?:a )?(?:licence|bachelor)(?: degree)?\b",
        r"\btitulaires? d une licence\b",
        r"حاملو شهادة ليسانس|حاملي شهادة ليسانس|حامل شهادة ليسانس",
    )
    explicitly_accepted = explicitly_accepted or any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in holder_patterns
    )
    if not explicitly_accepted:
        return False

    # A mandatory engineering diploma / Bac+5 still wins over Licence/Bac+3.
    # Negated requirements such as "Bac+5 not required" are not mandatory.
    mandatory_higher_degree = re.search(
        r"(diplome d ingenieur|diplome ingenieur|ingenieur d etat|"
        r"master(?:s)? degree|master 2|bac ?5|doctorat|phd|"
        r"شهادة مهندس|مهندس دولة|ماستر|باك ?5).{0,45}"
        r"(required|mandatory|must have|must hold|obligatoire|requis|requise|"
        r"exige|indispensable|يشترط|إجباري|اجباري|ضروري)"
        r"|"
        r"(required|mandatory|must have|must hold|obligatoire|requis|requise|"
        r"exige|indispensable|يشترط|إجباري|اجباري|ضروري).{0,45}"
        r"(diplome d ingenieur|diplome ingenieur|ingenieur d etat|"
        r"master(?:s)? degree|master 2|bac ?5|doctorat|phd|"
        r"شهادة مهندس|مهندس دولة|ماستر|باك ?5)",
        text,
        flags=re.IGNORECASE,
    )
    if mandatory_higher_degree is None:
        return True
    window = text[max(0, mandatory_higher_degree.start() - 35):
                  min(len(text), mandatory_higher_degree.end() + 45)]
    return _is_explicitly_not_required(window)


def _advertisement_has_experience_upper_bound(advertisement: str) -> bool:
    """True when a quantified experience value is phrased as a maximum, not a minimum."""
    raw = str(advertisement or "").lower()
    digit_map = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    raw = raw.translate(digit_map)
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("’", "'").replace("–", "-").replace("—", "-")
    raw = re.sub(r"\s+", " ", raw).strip()
    experience = re.compile(
        r"(?:experience|experiences|experience professionnelle|professional experience|"
        r"work experience|years of experience|experience in the field|exp[eé]rience|"
        r"خبرة|الخبرة|سنوات الخبرة)",
        flags=re.IGNORECASE,
    )
    quantified = re.compile(
        r"\d+(?:[.,]\d+)?\s*(?:years?|yrs?|ans?|annees|months?|mois|سنوات|سنة|اشهر|شهر)",
        flags=re.IGNORECASE,
    )
    cap = re.compile(
        r"(?:\bup to\b|\bat most\b|\bno more than\b|\bnot more than\b|"
        r"\bmaximum(?: of)?\b|\bmax\.?\b|\bau maximum\b|\bau plus\b|"
        r"\bmaximal(?:e)?(?: de)?\b|\bor less\b|\bor fewer\b|"
        r"كحد اقصى|كحد أقصى|بحد اقصى|بحد أقصى|على الاكثر|على الأكثر|"
        r"لا يزيد عن|لا تتجاوز|اقصى حد|أقصى حد)",
        flags=re.IGNORECASE,
    )
    for marker in cap.finditer(raw):
        context = raw[max(0, marker.start() - 90):min(len(raw), marker.end() + 90)]
        if experience.search(context) and quantified.search(context):
            return True
    return False


def _experience_requirement_years(advertisement: str):
    """
    Extract the stated minimum experience threshold for this position.
    For "2 to 5 years", use 2 as the stated lower/minimum endpoint.
    Convert months to a fraction of a year; return None if not tied to experience.
    """
    raw = str(advertisement or "").lower()
    digit_map = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    raw = raw.translate(digit_map)
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("’", "'").replace("–", "-").replace("—", "-")
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None

    experience = (
        r"(?:experience|experiences|experience professionnelle|professional experience|"
        r"work experience|years of experience|experience in the field|"
        r"exp[eé]rience|خبرة|الخبرة|سنوات الخبرة)"
    )
    unit = r"(?:years?|yrs?|ans?|annees|months?|mois|سنوات|سنة|اشهر|شهر)"
    number = r"\d+(?:[.,]\d+)?"
    candidates = []

    range_pattern = re.compile(
        rf"(?P<low>{number})\s*(?:to|a|à|-)\s*(?P<high>{number})\s*(?P<unit>{unit})",
        flags=re.IGNORECASE,
    )
    for match in range_pattern.finditer(raw):
        before = raw[max(0, match.start() - 65):match.start()]
        after = raw[match.end():min(len(raw), match.end() + 55)]
        if re.search(experience, before, re.IGNORECASE) or re.search(experience, after, re.IGNORECASE):
            try:
                value = float(match.group("low").replace(",", "."))
            except (TypeError, ValueError):
                continue
            if match.group("unit").lower() in {"month", "months", "mois", "شهر", "اشهر"}:
                value /= 12.0
            candidates.append((match.start(), match.end(), value))

    single_pattern = re.compile(
        rf"(?P<n>{number})\s*(?P<unit>{unit})",
        flags=re.IGNORECASE,
    )
    max_only_markers = re.compile(
        r"(?:\bup to\b|\bat most\b|\bno more than\b|\bnot more than\b|"
        r"\bmaximum(?: of)?\b|\bmax\.?\b|\bau maximum\b|\bau plus\b|"
        r"\bmaximal(?:e)?(?: de)?\b|\bor less\b|\bor fewer\b|"
        r"كحد اقصى|كحد أقصى|بحد اقصى|بحد أقصى|على الاكثر|على الأكثر|لا يزيد عن|لا تتجاوز|اقصى حد|أقصى حد)",
        flags=re.IGNORECASE,
    )

    for match in single_pattern.finditer(raw):
        if any(start <= match.start() < end for start, end, _value in candidates):
            continue
        before = raw[max(0, match.start() - 65):match.start()]
        after = raw[match.end():min(len(raw), match.end() + 55)]
        if not (
            re.search(experience, before, re.IGNORECASE)
            or re.search(experience, after, re.IGNORECASE)
        ):
            continue
        # Upper-bound phrasing is not a minimum experience threshold.
        if max_only_markers.search(before[-45:]) or max_only_markers.match(after.lstrip()[:45]):
            continue
        try:
            value = float(match.group("n").replace(",", "."))
        except (TypeError, ValueError):
            continue
        if match.group("unit").lower() in {"month", "months", "mois", "شهر", "اشهر"}:
            value /= 12.0
        candidates.append((match.start(), match.end(), value))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return max(0.0, candidates[0][2])


def _experience_requirement_policy(advertisement: str):
    """
    <=3 years: experience is not a rejection reason; 3..4 years: REVIEW;
    >4 years: REJECT. This affects only experience, not other eligibility rules.
    """
    years = _experience_requirement_years(advertisement)
    if years is None:
        return None, None
    if years <= 3:
        return years, "waive_experience"
    if years <= 4:
        return years, "manual_review"
    return years, "reject"


def _reason_is_experience_only(reason: str) -> bool:
    """Return true only if the AI's explanation contains no independent mismatch."""
    text = normalize_text(str(reason or ""))
    if not text or not re.search(
        r"(experience|experienced|years?|yrs?|ans?|annees|months?|mois|"
        r"experience professionnelle|work experience|خبرة|سنوات|سنة|اشهر|شهر)",
        text,
    ):
        return False
    unrelated_reasons = (
        "degree", "diploma", "diplome", "master", "bachelor", "licence",
        "ingenieur", "engineer", "certification", "certificate", "license",
        "training", "trained", "formation", "internship", "stage professionnel",
        "skill gap", "missing skills", "competency", "specialization", "speciality",
        "discipline", "electrical", "electricity",
        "electromechanical", "civil engineering", "hse", "aviation", "aircraft",
        "laboratory", "logistics", "inventory", "location", "residency", "residence",
        "housing", "driving licence", "wrong field", "not related", "unrelated",
        "does not match", "تخصص مختلف", "شهادة مهندس", "كهرباء", "كهروميكانيك",
        "هندسة مدنية", "مخبر", "مخازن",
    )
    return not any(marker in text for marker in unrelated_reasons)


def _reason_explicitly_says_candidate_lacks_experience(reason: str) -> bool:
    """Separate an actual lack of experience from uncertain/unconfirmed experience."""
    text = normalize_text(str(reason or ""))
    lack_markers = (
        "lacks experience", "lacks professional experience", "lack of experience",
        "no experience", "no professional experience", "without experience",
        "without professional experience", "has no experience", "has no professional experience",
        "does not have experience", "does not have professional experience",
        "candidate is inexperienced", "candidate has little experience",
        "لا يملك خبرة", "لا يمتلك خبرة", "ليس لديه خبرة", "ليست لديه خبرة",
        "عدم امتلاك خبرة", "بدون خبرة", "دون خبرة",
    )
    return any(marker in text for marker in lack_markers)


def _advertisement_explicitly_requires_missing_qualification(job, advertisement: str) -> bool:
    """
    Verify a missing qualification only against this position's own ad block.
    """
    ad = normalize_text(str(advertisement or ""))
    if not ad:
        return False

    required_years, experience_policy = _experience_requirement_policy(ad)
    if required_years is not None and experience_policy == "reject":
        return True

    no_experience_required = re.search(
        r"(no experience (?:is )?required|no prior experience|no previous experience|"
        r"experience not required|without experience|without prior experience|"
        r"no experience needed|no experience necessary|entry level no experience|"
        r"sans experience|aucune experience exigee|experience non exigee|"
        r"experience professionnelle non exigee|debutant accepte|debutante acceptee|"
        r"debutants acceptes|open to beginners|no work experience required|"
        r"experience is optional|experience is a plus|experience is preferred|"
        r"experience souhaitee|experience appreciee|experience non obligatoire|"
        r"لا يشترط وجود خبرة|لا يشترط الخبرة|الخبرة غير مطلوبة|"
        r"بدون خبرة|دون خبرة|للمبتدئين)",
        ad,
    )

    # Preserve the prior rule for an unquantified mandatory-experience condition.
    # Numeric requirements up to 4 years are governed by the explicit threshold rule.
    if required_years is None and not no_experience_required:
        experience_language = re.search(
            r"(experience professionnelle|professional experience|work experience|"
            r"experience in the field|experience|خبرة مهنية|خبرة في المجال|خبرة)",
            ad,
        )
        mandatory_experience_nearby = re.search(
            r"(?:professional experience|experience professionnelle|work experience|"
            r"experience|خبرة مهنية|خبرة).{0,40}"
            r"(?:mandatory|required|obligatoire|requis|requise|exige|exigee|"
            r"اجبارية|إجبارية|مطلوبة|يشترط|ضرورية)"
            r"|(?:mandatory|required|obligatoire|requis|requise|exige|exigee|"
            r"اجبارية|إجبارية|مطلوبة|يشترط|ضرورية).{0,40}"
            r"(?:professional experience|experience professionnelle|work experience|"
            r"experience|خبرة مهنية|خبرة)",
            ad,
        )
        if (
            experience_language
            and mandatory_experience_nearby
            and not _advertisement_has_experience_upper_bound(ad)
        ):
            return True

    # A Licence/Bac+3 is not an Ingénieur d'État/Master/Bac+5 qualification.
    # Explicit Engineer title mismatches are handled in the title gate below.
    higher_degree = re.search(
        r"(diplome d ingenieur|diplome ingenieur|ingenieur d etat|"
        r"master(?:s)? degree|master 2|bac\s*5|doctorat|phd|postgraduate degree|"
        r"ماستر|شهادة مهندس|مهندس دولة|باك\s*5)",
        ad,
    )
    if higher_degree:
        window = ad[max(0, higher_degree.start() - 40):min(len(ad), higher_degree.end() + 50)]
        optional = re.search(
            r"(preferred|desirable|optional|a plus|souhaite|souhaitee|apprecie|"
            r"atout|facultatif|non obligatoire|pas obligatoire|يفضل|مفضل|ميزة اضافية|"
            r"ليس إلزاميا|غير إجباري|not required|not mandatory|not necessary|not needed|not a requirement|" +
            r"pas requis|non requis|pas exige|non exige|pas necessaire|non necessaire|" +
            r"n est pas requis|n est pas exige|غير مطلوب|ليس مطلوب|لا يشترط|غير اجباري|ليس الزامي|لا يلزم)",
            window,
        )
        mandatory = r"(required|mandatory|must have|must hold|obligatoire|requis|requise|exige|indispensable|يشترط|إجباري|اجباري|ضروري)"
        if re.search(mandatory, window) and not optional:
            return True

    # Only explicitly mandatory missing professional licences/certifications
    # are hard gates. A driving licence elsewhere cannot imply ATEX is required.
    for certificate in ("atex", "h2s", "bosiet", "caces", "nebosh", "api 510", "api 570", "aml", "easa part 66", "faa"):
        if certificate not in ad:
            continue
        escaped = re.escape(certificate)
        match = re.search(
            rf"(?:{escaped}).{{0,45}}(?:required|mandatory|obligatoire|requis|requise|exige|يشترط|إجباري|اجباري)"
            rf"|(?:required|mandatory|obligatoire|requis|requise|exige|يشترط|إجباري|اجباري).{{0,45}}(?:{escaped})",
            ad,
        )
        if match:
            optional = re.search(
                r"(preferred|desirable|optional|a plus|souhaite|souhaitee|apprecie|"
                r"atout|facultatif|non obligatoire|pas obligatoire|يفضل|مفضل|ميزة اضافية)",
                ad[max(0, match.start() - 20):min(len(ad), match.end() + 20)],
            )
            if not optional:
                return True

    return False

def enforce_candidate_fit_rule(job, decision, advertisement="", cv_match_score=None):
    """
    Apply the candidate's agreed rules before any Telegram review/send action.
    A keyword score never overrides a wrong discipline, wrong degree level,
    or an explicit mandatory requirement the candidate does not meet.
    """
    decision = str(decision or "REJECT").upper().strip()
    if decision not in {"APPLY", "REVIEW", "REJECT"}:
        decision = "REJECT"

    title = str(
        job.get("job_title")
        or job.get("application_title_fr")
        or job.get("application_title_en")
        or ""
    ).strip()
    normalized_title = normalize_text(title)

    if not normalized_title:
        return "REJECT", "رُفضت الوظيفة لأن المسمى الوظيفي غير واضح."

    if (
        any(re.search(pattern, normalized_title) for pattern in ENGINEERING_GRADE_ROLE_TITLE_PATTERNS)
        and not _advertisement_explicitly_accepts_licence_bac3(advertisement)
    ):
        return (
            "REJECT",
            "رُفضت الوظيفة لأن المسمى Ingénieur/Engineer يشير إلى مسار مهندس، ولم يثبت الإعلان قبول Licence/Bac+3 كبديل للمؤهل الهندسي.",
        )

    for pattern in UNRELATED_ROLE_TITLE_PATTERNS:
        if re.search(pattern, normalized_title):
            return (
                "REJECT",
                "رُفضت الوظيفة لأن تخصصها خارج المسارات المتفق عليها: الميكانيك والصيانة الميكانيكية والمضخات/الموائع والتصميم الميكانيكي.",
            )

    if cv_match_score is None:
        cv_match_score, _matched_groups = calculate_candidate_cv_match(job, advertisement)
    try:
        cv_match_score = max(0, min(100, int(cv_match_score)))
    except (TypeError, ValueError):
        cv_match_score = 0

    potentially_relevant = any(
        re.search(pattern, normalized_title)
        for pattern in (*STRONG_RELEVANT_ROLE_PATTERNS, *GENERIC_TECHNICAL_ROLE_PATTERNS)
    ) or cv_match_score >= 30

    try:
        ai_score = float(job.get("score"))
    except (TypeError, ValueError):
        ai_score = None
    if ai_score is not None and (ai_score < 0 or ai_score > 100):
        return "REJECT", "رُفضت الوظيفة لأن درجة AI خارج النطاق الصحيح 0–100."

    # Hard mismatches (degree, excluded discipline, named credentials, or >4
    # years of experience) still reject before any experience exception.
    if _advertisement_explicitly_requires_missing_qualification(job, advertisement):
        return (
            "REJECT",
            "رُفضت الوظيفة لأن الإعلان يثبت شرطًا إلزاميًا خارج استثناء سنوات الخبرة. لا تتغلب قاعدة الخبرة على التخصص أو المؤهل أو الاعتماد المطلوب.",
        )

    if _is_senior_role_title(normalized_title):
        return "REJECT", "رُفضت الوظيفة لأن المسمى إشرافي/قيادي ولا يناسب مستوى المرشح الحالي دون خبرة مهنية."

    required_years, experience_policy = _experience_requirement_policy(advertisement)
    if experience_policy == "manual_review":
        return (
            "REVIEW",
            f"يشترط الإعلان {required_years:g} سنوات خبرة؛ وفق القاعدة المتفق عليها، ما زاد على 3 سنوات وحتى 4 سنوات يُرسل للمراجعة اليدوية فقط، دون إرسال تلقائي.",
        )

    reason = str(job.get("reason", ""))
    no_requirement_lack_override = (
        experience_policy is None
        and decision in {"REJECT", "REVIEW"}
        and _reason_explicitly_says_candidate_lacks_experience(reason)
    )
    experience_only_override = (
        (experience_policy == "waive_experience" or no_requirement_lack_override)
        and potentially_relevant
        and cv_match_score >= 30
        and _reason_is_experience_only(reason)
    )
    if experience_only_override:
        if decision in {"REJECT", "REVIEW"}:
            decision = "APPLY"
        if experience_policy == "waive_experience":
            experience_waiver_reason = (
                f"تم تجاوز شرط الخبرة ({required_years:g} سنة/سنوات أو أقل) وفق القاعدة المتفق عليها؛ "
                "لم يظهر في سبب الرفض نقص تخصص/مؤهل مستقل. بقية حواجز التخصص والموقع ووسيلة التقديم والتكرار ما زالت فعالة."
            )
        else:
            experience_waiver_reason = (
                "لا يذكر الإعلان شرط خبرة إلزاميًا، لذلك لم يُستخدم نقص الخبرة سببًا للرفض؛ "
                "بقية حواجز التخصص والمؤهل والموقع ووسيلة التقديم والتكرار ما زالت فعالة."
            )
    else:
        experience_waiver_reason = ""

    if decision == "REJECT":
        if potentially_relevant:
            return (
                "REVIEW",
                f"الدور قريب من مسار المرشح (مؤشر CV {cv_match_score}/100) لكن الإعلان ضمن هذا القسم لا يثبت شرطًا إلزاميًا مفقودًا؛ لذلك حُوّل إلى مراجعة بشرية بدل الرفض الآلي. لا إرسال تلقائي.",
            )
        return "REJECT", "رُفضت الوظيفة لأن المسمى ومهارات المنصب المحددة لا يثبتان ارتباطًا كافيًا بسيرة المرشح."

    if decision == "APPLY":
        if not potentially_relevant:
            return (
                "REJECT",
                f"رُفضت الوظيفة لأن تخصص المنصب لا يطابق المسارات المتفق عليها (مؤشر CV {cv_match_score}/100).",
            )
        if cv_match_score < 30:
            return (
                "REVIEW",
                f"المسمى قريب تقنيًا لكن الأدلة الخاصة بهذا المنصب ضعيفة (مؤشر CV {cv_match_score}/100)؛ يلزم تحقق يدوي قبل أي تقديم.",
            )
        if ai_score is not None and ai_score < 35 and not experience_only_override:
            return (
                "REVIEW",
                f"درجة AI منخفضة ({ai_score:g}/100) رغم وجود صلة محتملة؛ يلزم تحقق يدوي قبل التقديم.",
            )
        if (
            not any(re.search(p, normalized_title) for p in STRONG_RELEVANT_ROLE_PATTERNS)
            and cv_match_score < 50
            and not experience_only_override
        ):
            return (
                "REVIEW",
                f"المنصب صناعي/تقني عام وليس تطابقًا مباشرًا واضحًا (مؤشر CV {cv_match_score}/100)؛ لا إرسال آلي.",
            )

    if decision == "REVIEW":
        if not potentially_relevant:
            return (
                "REJECT",
                "رُفضت الوظيفة حتى لو اختار AI المراجعة، لأن تخصص المنصب لا يطابق المسارات المتفق عليها.",
            )
        return "REVIEW", ""

    return decision, experience_waiver_reason if experience_only_override else ""

def contains_arabic(text):

    return bool(
        re.search(
            r"[\u0600-\u06FF]",
            str(text or "")
        )
    )


def extract_emails(text):

    pattern = (
        r"[A-Za-z0-9._%+-]+"
        r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    )

    matches = re.findall(
        pattern,
        text or ""
    )

    result = []
    seen = set()

    for email in matches:

        clean = email.strip()

        key = clean.lower()

        if key not in seen:

            seen.add(
                key
            )

            result.append(
                clean
            )

    return result


def email_domain(email):

    return str(
        email or ""
    ).strip().lower().split(
        "@",
        1
    )[-1]




# ============================================================
# JOB-POST SECTION SCOPING
# Prevent contact details, accommodation, and language rules from
# one numbered recruitment notice leaking into another.
# ============================================================

_RECRUITMENT_MARKER_RE = re.compile(
    r"(?:recrutement|recrute|offre\s+d['’ ]?emploi|offre\s+de\s+recrutement|"
    r"job\s+offer|job\s+vacanc(?:y|ies)|hiring|vacancy|vacancies|"
    r"توظيف|التوظيف|إعلان\s+توظيف|اعلان\s+توظيف|وظائف\s+شاغرة|"
    r"عرض\s+عمل|فرصة\s+عمل)",
    flags=re.IGNORECASE,
)

_NUMBERED_HEADING_RE = re.compile(
    r"^\s*(\d{1,2})\s*[.)\-–—:]\s*(.+?)\s*$"
)

_UNNUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:recrutement|recrute|offre\s+d['’ ]?emploi|"
    r"job\s+offer|job\s+vacanc(?:y|ies)|hiring|"
    r"إعلان\s+توظيف|اعلان\s+توظيف|توظيف)\b",
    flags=re.IGNORECASE,
)

_SCOPE_STOP_WORDS = {
    "de", "du", "des", "la", "le", "les", "un", "une", "et", "en",
    "au", "aux", "pour", "with", "the", "a", "an", "and", "of",
    "in", "at", "to", "for", "on", "as", "junior", "juniors",
    "مطلوب", "منصب", "وظيفة", "وظائف", "في", "من", "على", "و",
}


def _is_recruitment_section_heading(line: str) -> bool:
    """Detect clear top-level headings without splitting ordinary numbered requirements."""
    clean = re.sub(r"^[^\w\d]+", "", (line or "").strip()).strip()
    if not clean:
        return False

    numbered = _NUMBERED_HEADING_RE.match(clean)
    if numbered:
        heading_text = numbered.group(2)
        return bool(_RECRUITMENT_MARKER_RE.search(heading_text))

    return bool(_UNNUMBERED_HEADING_RE.match(clean))


def split_advertisement_sections(advertisement: str) -> list[str]:
    """
    Split a Telegram message into independently headed recruitment notices.
    If no confident boundary is found, return one section (the full message).
    """
    text = str(advertisement or "")
    lines = text.splitlines()
    if not lines:
        return [text] if text else [""]

    starts = [0]
    for index, line in enumerate(lines):
        if index == 0:
            continue
        if _is_recruitment_section_heading(line):
            starts.append(index)

    if len(starts) == 1:
        return [text]

    sections = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        section = "\n".join(lines[start:end]).strip()
        if section:
            sections.append(section)

    return sections or [text]


_COMPACT_ROLE_PREFIXES = (
    "chef d equipe", "gestionnaire", "acheteur", "responsable",
    "electromecanicien", "electricien", "plombier", "soudeur", "peintre",
    "technicien", "technologue", "ingenieur", "operateur", "agent",
    "coordinateur", "conducteur", "assistant", "charge", "mecanicien",
    "dessinateur", "projeteur", "controleur", "magasinier", "comptable",
    "superviseur", "chauffeur", "tourneur", "fraiseur", "programmeur",
    "regleur", "monteur", "manoeuvre", "مهندس", "تقني", "فني", "مشغل",
    "عامل", "مسؤول", "رئيس", "مدير", "مسير", "مشتري", "كهربائي",
    "سباك", "لحام", "دهان", "ميكانيكي", "عون", "مكلف", "مراقب", "منسق",
)


def extract_compact_listed_role_titles(advertisement: str) -> list[str]:
    """Extract job titles written as separate short lines in compact role lists."""
    found = []
    seen = set()
    for raw_line in str(advertisement or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•▪●]|\d{1,2}[.)\-])\s*", "", raw_line).strip()
        line = line.strip(" .;,-–—")
        if not line or len(line) > 120 or len(line.split()) > 14:
            continue
        if ":" in line or "http://" in line.lower() or "https://" in line.lower():
            continue
        normalized = normalize_text(line)
        if not normalized or not any(
            normalized == prefix or normalized.startswith(prefix + " ")
            for prefix in _COMPACT_ROLE_PREFIXES
        ):
            continue
        if normalized not in seen:
            seen.add(normalized)
            found.append(line)
    # Avoid treating an ordinary paragraph that starts with a title word as a
    # multi-position list. This is designed for short one-title-per-line posts.
    return found if len(found) >= 3 else []


def _compact_list_title_matches(title: str, job: dict) -> bool:
    normalized_title = normalize_text(title)
    if not normalized_title or not isinstance(job, dict):
        return False
    variants = [
        normalize_text(job.get(field) or "")
        for field in ("job_title", "application_title_fr", "application_title_en")
    ]
    for candidate in variants:
        if not candidate:
            continue
        if normalized_title == candidate or normalized_title in candidate or candidate in normalized_title:
            return True
        left = set(normalized_title.split()) - _SCOPE_STOP_WORDS
        right = set(candidate.split()) - _SCOPE_STOP_WORDS
        if left and right and len(left & right) / max(len(left), len(right)) >= 0.78:
            return True
    return False


def _recover_missing_compact_list_roles(titles: list[str], jobs: list[dict]) -> tuple[list[dict], int]:
    """Keep every detected title visible, routing omitted/underspecified roles to review only."""
    recovered = list(jobs)
    count = 0
    for title in titles:
        if any(_compact_list_title_matches(title, job) for job in recovered):
            continue
        recovered.append({
            "job_title": title,
            "application_title_fr": title,
            "application_title_en": title,
            "company": "",
            "location": "",
            "decision": "REVIEW",
            "score": 0,
            "reason": (
                "استُرجع هذا المسمى من قائمة وظائف منفصلة لأن التحليل الأول لم يستخرج كل المناصب. "
                "تفاصيل شروط هذا المنصب غير كافية في بيانات الاستخراج؛ يلزم التحقق اليدوي، "
                "ولا يسمح هذا الاسترجاع وحده بالإرسال التلقائي."
            ),
        })
        count += 1
    return recovered, count


def _scope_title_tokens(value: str) -> set[str]:
    tokens = normalize_text(value).split()
    return {
        token for token in tokens
        if len(token) >= 2 and token not in _SCOPE_STOP_WORDS
    }


def _position_title_variants(job: dict) -> list[str]:
    """
    Return literal titles plus a small, explicit set of bilingual aliases for
    title variants observed in Algerian Telegram notices. These aliases are
    used only to locate a position block; they never make a role relevant.
    """
    values = []
    for field in ("job_title", "application_title_fr", "application_title_en"):
        value = str(job.get(field) or "").strip()
        if value and value not in values:
            values.append(value)

    blob = normalize_text(" ".join(values))

    alias_groups = (
        (
            (
                ("service operator" in blob and any(
                    term in blob for term in ("surface solutions", "baroid", " ss ")
                ))
                or ("مشغل خدمات" in blob and "سوائل الحفر" in blob)
            ),
            (
                "مشغل خدمات معالجة سوائل الحفر",
                "مشغل خدمات سوائل الحفر",
                "Service Operator I SS",
            ),
        ),
        (
            (
                ("technologist" in blob and "laboratory" in blob)
                or ("مساعد تقني مخبر" in blob)
                or ("تقني مخبر" in blob)
            ),
            (
                "مساعد تقني مخبر",
                "تقني مخبر",
                "Technologist Laboratory Assoc",
            ),
        ),
        (
            (
                ("service supervisor" in blob and "cementing" in blob)
                or ("مشرف خدمات" in blob and "السمنتة" in blob)
            ),
            (
                "مشرف خدمات السمنتة",
                "مشرف السمنتة",
                "Service Supervisor III Cementing",
            ),
        ),
        (
            (
                (("technicien" in blob or "technician" in blob) and "maintenance" in blob)
                or "تقني صيانة" in blob
                or "تقني الصيانة" in blob
            ),
            (
                "تقني صيانة",
                "تقني الصيانة",
                "Technicien Maintenance",
                "Maintenance Technician",
            ),
        ),
        (
            (
                (("operateur" in blob or "operator" in blob) and "ligne" in blob)
                or "مشغل خط" in blob
            ),
            (
                "مشغل خط الإنتاج",
                "مشغل خط انتاج",
                "Opérateur de Ligne",
            ),
        ),
        (
            (
                any(term in blob for term in ("gestionnaire des stocks", "stock manager", "inventory", "storekeeper"))
                or "مسير مخزون" in blob
                or "مسير المخزون" in blob
            ),
            (
                "مسير مخزون",
                "مسير المخزون",
                "Gestionnaire des Stocks",
            ),
        ),
        (
            (
                any(term in blob for term in ("maintenance", "maintenancier", "maintenace", "صيانة"))
                and any(term in blob for term in ("ingenieur", "engineer", "هندسة", "مهندس"))
            ),
            (
                "Maintenance",
                "Maintenance industrielle",
                "Ingénieur Maintenance",
                "Maintenance Engineer",
                "هندسة صيانة",
                "صيانة",
            ),
        ),
        (
            any(term in blob for term in ("electronique", "electronic", "electronics", "الكترون", "إلكترون")),
            (
                "Électronique",
                "Electronic Engineering",
                "Engineering Electronics",
                "هندسة إلكترونية",
                "هندسة الكترونية",
                "إلكترونيك",
            ),
        ),
        (
            any(term in blob for term in ("controle qualite", "quality control", "مراقبة الجودة", "مراقبة النوعية")),
            (
                "Contrôle Qualité",
                "Quality Control",
                "مراقبة الجودة",
                "مراقبة النوعية",
            ),
        ),
    )

    for enabled, aliases in alias_groups:
        if enabled:
            for alias in aliases:
                if alias not in values:
                    values.append(alias)

    return values


def _job_section_score(job: dict, section: str) -> tuple[float, float]:
    """Return (total score, title score); title relevance is mandatory."""
    normalized_section = normalize_text(section)
    section_tokens = set(normalized_section.split())
    title_candidates = _position_title_variants(job)

    best_title_score = 0.0
    for candidate in title_candidates:
        normalized_title = normalize_text(candidate)
        if not normalized_title:
            continue
        if normalized_title in normalized_section:
            best_title_score = 1.0
            break

        tokens = _scope_title_tokens(candidate)
        if not tokens:
            continue
        matched = sum(1 for token in tokens if token in section_tokens)
        coverage = matched / len(tokens)
        best_title_score = max(best_title_score, coverage)

    # Company and location are secondary tie-breakers; neither may identify a
    # section without sufficient job-title evidence.
    score = best_title_score * 0.80

    company = normalize_text(job.get("company") or "")
    if company and len(company) >= 3 and company in normalized_section:
        score += 0.15

    location = normalize_text(job.get("location") or "")
    if location and len(location) >= 3 and location in normalized_section:
        score += 0.05

    return min(score, 1.0), best_title_score


def resolve_job_section(
    job: dict,
    advertisement: str,
    sections: list[str],
    total_jobs: int,
) -> tuple[str, bool, str]:
    """
    Map one AI-extracted role to its own advertisement section.
    Fail closed when independent jobs/contacts cannot be distinguished.
    """
    all_emails = extract_emails(advertisement)

    if len(sections) <= 1:
        # A single email can reasonably be shared by several positions in one
        # combined advertisement. Multiple emails without section boundaries
        # are ambiguous, so never fan the same application out to all of them.
        if len(all_emails) > 1:
            return "", False, "single section contains multiple email addresses"
        return advertisement, True, "single advertisement section"

    scored = []
    for index, section in enumerate(sections):
        score, title_score = _job_section_score(job, section)
        scored.append((score, title_score, index, section))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best_score, best_title_score, best_index, best_section = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_title_score < 0.55 or best_score < 0.50:
        return "", False, "no section confidently matches the job title"

    if len(scored) > 1 and (best_score - second_score) < 0.10:
        return "", False, "multiple advertisement sections match this job"

    return best_section, True, f"section {best_index + 1} of {len(sections)}"


def _is_shared_global_experience_footer_line(line: str) -> bool:
    """Identify explicit, post-wide experience conditions outside position blocks."""
    if _experience_requirement_years(line) is None:
        return False

    normalized = normalize_text(line)
    global_markers = (
        "شرط اساسي", "شرط عام", "شرط موحد", "متطلب عام", "المتطلب العام",
        "متطلبات عامة", "general condition", "general requirement",
        "common requirement", "for all positions", "all positions",
        "all candidates", "conditions generales", "condition generale",
        "condition essentielle", "exigence generale",
    )
    return any(marker in normalized for marker in global_markers)


def _is_global_requirements_heading(line: str) -> bool:
    normalized = normalize_text(line)
    markers = (
        "شروط التوظيف", "شروط العمل", "الشروط المطلوبة", "متطلبات التوظيف",
        "recruitment conditions", "job requirements", "employment requirements",
        "conditions de recrutement", "conditions de travail",
    )
    return any(marker in normalized for marker in markers)


def resolve_job_position_scope(
    job: dict,
    section_text: str,
    all_jobs: list[dict],
) -> tuple[str, bool, str]:
    """
    Split a single company post containing several positions into title-bounded
    blocks. Prevent another role's experience, skills, degree, or housing from
    leaking into this position's decision.
    """
    section_text = str(section_text or "")
    if not section_text:
        return "", False, "no verified advertisement section"

    valid_jobs = [item for item in (all_jobs or []) if isinstance(item, dict)]
    if len(valid_jobs) <= 1:
        return section_text, True, "single position"

    lines = section_text.splitlines()
    if not lines:
        return section_text, True, "single position"

    def title_variants(item: dict) -> list[str]:
        values = []
        for raw in _position_title_variants(item):
            value = normalize_text(raw)
            if value and value not in values and len(value) >= 4:
                values.append(value)
        return values

    def line_matches_multiple_roles(normalized_line: str) -> bool:
        """A shared headline listing several roles is not a position-specific title block."""
        line_tokens = set(_scope_title_tokens(normalized_line))
        matched_roles = 0
        for candidate_job in valid_jobs:
            role_matched = False
            for candidate in title_variants(candidate_job):
                normalized_candidate = normalize_text(candidate)
                if normalized_candidate and normalized_candidate in normalized_line:
                    role_matched = True
                    break
                tokens = _scope_title_tokens(candidate)
                if tokens:
                    coverage = sum(token in line_tokens for token in tokens) / len(tokens)
                    if coverage >= 0.75:
                        role_matched = True
                        break
            if role_matched:
                matched_roles += 1
                if matched_roles > 1:
                    return True
        return False

    def match_line(item: dict) -> tuple[int | None, float]:
        best_idx, best_score = None, 0.0
        variants = title_variants(item)
        if not variants:
            return None, 0.0
        for idx, line in enumerate(lines):
            normalized_line = normalize_text(line)
            if not normalized_line or "http" in normalized_line:
                continue
            if line_matches_multiple_roles(normalized_line):
                continue
            line_tokens = set(_scope_title_tokens(normalized_line))
            for candidate in variants:
                normalized_candidate = normalize_text(candidate)
                if normalized_candidate and normalized_candidate in normalized_line:
                    score = 1.0
                else:
                    tokens = _scope_title_tokens(candidate)
                    if not tokens:
                        continue
                    coverage = sum(token in line_tokens for token in tokens) / len(tokens)
                    score = coverage
                if score > best_score:
                    best_idx, best_score = idx, score
        return best_idx, best_score

    matches = []
    target_idx = None
    for item in valid_jobs:
        idx, score = match_line(item)
        if idx is None or score < 0.75:
            continue
        matches.append((idx, item, score))
        if item is job:
            target_idx = idx

    if target_idx is None:
        return "", False, "could not identify this position's own title block"

    same_line = [item for idx, item, _ in matches if idx == target_idx and item is not job]
    if same_line:
        return "", False, "multiple positions match the same title line"

    later_starts = sorted({idx for idx, item, score in matches if idx > target_idx and score >= 0.75})
    end_idx = later_starts[0] if later_starts else len(lines)

    # Explicit post-wide conditions placed after the final role title belong to
    # every position, not only the last bullet. Restrict this to clearly global
    # experience-condition lines so requirements for one role are never copied
    # into sibling positions.
    last_title_idx = max(idx for idx, _item, score in matches if score >= 0.75)
    requirements_headings = [
        idx for idx, line in enumerate(lines)
        if idx > last_title_idx and _is_global_requirements_heading(line)
    ]
    if requirements_headings:
        # An explicit shared "job requirements" heading after all listed roles
        # applies to each role, including shared Licence and no-experience rules.
        shared_footer_lines = lines[requirements_headings[0]:]
    else:
        shared_footer_lines = [
            line
            for idx, line in enumerate(lines)
            if idx > last_title_idx and _is_shared_global_experience_footer_line(line)
        ]

    block_lines = list(lines[target_idx:end_idx])
    block_normalized = {normalize_text(line) for line in block_lines if normalize_text(line)}
    for footer_line in shared_footer_lines:
        normalized_footer = normalize_text(footer_line)
        if normalized_footer and normalized_footer not in block_normalized:
            block_lines.append(footer_line)
            block_normalized.add(normalized_footer)

    block = "\n".join(block_lines).strip()
    if not block:
        return "", False, "position title block is empty"

    return block, True, f"position-specific block at line {target_idx + 1}"

def scoped_accommodation_value(
    job: dict,
    job_section: str,
    has_multiple_sections: bool,
):
    """
    Trust only explicit benefit evidence in the mapped advertisement section.

    The model's accommodation_provided field is a hypothesis, not evidence.
    Apply this rule to single- and multi-position posts alike.
    """
    signal = accommodation_signal_in_text(str(job_section or ""))
    if signal is True:
        return "true"
    if signal is False:
        return "false"
    return "unknown"


def accommodation_signal_in_text(text: str):
    """Return True only for explicit employer-provided housing; False for explicit negatives."""
    normalized = normalize_text(text)

    negative_markers = [
        "no accommodation",
        "accommodation not provided",
        "without accommodation",
        "no housing",
        "housing not provided",
        "sans hebergement",
        "sans logement",
        "hebergement non fourni",
        "logement non fourni",
        "pas d hebergement",
        "pas de logement",
        "لا يوجد سكن",
        "لا يتوفر سكن",
        "السكن غير متوفر",
        "الإقامة غير متوفرة",
        "الاقامة غير متوفرة",
        "دون سكن",
        "sans prise en charge du logement",
        "logement a la charge du candidat",
        "aucun logement fourni",
        "aucun hebergement fourni",
        "aucun hébergement fourni",
        "hebergement non assure",
        "hébergement non assuré",
        "logement non assure",
        "logement non assuré",
        "housing is not provided",
        "accommodation is not provided",
        "the candidate must arrange housing",
        "السكن على عاتق المترشح",
        "السكن على عاتق المترشح نفسه",
        "لا توفر الشركة السكن",
        "الشركة لا توفر السكن",
        "لا تتكفل الشركة بالإقامة",
    ]
    if any(normalize_text(marker) in normalized for marker in negative_markers):
        return False

    if any(normalize_text(marker) in normalized for marker in ACCOMMODATION_MARKERS):
        return True

    # Bare "résidence", "résider à", "candidats résidant à", "habitant à",
    # "مقيم في", and "يشترط الإقامة" are location/residency facts, not proof
    # that the employer provides accommodation.
    return None




# ============================================================
# AI JSON HELPERS
# ============================================================

def parse_json_response(result):

    if result is None:

        raise ValueError(
            "AI returned empty response"
        )

    result = str(
        result
    ).strip()

    if not result:

        raise ValueError(
            "AI returned empty response"
        )

    result = re.sub(
        r"^```(?:json)?\s*",
        "",
        result,
        flags=re.IGNORECASE
    )

    result = re.sub(
        r"\s*```$",
        "",
        result
    ).strip()

    try:

        return json.loads(
            result
        )

    except json.JSONDecodeError:

        pass

    start = result.find(
        "{"
    )

    end = result.rfind(
        "}"
    )

    if start != -1 and end > start:

        extracted = result[
            start:end + 1
        ]

        try:

            return json.loads(
                extracted
            )

        except json.JSONDecodeError as e:

            raise ValueError(
                f"Invalid JSON from AI: {e}"
            )

    raise ValueError(
        "AI response does not contain valid JSON"
    )


_DEFAULT_GROQ_MODEL_FALLBACKS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    # Independent model quota bucket on Groq.
    "qwen/qwen3.8-27b",
)
GROQ_MODEL_FALLBACKS = _DEFAULT_GROQ_MODEL_FALLBACKS if client is not None else ()

_RATE_LIMITED_GROQ_MODELS: set[str] = set()
_UNAVAILABLE_GROQ_MODELS: set[str] = set()
_DISABLED_GEMINI_MODELS: set[str] = set()
_GEMINI_DISABLED_FOR_RUN = False
_GROQ_DISABLED_FOR_RUN = False
_OPENROUTER_DISABLED_FOR_RUN = False
_AI_PROVIDER_LAST_REQUEST_AT: dict[str, float] = {}


def _pace_ai_provider(provider: str) -> None:
    """Apply an optional shared per-provider minimum interval to avoid quota bursts."""
    raw_interval = os.getenv("JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS", "0").strip()
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError):
        interval = 0.0
    if interval <= 0:
        return

    provider_key = str(provider or "unknown").strip().lower()
    now = time.monotonic()
    previous = _AI_PROVIDER_LAST_REQUEST_AT.get(provider_key)
    if previous is not None:
        remaining = interval - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
            now += remaining
    _AI_PROVIDER_LAST_REQUEST_AT[provider_key] = now


class AIProviderUnavailable(RuntimeError):
    """Raised when no configured AI route can currently process a request."""

    def __init__(self, retry_after_seconds: int, reason: str = "AI provider routes unavailable"):
        self.retry_after_seconds = max(60, int(retry_after_seconds))
        self.reason = str(reason or "AI provider routes unavailable")[:160]
        super().__init__(
            f"{self.reason}; retry after {self.retry_after_seconds} seconds"
        )


class AIProviderRateLimited(AIProviderUnavailable):
    """Raised when quota/rate limits exhaust the available AI routes."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(
            retry_after_seconds,
            reason="All configured AI providers are rate-limited",
        )


def _is_rate_limit_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if str(status_code) == "429":
        return True
    error_name = type(error).__name__.lower()
    details = str(error).lower()
    return (
        "ratelimit" in error_name
        or "rate limit" in details
        or "rate_limit_exceeded" in details
        or "resource_exhausted" in details
    )


def _is_provider_configuration_error(error: Exception) -> bool:
    """Recognize provider-wide credential/permission failures that model failover cannot fix."""
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if str(status_code) in {"401", "403"}:
        return True
    details = str(error or "").lower()
    return any(
        marker in details
        for marker in (
            "api key not valid",
            "invalid api key",
            "incorrect api key",
            "unauthenticated",
            "authentication failed",
            "authentication_error",
            "unauthorized",
            "permission_denied",
            "permission denied",
        )
    )


def _is_temporary_provider_overload(error: Exception) -> bool:
    """Identify temporary provider/network failures that warrant failover or cooldown."""
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if str(status_code) in {"408", "425", "429", "500", "502", "503", "504"}:
        return True
    details = str(error or "").lower()
    return any(
        marker in details
        for marker in (
            "high demand",
            "temporarily unavailable",
            "service unavailable",
            "server overloaded",
            "upstream timeout",
            "temporary failure in name resolution",
            "name or service not known",
            "connection reset",
            "connection aborted",
            "connection refused",
            "connection error",
            "network is unreachable",
            "remote end closed connection",
            "connect timeout",
            "read timeout",
            "timed out",
        )
    )




def _parse_rate_limit_duration(value):
    """Parse seconds or provider durations such as '2m59.5s'."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
        raw,
    )
    if not matches:
        return None
    total = 0.0
    for amount, unit in matches:
        if unit.startswith("d"):
            key = "d"
        elif unit.startswith("h"):
            key = "h"
        elif unit.startswith("m"):
            key = "m"
        else:
            key = "s"
        total += float(amount) * unit_seconds[key]
    return total if total > 0 else None


def _rate_limit_retry_seconds(error: Exception, default: int = 3600) -> int:
    """Honor Retry-After/reset headers; otherwise back off instead of retrying every 5 minutes."""
    headers = getattr(error, "headers", None)
    response = getattr(error, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    header_values = []
    if headers is not None:
        for name in (
            "retry-after",
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
        ):
            try:
                value = headers.get(name) or headers.get(name.title())
            except Exception:
                value = None
            parsed = _parse_rate_limit_duration(value)
            if parsed is not None:
                header_values.append(parsed)
    if header_values:
        seconds = max(header_values)
    else:
        details = str(error).lower()
        # Gemini can expose cooldowns in a structured retryDelay field instead
        # of an HTTP Retry-After header or a natural-language error message.
        structured_match = re.search(
            r"""["']?(?:retrydelay|retry_after|retryafter)["']?\s*:\s*["']([^"']+)["']""",
            details,
        )
        structured = (
            _parse_rate_limit_duration(structured_match.group(1))
            if structured_match else None
        )
        duration_match = re.search(
            r"(?:try again|retry|reset)(?: after| in|:)?\s*"
            r"(\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s))",
            details,
        )
        parsed = _parse_rate_limit_duration(duration_match.group(1)) if duration_match else None
        seconds = structured if structured is not None else (
            parsed if parsed is not None else default
        )
    return max(60, min(86400, int(seconds) + 15))


def _notify_ai_rate_limit(retry_after_seconds: int, provider: str) -> None:
    callback = JOB_AI_RATE_LIMIT_CALLBACK
    if callback is None:
        return
    try:
        callback(max(60, int(retry_after_seconds)), str(provider or "AI providers"))
    except Exception as exc:
        print("AI COOLDOWN PERSISTENCE ERROR:", type(exc).__name__)


def _notify_ai_provider_status(
    provider: str,
    model: str,
    status: str,
    reason: str = "",
    gemini_configured: bool = True,
) -> None:
    """Publish only a sanitized route status; callback failures never break analysis."""
    callback = JOB_AI_PROVIDER_STATUS_CALLBACK
    if callback is None:
        return
    if status not in {"healthy", "degraded", "groq_only"}:
        return
    snapshot = {
        "provider": str(provider or "unknown")[:24].lower(),
        "model": str(model or "")[:100],
        "status": status,
        "reason": " ".join(str(reason or "").split())[:180],
        "gemini_configured": bool(gemini_configured),
        "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        callback(snapshot)
    except Exception as exc:
        print("AI PROVIDER STATUS CALLBACK ERROR:", type(exc).__name__)


def _safe_ai_provider_error(error: Exception) -> str:
    """Return a short provider diagnostic without exposing prompts or credentials."""
    status_code = getattr(error, "status_code", None)
    details = str(error or "")
    message = ""
    match = re.search(r"Gemini HTTP \d+:\s*(\{.*\})", details, flags=re.DOTALL)
    if match:
        try:
            body = json.loads(match.group(1))
            error_body = body.get("error") or {}
            status_code = status_code or error_body.get("code")
            message = str(error_body.get("message") or error_body.get("status") or "")
        except (TypeError, ValueError):
            message = ""
    if not message:
        # Transport failures often wrap useful DNS/timeout detail in RuntimeError.
        # Expose only sanitized provider diagnostics; never prompts or credentials.
        fallback = re.sub(r"https?://\S+", "[request URL]", details)
        fallback = re.sub(
            r"(?i)(x-goog-api-key|api[-_ ]?key|authorization)\s*[:=]\s*[^,\s;]+",
            r"\1=[redacted]",
            fallback,
        )
        fallback = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted]", fallback)
        fallback = re.sub(r"sk-or-v1-[0-9A-Za-z_-]{20,}", "[redacted]", fallback)
        fallback = re.sub(r"gsk_[0-9A-Za-z_-]{20,}", "[redacted]", fallback)
        fallback = re.sub(r"[\r\n\t]+", " ", fallback)
        fallback = re.sub(r"\s+", " ", fallback).strip()[:220]
        return fallback or (f"HTTP {status_code}" if status_code else type(error).__name__)
    message = re.sub(r"[\r\n\t]+", " ", message)
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted]", message)
    message = re.sub(r"sk-or-v1-[0-9A-Za-z_-]{20,}", "[redacted]", message)
    message = re.sub(r"gsk_[0-9A-Za-z_-]{20,}", "[redacted]", message)
    message = re.sub(r"\s+", " ", message).strip()[:220]
    return f"HTTP {status_code}: {message}" if status_code else message


def _gemini_json_request(messages, temperature=0, max_completion_tokens=1000, model_name=None):
    """Use Gemini as an independent provider after Groq's model pool is exhausted."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    model_name = str(model_name or os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash").strip()
    system_content = []
    conversation = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role in {"system", "developer"}:
            system_content.append(content)
        else:
            conversation.append(f"{role.upper()}:\n{content}")
    prompt_parts = []
    if system_content:
        prompt_parts.append("SYSTEM INSTRUCTIONS:\n" + "\n\n".join(system_content))
    prompt_parts.extend(conversation)
    prompt = "\n\n".join(prompt_parts)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            # Gemini 3.8 rejects deprecated sampling parameters such as temperature.
            "maxOutputTokens": max_completion_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model_name, safe="")
        + ":generateContent"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:500]
        error = RuntimeError(f"Gemini HTTP {exc.code}: {details}")
        error.status_code = exc.code
        error.headers = exc.headers
        raise error from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None)
        if reason is not None:
            reason_detail = f"{type(reason).__name__}: {str(reason)[:150]}"
        else:
            reason_detail = type(exc).__name__
        raise RuntimeError(
            f"Gemini transport failure ({type(exc).__name__}): {reason_detail}"
        ) from exc
    candidates = body.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidate")
    content = candidates[0].get("content") or {}
    response_text = "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, dict)
    ).strip()
    if not response_text:
        raise ValueError("Gemini returned empty content")
    return parse_json_response(response_text)


# Google routes deprecated Gemini aliases to their replacements. Canonicalize them
# before failover so one underlying model/quota pool is never attempted twice.
_GEMINI_MODEL_ALIASES = {
    "gemini-3.7-flash": "gemini-3.8-flash",
    "gemini-3.5-flash": "gemini-3.6-flash",
}


def _configured_gemini_models():
    """Return unique Gemini model routes with distinct fallback quota pools."""
    primary = (
        os.getenv("GEMINI_MODEL", "").strip()
        or "gemini-3.8-flash"
    )
    # These are currently distinct stable text-generation model endpoints.
    # Keep aliases canonicalized even when a repository variable is customized.
    fallback_csv = (
        os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
        or "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    )
    models = []
    for candidate in [primary, *fallback_csv.split(",")]:
        name = str(candidate or "").strip()
        name = _GEMINI_MODEL_ALIASES.get(name, name)
        if name and name not in models:
            models.append(name)
    return tuple(models)



def _configured_openrouter_model() -> str:
    """Restrict the optional tertiary route to free OpenRouter model IDs."""
    model = os.getenv("OPENROUTER_MODEL", "").strip() or "openrouter/free"
    if model != "openrouter/free" and not model.endswith(":free"):
        raise RuntimeError(
            "OPENROUTER_MODEL must be openrouter/free or a model ID ending in :free; paid models are blocked"
        )
    return model


def _openrouter_json_request(messages, temperature=0, max_completion_tokens=1000):
    """Send a JSON-only request through OpenRouter's free-model router."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    model_name = _configured_openrouter_model()
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        # The free router may select a reasoning model. Reasoning consumes the
        # same output-token budget and can otherwise leave visible content empty.
        "max_tokens": max(512, int(max_completion_tokens or 0)),
        "reasoning": {"effort": "low", "exclude": True},
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/abdelkaderdj/jobfinder",
            "X-Title": "JobFinder",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:500]
        message = raw
        try:
            parsed = json.loads(raw)
            error_body = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error_body, dict):
                message = str(error_body.get("message") or error_body.get("code") or raw)
        except (TypeError, ValueError):
            pass
        error = RuntimeError(f"OpenRouter HTTP {exc.code}: {message}")
        error.status_code = exc.code
        error.headers = exc.headers
        raise error from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None)
        detail = f"{type(reason).__name__}: {str(reason)[:150]}" if reason is not None else type(exc).__name__
        raise RuntimeError(f"OpenRouter transport failure ({type(exc).__name__}): {detail}") from exc

    if not isinstance(body, dict):
        raise ValueError("OpenRouter returned an invalid response")
    if isinstance(body.get("error"), dict):
        error_body = body["error"]
        code = error_body.get("code")
        message = str(error_body.get("message") or "request rejected")
        error = RuntimeError(f"OpenRouter error: {message}")
        try:
            error.status_code = int(code)
        except (TypeError, ValueError):
            pass
        raise error
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("OpenRouter returned no response choices")
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        choice = choices[0]
        finish_reason = str(choice.get("finish_reason") or "unknown")[:80]
        response_model = str(body.get("model") or model_name)[:120]
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens", "unknown")
        token_details = usage.get("completion_tokens_details")
        reasoning_tokens = (
            token_details.get("reasoning_tokens", "unknown")
            if isinstance(token_details, dict)
            else "unknown"
        )
        raise ValueError(
            "OpenRouter returned empty content "
            f"(model={response_model}, finish_reason={finish_reason}, "
            f"completion_tokens={completion_tokens}, reasoning_tokens={reasoning_tokens})"
        )
    return parse_json_response(content)


def groq_json_request(messages, temperature=0, max_completion_tokens=1000):
    """
    Route through independent provider/model quota pools.

    Try Gemini first, then its alternate model quota pools, then Groq. Models
    that return rate-limit or model-unavailable responses are skipped for the
    rest of this short-lived runner so one exhausted quota cannot burn every
    remaining request. If every provider is unavailable, preserve the cursor
    and let the cloud runner retry later; never mark the failed post processed.
    """
    global _GEMINI_DISABLED_FOR_RUN, _GROQ_DISABLED_FOR_RUN, _OPENROUTER_DISABLED_FOR_RUN

    common_kwargs = {
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
    }
    last_error = None
    rate_limit_errors = []
    gemini_configured = bool(os.getenv("GEMINI_API_KEY", "").strip())
    openrouter_configured = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    configured_gemini_models = _configured_gemini_models() if gemini_configured else ()
    primary_gemini_model = configured_gemini_models[0] if configured_gemini_models else ""
    gemini_issues = []
    gemini_tried = False

    if gemini_configured and not _GEMINI_DISABLED_FOR_RUN:
        for model_name in configured_gemini_models:
            if model_name in _DISABLED_GEMINI_MODELS:
                continue
            gemini_tried = True
            try:
                _pace_ai_provider("gemini")
                result = _gemini_json_request(
                    messages=messages,
                    temperature=temperature,
                    max_completion_tokens=max_completion_tokens,
                    model_name=model_name,
                )
                degraded = bool(
                    model_name != primary_gemini_model or gemini_issues
                )
                status = "degraded" if degraded else "healthy"
                reason = (
                    gemini_issues[0]
                    if gemini_issues
                    else (
                        "Primary Gemini route was skipped or unavailable; a fallback model responded."
                        if degraded else ""
                    )
                )
                _notify_ai_provider_status(
                    "gemini", model_name, status, reason, gemini_configured
                )
                print(f"AI PROVIDER: Gemini ({model_name}) succeeded")
                return result
            except Exception as gemini_error:
                last_error = gemini_error
                gemini_issues.append(
                    f"{model_name}: {_safe_ai_provider_error(gemini_error)}"
                )
                if _is_rate_limit_error(gemini_error):
                    _DISABLED_GEMINI_MODELS.add(model_name)
                    rate_limit_errors.append(gemini_error)
                    print(
                        f"AI RATE LIMIT: Gemini ({model_name}); "
                        "switching to the next Gemini model"
                    )
                    continue

                if _is_provider_configuration_error(gemini_error):
                    _GEMINI_DISABLED_FOR_RUN = True
                    print(
                        "AI GEMINI AUTHENTICATION/CONFIGURATION ERROR: "
                        f"{_safe_ai_provider_error(gemini_error)}; "
                        "skipping the remaining Gemini models for this run"
                    )
                    break

                if _is_temporary_provider_overload(gemini_error):
                    _DISABLED_GEMINI_MODELS.add(model_name)
                    print(
                        f"AI GEMINI TEMPORARY OVERLOAD: {model_name}; "
                        f"{_safe_ai_provider_error(gemini_error)}; trying next model"
                    )
                    continue

                status_code = getattr(gemini_error, "status_code", None)
                if status_code is None:
                    status_code = getattr(gemini_error, "code", None)
                if str(status_code) in {"400", "404", "410"}:
                    _DISABLED_GEMINI_MODELS.add(model_name)
                    print(
                        f"AI GEMINI MODEL UNAVAILABLE: {model_name}; "
                        "switching to the next Gemini model"
                    )
                    continue

                # Invalid credentials, network failures, and provider-wide
                # errors are not worth repeating for each post in this run.
                _GEMINI_DISABLED_FOR_RUN = True
                print(
                    "AI GEMINI PROVIDER FAILED: "
                    f"{_safe_ai_provider_error(gemini_error)}; switching to Groq"
                )
                break

        if gemini_tried and not _GEMINI_DISABLED_FOR_RUN:
            _GEMINI_DISABLED_FOR_RUN = True
            print("AI GEMINI MODEL POOL EXHAUSTED: switching to Groq for this run")

    for model_name in GROQ_MODEL_FALLBACKS:
        if _GROQ_DISABLED_FOR_RUN:
            break
        if model_name in _RATE_LIMITED_GROQ_MODELS or model_name in _UNAVAILABLE_GROQ_MODELS:
            continue
        # Keep reasoning-oriented fallback models in low/hidden mode to reduce
        # unnecessary reasoning-token use while preserving structured JSON output.
        model_kwargs = {}
        if model_name.startswith("openai/gpt-oss-") or model_name == "qwen/qwen3.8-27b":
            model_kwargs = {"reasoning_effort": "low", "reasoning_format": "hidden"}
        request_kwargs = {**common_kwargs, **model_kwargs, "model": model_name}
        try:
            _pace_ai_provider("groq")
            response = client.chat.completions.create(
                **request_kwargs,
                response_format={"type": "json_object"},
            )
            result = parse_json_response(response.choices[0].message.content)
            if gemini_configured:
                reason = (
                    gemini_issues[0]
                    if gemini_issues
                    else "Gemini routes were unavailable earlier in this runner; Groq fallback responded."
                )
                _notify_ai_provider_status(
                    "groq", model_name, "degraded", reason, gemini_configured
                )
            else:
                _notify_ai_provider_status(
                    "groq",
                    model_name,
                    "groq_only",
                    "GEMINI_API_KEY is not configured.",
                    gemini_configured,
                )
            print(f"AI PROVIDER: Groq ({model_name}) succeeded")
            return result
        except Exception as first_error:
            last_error = first_error
            if _is_rate_limit_error(first_error):
                _RATE_LIMITED_GROQ_MODELS.add(model_name)
                rate_limit_errors.append(first_error)
                print(f"AI RATE LIMIT: {model_name}; switching to the next available model")
                continue
            if _is_temporary_provider_overload(first_error):
                _UNAVAILABLE_GROQ_MODELS.add(model_name)
                print(f"AI PROVIDER TEMPORARILY UNAVAILABLE: {model_name}; trying next model")
                continue
            if _is_provider_configuration_error(first_error):
                _GROQ_DISABLED_FOR_RUN = True
                print(
                    "AI GROQ AUTHENTICATION/CONFIGURATION ERROR: "
                    f"{_safe_ai_provider_error(first_error)}; "
                    "skipping remaining Groq models for this run"
                )
                break
            print(f"JSON MODE RETRY: {model_name}; {type(first_error).__name__}")

        try:
            _pace_ai_provider("groq")
            response = client.chat.completions.create(**request_kwargs)
            result = parse_json_response(response.choices[0].message.content)
            if gemini_configured:
                reason = (
                    gemini_issues[0]
                    if gemini_issues
                    else "Gemini routes were unavailable earlier in this runner; Groq fallback responded."
                )
                _notify_ai_provider_status(
                    "groq", model_name, "degraded", reason, gemini_configured
                )
            else:
                _notify_ai_provider_status(
                    "groq",
                    model_name,
                    "groq_only",
                    "GEMINI_API_KEY is not configured.",
                    gemini_configured,
                )
            print(f"AI PROVIDER: Groq ({model_name}) succeeded after JSON-mode retry")
            return result
        except Exception as second_error:
            last_error = second_error
            if _is_rate_limit_error(second_error):
                _RATE_LIMITED_GROQ_MODELS.add(model_name)
                rate_limit_errors.append(second_error)
                print(f"AI RATE LIMIT: {model_name}; switching to the next available model")
            elif _is_temporary_provider_overload(second_error):
                _UNAVAILABLE_GROQ_MODELS.add(model_name)
                print(f"AI PROVIDER TEMPORARILY UNAVAILABLE: {model_name}; trying next model")
            elif _is_provider_configuration_error(second_error):
                _GROQ_DISABLED_FOR_RUN = True
                print(
                    "AI GROQ AUTHENTICATION/CONFIGURATION ERROR: "
                    f"{_safe_ai_provider_error(second_error)}; "
                    "skipping remaining Groq models for this run"
                )
                break
            else:
                print(
                    f"AI MODEL RETRY FAILED: {model_name}; "
                    f"{type(second_error).__name__}"
                )

    # One optional independent provider after Gemini and Groq are exhausted.
    # The free router is a single endpoint, so a failure opens a per-run circuit
    # breaker rather than spending more free requests against the same route.
    if openrouter_configured and not _OPENROUTER_DISABLED_FOR_RUN:
        try:
            _pace_ai_provider("openrouter")
            result = _openrouter_json_request(
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
            )
            model_name = _configured_openrouter_model()
            using_fallback = bool(gemini_configured or GROQ_MODEL_FALLBACKS)
            reason = (
                gemini_issues[0]
                if gemini_issues
                else "Gemini/Groq routes were unavailable; the OpenRouter free fallback responded."
            )
            _notify_ai_provider_status(
                "openrouter",
                model_name,
                "degraded" if using_fallback else "healthy",
                reason if using_fallback else "",
                gemini_configured,
            )
            print(f"AI PROVIDER: OpenRouter ({model_name}) succeeded")
            return result
        except Exception as openrouter_error:
            last_error = openrouter_error
            _OPENROUTER_DISABLED_FOR_RUN = True
            if _is_rate_limit_error(openrouter_error):
                rate_limit_errors.append(openrouter_error)
                print(
                    "AI OPENROUTER RATE LIMIT: "
                    f"{_safe_ai_provider_error(openrouter_error)}; disabled for this run"
                )
            elif _is_provider_configuration_error(openrouter_error):
                print(
                    "AI OPENROUTER AUTHENTICATION/CONFIGURATION ERROR: "
                    f"{_safe_ai_provider_error(openrouter_error)}; disabled for this run"
                )
            elif _is_temporary_provider_overload(openrouter_error):
                print(
                    "AI OPENROUTER TEMPORARY OUTAGE: "
                    f"{_safe_ai_provider_error(openrouter_error)}; disabled for this run"
                )
            else:
                print(
                    "AI OPENROUTER REQUEST FAILED: "
                    f"{_safe_ai_provider_error(openrouter_error)}; disabled for this run"
                )

    all_groq_routes_exhausted = (
        not GROQ_MODEL_FALLBACKS
        or _GROQ_DISABLED_FOR_RUN
        or set(GROQ_MODEL_FALLBACKS).issubset(
            _RATE_LIMITED_GROQ_MODELS | _UNAVAILABLE_GROQ_MODELS
        )
    )
    all_gemini_routes_exhausted = (
        not gemini_configured or _GEMINI_DISABLED_FOR_RUN
    )
    all_openrouter_routes_exhausted = (
        not openrouter_configured or _OPENROUTER_DISABLED_FOR_RUN
    )

    if (
        all_groq_routes_exhausted
        and all_gemini_routes_exhausted
        and all_openrouter_routes_exhausted
    ):
        if rate_limit_errors:
            retry_seconds = max(
                (_rate_limit_retry_seconds(error) for error in rate_limit_errors),
                default=3600,
            )
            if openrouter_configured:
                configured_providers = []
                if gemini_configured:
                    configured_providers.append("Gemini")
                if GROQ_MODEL_FALLBACKS:
                    configured_providers.append("Groq")
                configured_providers.append("OpenRouter")
                provider_names = "/".join(configured_providers)
                provider = (
                    f"AI quota exhausted across {provider_names} routes; "
                    "some routes may also be temporarily overloaded"
                )
            elif gemini_configured and (gemini_tried or _GEMINI_DISABLED_FOR_RUN):
                provider = (
                    "AI quota exhausted across Gemini/Groq routes; "
                    "some routes may also be temporarily overloaded"
                )
            else:
                provider = (
                    "Groq quota exhausted; configure GEMINI_API_KEY for an independent AI provider; "
                    "some routes may also be temporarily overloaded"
                )
            _notify_ai_rate_limit(retry_seconds, provider)
            raise AIProviderRateLimited(retry_seconds) from last_error

        # Only apply an outage cooldown when the last failures indicate temporary
        # overload. Authentication, model-ID, and request errors should surface
        # as configuration/analysis failures rather than being mislabeled as outages.
        if last_error is not None and _is_temporary_provider_overload(last_error):
            try:
                outage_retry_seconds = int(
                    os.getenv("JOBFINDER_AI_OUTAGE_RETRY_SECONDS", "300").strip()
                )
            except (TypeError, ValueError):
                outage_retry_seconds = 300
            outage_retry_seconds = max(60, min(3600, outage_retry_seconds))
            provider = "All configured Groq routes are temporarily overloaded"
            if gemini_configured and (gemini_tried or _GEMINI_DISABLED_FOR_RUN):
                provider = "All configured Gemini/Groq routes are temporarily unavailable"
            if openrouter_configured:
                provider = "All configured AI provider routes are temporarily unavailable"
            _notify_ai_rate_limit(outage_retry_seconds, provider)
            raise AIProviderUnavailable(outage_retry_seconds, provider) from last_error

    if last_error is not None and _is_provider_configuration_error(last_error):
        try:
            config_retry_seconds = int(
                os.getenv("JOBFINDER_AI_CONFIG_RETRY_SECONDS", "900").strip()
            )
        except (TypeError, ValueError):
            config_retry_seconds = 900
        config_retry_seconds = max(300, min(3600, config_retry_seconds))
        provider = (
            "AI provider credentials/permissions rejected; check GEMINI_API_KEY, "
            "GROQ_API_KEY, OPENROUTER_API_KEY, and model configuration"
        )
        _notify_ai_rate_limit(config_retry_seconds, provider)
        raise AIProviderUnavailable(config_retry_seconds, provider) from last_error

    if last_error is not None:
        raise last_error
    raise RuntimeError("No configured AI provider was available")


# ============================================================
# AI ANALYSIS
# ============================================================

def analyze_offer(text, vacancy_rescue=False):

    prompt = """

You are a precise multilingual recruitment screening AI.

Analyze the COMPLETE job advertisement against the real candidate.

============================================================
CORE RULES
============================================================

1. Understand Arabic, French and English.

2. Identify EVERY distinct position.

3. Do not combine multiple positions.

4. Judge the actual employer requirements.

5. Never invent candidate experience or qualifications.

6. The candidate has NO professional engineering experience.

7. The candidate graduated in 2021.

8. The candidate is NOT a fresh graduate and NOT a recent graduate.

9. Junior and entry-level positions can be APPLY only when the
candidate genuinely matches the stated requirements.

10. Clearly unrelated positions are REJECT.

11. Partially relevant positions with uncertainty are REVIEW.

12. Extract every email address found in the advertisement.

13. Do NOT assume that a technical role requires prior professional experience,
   certification, trade training, or a specific degree merely because that is
   common in the industry. Mark a requirement as mandatory only when the ad
   explicitly states it. For a plausibly relevant junior/technical role, use
   REVIEW if eligibility is uncertain; do not REJECT based on an inferred
   requirement. REVIEW is safe because it cannot send an automatic email.

14. Preserve the OWNER-DEFINED candidate boundaries below; they outrank broad
    keyword overlap, company industry, or generic words such as "technical",
    "industrial", "equipment", "energy", "production", or "hydraulic".

    - Highest qualification: Licence / Bac+3 in Mechanical and Energy Engineering.
      Treat as technician / Bac+3 level, NOT as an Ingénieur / Engineer diploma.
      Any advertised position titled "Ingénieur", "Engineer", or "مهندس" is REJECT unless the position-specific ad explicitly accepts Licence/Bac+3 as sufficient and does not make a higher engineering diploma mandatory. When that exception is explicit, evaluate the actual duties and discipline; do not reject solely because of the title.
     - Target lanes: mechanical maintenance technician; mechanical equipment;
       refrigeration/froid/frigoriste, HVAC and thermal technician roles; heat
       transfer and thermodynamics-related technical work; centrifugal pumps and
       fluid systems; junior mechanical/CAD/SolidWorks design and technical
       drawings; closely adjacent industrial operator roles only when actual
       duties and entry requirements fit. A Licence in Mechanical and Energy
       Engineering includes relevant academic grounding in energy and heat
       transfer, but does not prove professional refrigeration experience.
       Do not exclude a cooling role solely because it says "froid" or "HVAC".
       When the ad accepts beginners, a stage, practical test or training, assess
       the remaining explicit requirements; keep unclear cases in REVIEW.
      instrumentation, civil engineering, HSE engineering, laboratory chemistry/
      biology, aircraft maintenance/aviation, stock/inventory/logistics/supply
      chain, sales, and other unrelated roles even if the full advertisement
      mentions maintenance, pumps, fluids, or production elsewhere.
    - The candidate has no professional engineering experience. Explicit
      minimum experience requirements not met by the candidate are REJECT,
      including "at least 6 months", "6 months minimum", "2–5 years",
      "2 ans minimum", and "6 أشهر على الأقل". These are not REVIEW.
    - "Débutant accepté", "no experience required", or equivalent means do NOT
      invent an experience requirement; assess the other actual requirements.
    - For a message containing several jobs, evaluate each position's own title
      and text block. Never use keywords or experience requirements belonging
      to another position to decide this one.

============================================================
VERY IMPORTANT — COMMON / GLOBAL REQUIREMENTS
============================================================

A Telegram message may combine several separate recruitment notices,
often with numbered headings, different companies, locations, contacts,
and application methods.

Treat each numbered/separately headed recruitment notice as its own scope.
Company, location, accommodation, application method, and email addresses
inside one notice belong only to positions in that notice unless the text
explicitly says otherwise.

Only a condition clearly presented as a common requirement for ALL listed
positions applies across multiple positions/notices. Never copy an email,
WhatsApp number, accommodation condition, CV-language instruction, or
company from a different separately headed notice.

For each position, the "emails" array must include only email addresses
clearly associated with that position's own notice. Do not return every
email in the complete Telegram message for every position.

Examples of common requirements:

- "Experience in Oil & Gas is required"
- "Minimum 3 years of experience"
- "Professional experience is mandatory"
- "ATEX certification required"
- "Only candidates residing in Libya"
- "Work certificate required"
- "Previous Thermal Desorption experience required"

If such a condition is common to the advertisement:

YOU MUST APPLY IT TO EVERY POSITION.

A strong match with the job title DOES NOT override a common mandatory
requirement.

Example:

Advertisement has 10 jobs.

General requirement:
"Proven Oil & Gas experience is required."

Candidate:
No professional Oil & Gas experience.

Correct result:
All 10 jobs governed by that requirement must be REJECT.

Incorrect result:
Applying to "Pump Man" only because the candidate knows centrifugal
pumps.

Another example:

General requirement:
"Previous Thermal Desorption Operations experience required."

Candidate:
No Thermal Desorption professional experience.

Correct:
"Mechanical Technician" -> REJECT.

Do NOT ignore common requirements simply because the job title matches
the candidate's academic background.

============================================================
CANDIDATE vs EMPLOYER
============================================================

The employer requirements describe what the COMPANY wants.

The candidate profile describes what the CANDIDATE actually has.

Never confuse them.

A job responsibility is NOT evidence that the candidate performed it.

Example:

Advertisement:
"Assurer la maintenance préventive et corrective"

Candidate:
"Industrial maintenance fundamentals"

Correct:
Candidate has knowledge/fundamentals in industrial maintenance.

Incorrect:
Candidate has preventive and corrective maintenance experience.

Incorrect:
Candidate can perform preventive and corrective maintenance
professionally.

============================================================
EXPERIENCE RULE
============================================================

The candidate has:

NO professional engineering experience.

Knowledge is NOT experience.

Training is NOT experience.

Education is NOT experience.

Never describe:

- maintenance knowledge as maintenance experience
- pump knowledge as pump experience
- training as professional experience
- university study as professional experience

Never use:
"experience"
"expérience"
"professional experience"
"expérience professionnelle"

to describe knowledge or training.

============================================================
GRADUATION DATE RULE
============================================================

The candidate graduated in 2021.

Never call him:

- fresh graduate
- recent graduate
- newly graduated
- recent graduate profile

A candidate who graduated in 2021 must simply be described as:

- Junior / Entry-level candidate
- Bachelor / Licence holder
- candidate without professional engineering experience

============================================================
LEVEL / SENIORITY RULE
============================================================

The candidate is Junior / Entry-level.

Roles such as:

- Senior
- Specialist
- Expert
- Manager
- Team Leader
- Supervisor
- Foreman
- Chief
- Head

normally require professional experience or leadership.

If the advertisement does not provide enough information to establish
a realistic junior match for such a role, prefer REVIEW or REJECT.

Do not automatically APPLY simply because the title is mechanically
related.

============================================================
EDUCATION RULE
============================================================

Candidate has:

Licence / Bachelor in Mechanical and Energy Engineering.

Candidate does NOT have:

Engineer degree.

Therefore:

"Mechanical Engineer" or "Ingénieur en Mécanique" requiring an
engineering degree must NOT be treated as equivalent to the candidate's
Licence/Bachelor.
""
"However, do NOT infer a mandatory engineering diploma from the title alone."
"If the advertisement explicitly says Licence/Bachelor/Bac+3 holders are eligible"
"for the listed position, do NOT score the position as 0 or reject it solely"
"because the heading uses Ingénieur/Engineer. Evaluate the exact discipline and"
"stated qualification; use REVIEW if the title or qualification remains ambiguous."
""

Do not call the candidate "Ingénieur diplômé".

============================================================
LOCATION / ELIGIBILITY RULE
============================================================

Candidate's preferred work geography:

LOCAL / NEARBY:

- Mostaganem
- Oran
- Arzew
- Bethioua
- Arzew/Bethioua industrial zones
- nearby areas around Oran and Mostaganem

SOUTH / SAHARA:

Accept positions anywhere in southern / Saharan Algeria, especially
energy/oilfield locations.

OTHER ALGERIAN LOCATIONS:

Positions outside the accepted local/nearby area and outside the South
should normally be rejected unless the employer clearly provides:

- accommodation explicitly provided/covered by the employer
- housing/logement explicitly supplied, paid, or taken in charge by the employer
- a staff residence explicitly provided (for example "résidence de fonction")
- camp/base de vie when the advertisement explicitly states it is provided
- accommodation + meals when it clearly means employer-provided on-site living

A requirement that candidates already live/reside in a city or wilaya is NOT
an accommodation benefit. The bare words "résidence", "logement", "housing",
or "accommodation" without explicit provision by the employer do NOT prove that
housing is provided.

IMPORTANT:

Do not reject a South/Saharan position merely because accommodation is
not explicitly stated.

If work location is unclear, use REVIEW rather than assuming it is
acceptable.

If a position explicitly requires candidates to:

- live in Libya
- be currently in Libya
- be residents of a specific country
- have a specific eligibility condition

and the candidate does not satisfy it:

REJECT that position.

============================================================
LOCATION OUTPUT
============================================================

For each job return:

- company: employer/company name if explicitly identifiable,
  otherwise ""
- location: the main work location for THIS position if identifiable,
  otherwise ""
- accommodation_provided: true / false / unknown
- location_reason: short factual explanation

Do NOT invent a company or location.

============================================================
DECISION RULE
============================================================

APPLY:

Use only when the candidate genuinely matches the role and mandatory
requirements AND the location is eligible.

REVIEW:

Use when the role is potentially relevant but there is a meaningful
missing or unclear requirement or location.

REJECT:

Use when:

- a clearly stated mandatory requirement in the advertisement is not met
- explicitly required experience/certification/education is missing
- location eligibility is not met
- the role is unrelated
- seniority is clearly above junior level

Do not infer a mandatory requirement from the job title or usual industry practice.
If a mechanically/technically relevant role might fit but required experience,
certification, or education is not explicitly stated in the ad, choose REVIEW,
not REJECT. Do not use REJECT merely because the candidate's profile does not
mention a skill; missing information is not proof of disqualification.

============================================================
CV / APPLICATION LANGUAGE
============================================================

Do NOT determine the application language from the Telegram post
language alone.

The person who posted the Telegram advertisement may have translated
the job title or the entire advertisement.

Priority:

1. Explicit employer instruction about CV/application language.

Examples:

"Send CV in English" -> en
"Send your resume in English" -> en
"CV باللغة الإنجليزية" -> en
"Envoyer votre CV en anglais" -> en
"CV en français" -> fr
"Envoyer le CV en français" -> fr

2. If no explicit language is requested:

Determine the employer's normal working/application language from:

- employer country
- company identity
- international or foreign status
- sector
- international client/project
- company email/domain when useful

Defaults:

Libya / Libyan employer -> en

Foreign company / international company / global company ->
en

International project / international client ->
en

Chinese/foreign company operating in Algeria when employer context is
international -> en

Algerian local company -> fr

If still uncertain:
use the strongest company/market signal rather than Telegram
advertisement language.

The application language must be UNIFORM for the application:

- email body
- subject
- job title inside the email
- CV choice

Do NOT mix Arabic job titles into French or English emails.

============================================================
APPLICATION JOB TITLE
============================================================

For every job return:

- job_title = recognizable original job title
- application_title_fr = clean French title
- application_title_en = clean English title

IMPORTANT:

If the advertisement already provides a clean English title for the
position, DO NOT replace it with a different English synonym.

Example:

"ميكانيكي مضخات | Pump Man"

Correct English title:
"Pump Man"

Incorrect:
"Pump Mechanic"

Likewise:

"Junior Mechanical Maintenance Assistant"

must remain:

"Junior Mechanical Maintenance Assistant"

Rules:

1. If the advertisement already gives a clean French title, preserve it.

2. If it gives a clean English title, preserve it.

3. If it gives Arabic + English or Arabic + French, select the title
   corresponding to the application language.

4. If only Arabic is available, translate ONLY the job title into the
   required application language.

5. Do not put Arabic alongside the translated title.

6. Do not add qualifications that are not in the title.

7. Keep the title concise and professional.

8. Never invent a new position.

9. Do not replace an existing employer title with a synonym.

============================================================
REASON RULE
============================================================

The "reason" must be based only on factual candidate information and
actual advertisement requirements.

Clearly distinguish:

- education
- knowledge
- training
- professional experience

Do not invent professional experience.

Never describe the candidate as:

"fresh graduate"

"recent graduate"

============================================================
OUTPUT FORMAT
============================================================

Return ONLY valid JSON.

{
  "jobs": [
    {
      "job_title": "original or recognizable title",
      "application_title_fr": "clean French title",
      "application_title_en": "clean English title",
      "company": "",
      "location": "",
      "accommodation_provided": "true|false|unknown",
      "location_reason": "",
      "language": "fr|en|ar|mixed",
      "decision": "APPLY|REVIEW|REJECT",
      "score": 0,
      "reason": "short factual explanation",
      "emails": ["email@example.com"]
    }
  ]
}

Score from 0 to 100 using CV-to-vacancy overlap, not a feeling of how easy the job is to get.

CALIBRATED SCORE BANDS:
- 0–19: no material overlap with the candidate's education or skills.
- 20–29: weak/adjacent overlap; do not treat as a strong fit.
- 30–49: relevant overlap but borderline or missing evidence; normally REVIEW,
  never automatic rejection merely because professional experience is absent.
- 50–69: meaningful overlap; APPLY is possible only if stated mandatory requirements
  are met and the role is appropriate for junior/entry-level.
- 70–89: strong evidence-based match.
- 90–100: unusually close match; do not use this band casually.

Do not assign 0 only because the CV has no professional experience. The CV has
a Licence (Bac+3) in Mechanical and Energy Engineering (2021), fundamentals of
mechanical systems and industrial maintenance, knowledge of mechanical equipment,
centrifugal pumps and fluid systems, basic mechanical troubleshooting, technical
drawings/documentation, and SolidWorks training in part modelling, assemblies
and technical drawings. Treat knowledge/training as knowledge/training, not work
experience. Excel and Word are office skills; Arabic is native, French and English
are intermediate. Do not invent skills not listed in the CV.

A plausible overlap of 30% or more is a REVIEW floor when eligibility is uncertain,
not permission to auto-send. Missing facts about age, driving licence, or residency
must not be hallucinated as mismatches.

Remember:

COMMON REQUIREMENTS APPLY TO ALL JOBS IN THE SAME ADVERTISEMENT
unless the advertisement explicitly limits the requirement to certain
positions.

============================================================
REAL CANDIDATE
============================================================
""" + CANDIDATE + """

============================================================
JOB ADVERTISEMENT
============================================================
""" + text


    if vacancy_rescue:
        prompt += """

VACANCY EXTRACTION RETRY:
The previous pass returned an empty jobs array. Re-read the COMPLETE
advertisement carefully. If it advertises one or more positions, return
each distinct position in the JSON "jobs" array even if the candidate
does not qualify; use REJECT for an unsuitable position instead of
omitting it. Return an empty array only when the text truly has no job
vacancy. Never invent titles, employers, locations, contacts, or
requirements.
"""

    messages = [

        {
            "role": "system",
            "content": (
                "You are a strict recruitment screening AI. "
                "Common mandatory requirements must be applied to every "
                "position they govern. "
                "Never invent candidate experience. "
                "Never call the candidate a fresh graduate or recent "
                "graduate because he graduated in 2021. "
                "Preserve employer-provided job titles whenever possible. "
                "Do not invent company or location. "
                "Return only valid JSON."
            )
        },

        {
            "role": "user",
            "content": prompt
        }

    ]

    return groq_json_request(
        messages=messages,
        temperature=0,
        max_completion_tokens=2500
    )


# ============================================================
# LOCATION RULES - DETERMINISTIC SAFETY LAYER
# ============================================================

LOCAL_LOCATION_MARKERS = [

    # Oran
    "oran",
    "wahran",
    "وهران",
    "es senia",
    "es-senia",
    "السانية",
    "bir el djir",
    "بئر الجير",
    "sidi chami",
    "سيدي الشحمي",
    "hassi bounif",
    "حاسي بونيف",
    "hassi ben okba",
    "حاسي بن عقبة",

    # Arzew / Bethioua
    "ain el bia",
    "ain el biya",
    "عين البية",
    "arzew",
    "arzيو",
    "أرزيو",
    "bethouia",
    "bethioua",
    "بطيوة",
    "mers el hadjadj",
    "مرس الحجاج",

    # Mostaganem
    "mostaganem",
    "مستغانم",
    "mazagran",
    "مزغران",
    "hassi mameche",
    "hassi mamèche",
    "حاسي ماماش",
    "sayada",
    "صيادة",
    "kheir eddine",
    "خير الدين",
    "ain nouissy",
    "عين النويصي",
    "fornaka",
    "فرناكة"
]


SOUTH_LOCATION_MARKERS = [

    "adrar",
    "bechar",
    "béchar",
    "tamanrasset",
    "tamenghasset",
    "ouargla",
    "el oued",
    "ghardaia",
    "ghardaïa",
    "illizi",
    "djanet",
    "timimoun",
    "bordj badji mokhtar",
    "in salah",
    "in guezzam",
    "el meniaa",
    "el menia",
    "touggourt",
    "hassi messaoud",
    "hassi massoud",
    "حاسي مسعود",
    "hassi r mel",
    "hassi r'mel",
    "hassi r'mal",
    "حاسي الرمل",
    "in amenas",
    "عين أميناس"
]


# These are positive evidence phrases, not generic housing/residency nouns.
# Never treat "résidence à Alger" or "candidats résidant à Oran" as a housing benefit.
ACCOMMODATION_MARKERS = [
    "logement fourni",
    "hebergement fourni",
    "hébergement fourni",
    "logement assure",
    "hebergement assure",
    "hébergement assuré",
    "logement pris en charge",
    "hebergement pris en charge",
    "hébergement pris en charge",
    "logement de fonction",
    "residence de fonction",
    "résidence de fonction",
    "residence fournie",
    "résidence fournie",
    "accommodation provided",
    "housing provided",
    "lodging provided",
    "staff housing provided",
    "base de vie fournie",
    "camp fourni",
    "السكن متوفر",
    "السكن موفر",
    "توفير السكن",
    "التكفل بالسكن",
    "التكفل بالإقامة",
    "التكفل بالاقامة",
    "سكن وظيفي",
    "إقامة وظيفية",
    "اقامة وظيفية"
]


def location_contains_any(
    location,
    markers
):

    normalized = normalize_text(
        location
    )

    return any(
        normalize_text(marker) in normalized
        for marker in markers
    )


def job_location_category(job):

    location = str(
        job.get(
            "location",
            ""
        ) or ""
    ).strip()

    if not location:

        return "unknown"

    if location_contains_any(
        location,
        LOCAL_LOCATION_MARKERS
    ):

        return "local"

    if location_contains_any(
        location,
        SOUTH_LOCATION_MARKERS
    ):

        return "south"

    return "other"


def accommodation_value(job):

    value = job.get(
        "accommodation_provided",
        "unknown"
    )

    if isinstance(
        value,
        bool
    ):

        return value

    normalized = normalize_text(
        value
    )

    if normalized in {
        "true",
        "yes",
        "oui",
        "1",
        "provided",
        "available"
    }:

        return True

    if normalized in {
        "false",
        "no",
        "non",
        "0",
        "not provided",
        "unavailable"
    }:

        return False

    return None


def explicit_accommodation_in_ad(
    text
):

    return accommodation_signal_in_text(text) is True


def enforce_location_rule(
    job,
    decision,
    job_section_text,
    section_verified=True,
):
    category = job_location_category(job)
    accommodation = accommodation_value(job)

    if category == "local":
        return decision, "allowed_local", "Local/nearby area accepted"

    if category == "south":
        return decision, "allowed_south", "Southern/Saharan Algeria accepted"

    # Unknown geography cannot be made eligible by an accommodation claim.
    if category == "unknown":
        if decision == "APPLY":
            return (
                "REVIEW",
                "unknown_location",
                "Work location is not clear enough to confirm eligibility",
            )
        return (
            decision,
            "unknown_location",
            "Work location is not clearly identified",
        )

    # If a combined advertisement could not be mapped to this exact position,
    # do not let another section's location/housing facts decide this one.
    # REVIEW is safe: it blocks auto-send but avoids silently losing a plausible role.
    if category == "other" and not section_verified:
        if decision == "APPLY":
            return (
                "REVIEW",
                "location_section_unverified",
                "Could not verify this position's own location/accommodation section; automatic sending is disabled.",
            )
        return (
            decision,
            "location_section_unverified",
            "This position's own location/accommodation section could not be verified; do not infer an accommodation benefit.",
        )

    # For other Algerian regions, only explicit employer-provided housing
    # verified against this position's own advertisement section can qualify.
    if category == "other" and accommodation is True:
        return decision, "allowed_housing", "Explicit employer-provided housing verified in this position's ad"

    if category == "other":
        return (
            "REJECT",
            "rejected_location",
            "Work location is outside the accepted local/Saharan area and the ad does not explicitly provide accommodation",
        )

    return decision, "unknown_location", "Work location could not be classified"




# ============================================================
# LANGUAGE DETECTION
# ============================================================

def detect_explicit_cv_language(
    text
):

    text_lower = (
        text or ""
    ).lower()

    english_patterns = [

        r"send.{0,80}(?:cv|resume).{0,50}(?:english)",

        r"(?:cv|resume).{0,80}"
        r"(?:in english|english)",

        r"(?:english|anglais|الانجليزية|الإنجليزية)"
        r".{0,80}"
        r"(?:cv|resume|السيرة الذاتية)",

        r"(?:cv|resume|السيرة الذاتية)"
        r".{0,80}"
        r"(?:english|anglais|الانجليزية|الإنجليزية)"
    ]

    french_patterns = [

        r"envoyez?.{0,80}"
        r"(?:cv|curriculum vitae).{0,50}"
        r"(?:français|francais)",

        r"(?:cv|curriculum vitae)"
        r".{0,80}"
        r"(?:en français|en francais|français|francais)",

        r"(?:français|francais)"
        r".{0,80}"
        r"(?:cv|curriculum vitae|السيرة الذاتية)",

        r"(?:cv|curriculum vitae|السيرة الذاتية)"
        r".{0,80}"
        r"(?:français|francais)"
    ]

    english_hit = any(
        re.search(
            pattern,
            text_lower,
            flags=re.IGNORECASE | re.DOTALL
        )
        for pattern in english_patterns
    )

    french_hit = any(
        re.search(
            pattern,
            text_lower,
            flags=re.IGNORECASE | re.DOTALL
        )
        for pattern in french_patterns
    )

    if english_hit and not french_hit:

        return "en"

    if french_hit and not english_hit:

        return "fr"

    return None


def has_foreign_employer_signal(
    text
):

    text_lower = (
        text or ""
    ).lower()

    foreign_markers = [

        "international",
        "global",
        "foreign company",
        "international client",
        "client international",
        "offshore",
        "chinese company",
        "شركة صينية",
        "شركة أجنبية",
        "عميل دولي",
        "مشاريع مع عميل دولي",
        "get global group",
        "petrolead"
    ]

    return any(
        marker.lower() in text_lower
        for marker in foreign_markers
    )


def has_libya_signal(
    text
):

    text_lower = (
        text or ""
    ).lower()

    markers = [

        "libya",
        "libyan",
        "libye",
        "ليبيا",
        "ليبي"
    ]

    if any(
        marker in text_lower
        for marker in markers
    ):

        return True

    for email in extract_emails(
        text
    ):

        domain = email_domain(
            email
        )

        if domain.endswith(
            ".ly"
        ):

            return True

    return False


def has_algeria_signal(
    text
):

    text_lower = (
        text or ""
    ).lower()

    markers = [

        "algeria",
        "algerian",
        "algérie",
        "algerie",
        "الجزائر",
        "وهران",
        "حاسي مسعود",
        "حاسي عامر",
        "بابا علي",
        "بجاية"
    ]

    if any(
        marker in text_lower
        for marker in markers
    ):

        return True

    for email in extract_emails(
        text
    ):

        domain = email_domain(
            email
        )

        if domain.endswith(
            ".dz"
        ):

            return True

    return False


def has_other_foreign_market_signal(
    text
):

    text_lower = (
        text or ""
    ).lower()

    markers = [

        "saudi arabia",
        "saudi",
        "uae",
        "united arab emirates",
        "qatar",
        "oman",
        "kuwait",
        "bahrain",
        "egypt",
        "iraq",
        "jeddah",
        "riyadh",
        "dubai",
        "abu dhabi",
        "الامارات",
        "الإمارات",
        "السعودية",
        "قطر",
        "عمان",
        "الكويت",
        "البحرين",
        "العراق",
        "مصر"
    ]

    return any(
        marker in text_lower
        for marker in markers
    )


def determine_application_language(
    advertisement,
    ai_result
):

    # --------------------------------------------------------
    # 1. Explicit employer language
    # --------------------------------------------------------

    explicit = detect_explicit_cv_language(
        advertisement
    )

    if explicit:

        return (
            explicit,
            "explicit employer instruction"
        )

    # --------------------------------------------------------
    # 2. Libya
    # --------------------------------------------------------

    if has_libya_signal(
        advertisement
    ):

        return (
            "en",
            "Libya / Libyan employer context"
        )

    # --------------------------------------------------------
    # 3. Foreign / international
    # --------------------------------------------------------

    if has_foreign_employer_signal(
        advertisement
    ):

        return (
            "en",
            "foreign or international employer context"
        )

    # --------------------------------------------------------
    # 4. Other foreign markets
    # --------------------------------------------------------

    if has_other_foreign_market_signal(
        advertisement
    ):

        return (
            "en",
            "foreign market context"
        )

    # --------------------------------------------------------
    # 5. Algerian local employer
    # --------------------------------------------------------

    if has_algeria_signal(
        advertisement
    ):

        return (
            "fr",
            "Algerian local employer context"
        )

    # --------------------------------------------------------
    # 6. AI fallback
    # --------------------------------------------------------

    jobs = []

    if isinstance(
        ai_result,
        dict
    ):

        jobs = ai_result.get(
            "jobs",
            []
        )

    ai_languages = []

    for job in jobs:

        if not isinstance(
            job,
            dict
        ):

            continue

        language = str(
            job.get(
                "language",
                ""
            )
        ).lower().strip()

        if language in {
            "fr",
            "en"
        }:

            ai_languages.append(
                language
            )

    if ai_languages:

        if all(
            language == "en"
            for language in ai_languages
        ):

            return (
                "en",
                "AI fallback"
            )

        if all(
            language == "fr"
            for language in ai_languages
        ):

            return (
                "fr",
                "AI fallback"
            )

    return (
        "fr",
        "default"
    )


# ============================================================
# TITLE HELPERS
# ============================================================

def looks_like_clean_application_title(
    title,
    language
):

    if not title:

        return False

    title = str(
        title
    ).strip()

    if (
        not title
        or contains_arabic(title)
        or len(title) < 2
    ):

        return False

    return True


def remove_arabic_parenthetical(
    title
):

    if not title:

        return title

    title = re.sub(
        r"\([^)]*[\u0600-\u06FF][^)]*\)",
        "",
        title
    )

    title = re.sub(
        r"\s*[-–—]\s*[\u0600-\u06FF][^\n]*$",
        "",
        title
    )

    return re.sub(
        r"\s{2,}",
        " ",
        title
    ).strip()


def extract_title_from_bilingual_source(
    original_title,
    language
):

    title = str(
        original_title or ""
    ).strip()

    if not title:

        return ""

    raw_parts = [

        part.strip()

        for part in title.split("|")

        if part.strip()
    ]

    non_arabic_parts = []

    for part in raw_parts:

        clean_part = remove_arabic_parenthetical(
            part
        ).strip()

        if (
            clean_part
            and not contains_arabic(clean_part)
        ):

            non_arabic_parts.append(
                clean_part
            )

    if non_arabic_parts:

        if language == "en":

            english_hints = [

                "technician",
                "engineer",
                "mechanic",
                "specialist",
                "operator",
                "manager",
                "supervisor",
                "foreman",
                "maintenance",
                "pump",
                "electrical",
                "instrument",
                "control",
                "planner",
                "site",
                "quality",
                "document",
                "catering",
                "chef",
                "assistant",
                "rigger",
                "scaffolder",
                "turbine",
                "piping",
                "pipe",
                "fitter",
                "roustabout",
                "deck",
                "fireproofing",
                "static",
                "rotating",
                "atex"
            ]

            for part in non_arabic_parts:

                lower = part.lower()

                if any(
                    hint in lower
                    for hint in english_hints
                ):

                    return part

        if language == "fr":

            french_hints = [

                "technicien",
                "ingenieur",
                "mecanicien",
                "specialiste",
                "operateur",
                "responsable",
                "superviseur",
                "maintenance",
                "pompes",
                "electrique",
                "instrumentation",
                "chaudronnerie",
                "peintre",
                "manutention",
                "mecanique"
            ]

            for part in non_arabic_parts:

                lower = normalize_text(
                    part
                )

                if any(
                    hint in lower
                    for hint in french_hints
                ):

                    return part

        if len(non_arabic_parts) == 1:

            return non_arabic_parts[0]

    # --------------------------------------------------------
    # Complete original title already clean
    # --------------------------------------------------------

    clean_full = remove_arabic_parenthetical(
        title
    )

    if looks_like_clean_application_title(
        clean_full,
        language
    ):

        return clean_full

    # --------------------------------------------------------
    # Safe fallback translations
    # --------------------------------------------------------

    lower_title = normalize_text(
        title
    )

    fallback_map = {

        "ميكانيكي مضخات":
            {
                "en": "Pump Mechanic",
                "fr": "Technicien Pompes"
            },

        "مكانيكي مضخات":
            {
                "en": "Pump Mechanic",
                "fr": "Technicien Pompes"
            },

        "تقني صيانة":
            {
                "en": "Maintenance Technician",
                "fr": "Technicien Maintenance"
            },

        "تقني صيانة إكمال الآبار":
            {
                "en": "Well Completion Maintenance Technician",
                "fr": "Technicien Maintenance Complétion"
            },

        "تقني صيانة الرافعات":
            {
                "en": "Crane Maintenance Technician",
                "fr": "Technicien Maintenance Grues"
            },

        "مكانيكي للآليات الثقيلة والخفيفة":
            {
                "en": "Heavy / Light Vehicle Mechanic",
                "fr": "Mécanicien Lourd / léger"
            },

        "ميكانيكي للآليات الثقيلة والخفيفة":
            {
                "en": "Heavy / Light Vehicle Mechanic",
                "fr": "Mécanicien Lourd / léger"
            },

        "عامل سطح / طاقم السطح":
            {
                "en": "Roustabout / Deck Crew",
                "fr": "Manœuvre / Équipe de Pont"
            },

        "تقني ميكانيك الآلات الدوارة":
            {
                "en": "Rotating Equipment Technician",
                "fr": "Technicien Mécanique Machines Tournantes"
            }
    }

    for source_title, translations in fallback_map.items():

        if normalize_text(
            source_title
        ) in lower_title:

            return translations.get(
                language,
                ""
            )

    return ""


def get_application_job_title(
    job,
    language
):

    original_title = str(
        job.get(
            "job_title",
            ""
        )
    ).strip()

    original_title_candidate = (
        extract_title_from_bilingual_source(
            original_title,
            language
        )
    )

    if original_title_candidate:

        return original_title_candidate

    field_name = (
        "application_title_en"
        if language == "en"
        else "application_title_fr"
    )

    ai_title = str(
        job.get(
            field_name,
            ""
        )
    ).strip()

    if looks_like_clean_application_title(
        ai_title,
        language
    ):

        return remove_arabic_parenthetical(
            ai_title
        )

    if looks_like_clean_application_title(
        original_title,
        language
    ):

        return remove_arabic_parenthetical(
            original_title
        )

    return (
        "Maintenance Technician"
        if language == "en"
        else "Technicien Maintenance"
    )


# ============================================================
# CV SELECTION
# ============================================================

def choose_cv(
    language
):

    cvs_dir = os.path.join(
        PROJECT_DIR,
        "cvs"
    )

    if language == "en":

        preferred = [

            "cv_ats_en.pdf",
            "cv_normal_en.pdf"
        ]

    else:

        preferred = [

            "cv_ats_fr.pdf",
            "cv_normal_fr.pdf"
        ]

    for filename in preferred:

        path = os.path.join(
            cvs_dir,
            filename
        )

        if os.path.exists(
            path
        ):

            return path

    return None



def cv_attachment_filename(cv_path):
    """Return a recruiter-friendly filename while keeping private storage names internal."""
    internal_name = os.path.basename(str(cv_path or "")).lower()
    filenames = {
        "cv_ats_en.pdf": "DJEZIRI_ABDELKADER_CV_ATS_EN.pdf",
        "cv_ats_fr.pdf": "DJEZIRI_ABDELKADER_CV_ATS_FR.pdf",
        "cv_normal_en.pdf": "DJEZIRI_ABDELKADER_CV_EN.pdf",
        "cv_normal_fr.pdf": "DJEZIRI_ABDELKADER_CV_FR.pdf",
    }
    return filenames.get(
        internal_name,
        os.path.basename(str(cv_path or "CV.pdf")) or "CV.pdf",
    )


# ============================================================
# SENT APPLICATIONS
# + JOB DEDUPLICATION
# ============================================================

def application_key(
    post,
    job,
    email
):

    return "|".join(
        [

            str(
                post.get(
                    "source",
                    ""
                )
            ),

            str(
                post.get(
                    "message_id",
                    ""
                )
            ),

            normalize_text(
                job.get(
                    "job_title",
                    ""
                )
            ),

            str(email)
            .lower()
            .strip()
        ]
    )


def load_sent_records():

    records = []

    if not os.path.exists(
        SENT_FILE
    ):

        return records

    with open(
        SENT_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            if not line.strip():
                continue

            try:

                records.append(
                    json.loads(
                        line
                    )
                )

            except Exception:

                continue

    return records


def already_sent(
    key
):

    for record in load_sent_records():

        if record.get(
            "key"
        ) == key:

            return True

    return False


def save_sent(
    key,
    data
):

    record = {
        "key": key,
        **data
    }
    # Record an explicit Algeria-local timestamp so daily reports don't infer
    # dates from the GitHub runner's UTC clock.
    record.setdefault(
        "sent_at",
        datetime.now(ZoneInfo("Africa/Algiers")).isoformat(timespec="seconds"),
    )

    with open(
        SENT_FILE,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            json.dumps(
                record,
                ensure_ascii=False
            ) + "\n"
        )


# ============================================================
# JOB IDENTITY / DUPLICATE DETECTION
# ============================================================

TITLE_CANONICAL_MAP = {

    # English / French

    "technicien":
        "technician",

    "technicienne":
        "technician",

    "techniciens":
        "technician",

    "techniciennes":
        "technician",

    "technicians":
        "technician",

    "technician":
        "technician",

    "maintenance":
        "maintenance",

    "maint":
        "maintenance",

    "mecanicien":
        "mechanic",

    "mécanicien":
        "mechanic",

    "mecaniciens":
        "mechanic",

    "mecaniciennes":
        "mechanic",

    "mechanics":
        "mechanic",

    "mechanic":
        "mechanic",

    "mecanique":
        "mechanical",

    "mécanique":
        "mechanical",

    "mechanical":
        "mechanical",

    "pompe":
        "pump",

    "pompes":
        "pump",

    "pump":
        "pump",

    "pumps":
        "pump",

    "operateur":
        "operator",

    "opérateur":
        "operator",

    "operator":
        "operator",

    "ingenieur":
        "engineer",

    "ingénieur":
        "engineer",

    "engineer":
        "engineer",

    "superviseur":
        "supervisor",

    "supervisor":
        "supervisor",

    "responsable":
        "responsible",

    "manager":
        "manager",

    "chef":
        "leader",

    "team":
        "team",

    "equipe":
        "team",

    "équipe":
        "team",

    "assistant":
        "assistant",

    "assistante":
        "assistant",

    "electrique":
        "electrical",

    "électrique":
        "electrical",

    "electrical":
        "electrical",

    "electricien":
        "electrician",

    "electricienne":
        "electrician",

    "electriciens":
        "electrician",

    "electriciennes":
        "electrician",

    "electrician":
        "electrician",

    "electricians":
        "electrician",

    "automaticien":
        "automation",

    "automation":
        "automation",

    "instrumentation":
        "instrumentation",

    "instrument":
        "instrumentation",

    "rotating":
        "rotating",

    "tournantes":
        "rotating",

    "machine":
        "machine",

    "machines":
        "machine",

    "cng":
        "cng",

    "cnc":
        "cnc",

    "piping":
        "piping",

    "pipe":
        "pipe",

    "fitter":
        "fitter",

    "planner":
        "planner",

    "quality":
        "quality",

    "document":
        "document",

    "documents":
        "document",

    "rigger":
        "rigger",

    "scaffolder":
        "scaffolder",

    "roustabout":
        "roustabout",

    "deck":
        "deck",

    "static":
        "static",

    "turbine":
        "turbine",

    # Arabic

    "تقني":
        "technician",

    "فني":
        "technician",

    "تقنية":
        "technician",

    "صيانة":
        "maintenance",

    "ميكانيكي":
        "mechanic",

    "ميكانيكيين":
        "mechanic",

    "ميكانيكيون":
        "mechanic",

    "مكانيكي":
        "mechanic",

    "ميكانيكا":
        "mechanical",

    "مضخات":
        "pump",

    "مضخه":
        "pump",

    "مضخة":
        "pump",

    "مهندس":
        "engineer",

    "مشرف":
        "supervisor",

    "مشغل":
        "operator",

    "كهربائي":
        "electrical",

    "كهربائيين":
        "electrical",

    "كهربائيون":
        "electrical",

    "كهرباء":
        "electrical",

    "آلي":
        "automation",

    "آليات":
        "vehicle",

    "معدات":
        "equipment",

    "معدني":
        "metal",

    "دوارة":
        "rotating",

    "رافعات":
        "crane",

    "رافعة":
        "crane",

    "فنيين":
        "technician",

    "تقنيين":
        "technician",

    "مساعد":
        "assistant"
}


SENIORITY_TOKENS = {

    "senior",
    "expert",
    "specialist",
    "manager",
    "supervisor",
    "foreman",
    "chief",
    "head",
    "leader",
    "responsable",
    "superviseur",
    "chef"
}


def canonical_title_tokens(
    title
):

    normalized = normalize_text(
        title
    )

    for source, target in TITLE_CANONICAL_MAP.items():

        normalized = re.sub(
            rf"(?<!\w){re.escape(normalize_text(source))}(?!\w)",
            target,
            normalized
        )

    stop_words = {

        "the",
        "and",
        "of",
        "de",
        "du",
        "des",
        "la",
        "le",
        "les",
        "un",
        "une",
        "for",
        "to",
        "en",
        "a",
        "au",
        "aux"
    }

    tokens = {

        token

        for token in normalized.split()

        if (
            token not in stop_words
            and len(token) > 1
        )
    }

    return tokens


def title_has_seniority_difference(
    title_a,
    title_b
):

    a = canonical_title_tokens(
        title_a
    )

    b = canonical_title_tokens(
        title_b
    )

    senior_a = a.intersection(
        SENIORITY_TOKENS
    )

    senior_b = b.intersection(
        SENIORITY_TOKENS
    )

    if bool(senior_a) != bool(senior_b):

        return True

    if (
        senior_a != senior_b
        and (senior_a or senior_b)
    ):

        return True

    return False


def title_similarity(
    title_a,
    title_b
):

    a_tokens = canonical_title_tokens(
        title_a
    )

    b_tokens = canonical_title_tokens(
        title_b
    )

    if not a_tokens or not b_tokens:

        return 0.0

    jaccard = (
        len(a_tokens & b_tokens)
        /
        len(a_tokens | b_tokens)
    )

    a_string = " ".join(
        sorted(a_tokens)
    )

    b_string = " ".join(
        sorted(b_tokens)
    )

    sequence = SequenceMatcher(
        None,
        a_string,
        b_string
    ).ratio()

    return max(
        jaccard,
        sequence
    )


def normalize_company(
    company
):

    return normalize_text(
        company
    )


def normalize_location(
    location
):

    return normalize_text(
        location
    )


def job_identity_key(
    job,
    application_title,
    email
):

    company = normalize_company(
        job.get(
            "company",
            ""
        )
    )

    location = normalize_location(
        job.get(
            "location",
            ""
        )
    )

    title_tokens = ",".join(
        sorted(
            canonical_title_tokens(
                application_title
            )
        )
    )

    domain = email_domain(
        email
    )

    return "|".join(
        [
            company,
            domain,
            location,
            title_tokens
        ]
    )


def same_job_identity(
    current_job,
    current_title,
    current_email,
    old_record
):

    old_title = str(
        old_record.get(
            "application_job_title",
            old_record.get(
                "job_title",
                ""
            )
        )
    ).strip()

    if not old_title:

        return False

    if title_has_seniority_difference(
        current_title,
        old_title
    ):

        return False

    similarity = title_similarity(
        current_title,
        old_title
    )

    current_company = normalize_company(
        current_job.get(
            "company",
            ""
        )
    )

    old_company = normalize_company(
        old_record.get(
            "company",
            ""
        )
    )

    current_location = normalize_location(
        current_job.get(
            "location",
            ""
        )
    )

    old_location = normalize_location(
        old_record.get(
            "location",
            ""
        )
    )

    current_email = str(
        current_email or ""
    ).lower().strip()

    old_email = str(
        old_record.get(
            "email",
            ""
        )
    ).lower().strip()

    same_email = (
        bool(current_email)
        and bool(old_email)
        and current_email == old_email
    )

    current_domain = email_domain(
        current_email
    )

    old_domain = email_domain(
        old_email
    )

    same_domain = (

        bool(current_domain)

        and bool(old_domain)

        and current_domain == old_domain

        and current_domain not in {
            "",
            "gmail.com",
            "hotmail.com",
            "outlook.com",
            "yahoo.com"
        }
    )

    same_company = (

        bool(current_company)

        and bool(old_company)

        and current_company == old_company
    )

    location_compatible = (

        not current_location

        or not old_location

        or current_location == old_location

        or current_location in old_location

        or old_location in current_location
    )

    # --------------------------------------------------------
    # Exact identity
    #
    # IMPORTANT:
    # Do not use an identity made only of empty fields + title.
    # That could incorrectly merge unrelated jobs with same title.
    # --------------------------------------------------------

    old_identity = str(
        old_record.get(
            "identity_key",
            ""
        )
    ).strip()

    current_identity = job_identity_key(
        current_job,
        current_title,
        current_email
    )

    identity_has_anchor = (
        bool(current_company)
        or bool(current_email)
        or bool(current_location)
    )

    if (
        identity_has_anchor
        and old_identity
        and current_identity == old_identity
    ):

        return True

    # --------------------------------------------------------
    # Same email + strong title similarity, provided the known employer
    # and location do not identify a different vacancy.
    # Public/recruitment inboxes are often reused across employers.
    # --------------------------------------------------------

    companies_conflict = bool(
        current_company and old_company and current_company != old_company
    )

    if (
        same_email
        and not companies_conflict
        and location_compatible
        and similarity >= 0.82
    ):

        return True

    # --------------------------------------------------------
    # Same company + compatible location +
    # strong title similarity
    # --------------------------------------------------------

    if (
        same_company
        and location_compatible
        and similarity >= 0.82
    ):

        return True

    # --------------------------------------------------------
    # Same company/email domain +
    # strong title similarity
    # --------------------------------------------------------

    if (
        (same_email or same_domain)
        and not companies_conflict
        and location_compatible
        and similarity >= 0.82
    ):

        return True

    return False


# Cross-posts are treated as duplicates only for a short window. A genuinely
# new repost after this window is a new opportunity, even if the role and email
# are unchanged. Exact source/message application keys remain permanent guards.
JOB_DUPLICATE_WINDOW_HOURS = 48


def _sent_record_is_recent(record, now=None):
    """Only timestamped sends from the recent duplicate window can fuzzy-block a repost."""
    raw_timestamp = str(record.get("sent_at") or "").strip()
    if not raw_timestamp:
        # Legacy history without a timestamp cannot prove when the application
        # was sent, so it must not suppress a newly published post forever.
        return False

    try:
        sent_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False

    # Treat timezone-less/invalid legacy timestamps as unknown rather than
    # guessing a date and blocking a new opportunity.
    if sent_at.tzinfo is None or sent_at.utcoffset() is None:
        return False

    if now is None:
        now = datetime.now(ZoneInfo("Africa/Algiers"))
    elif now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=ZoneInfo("Africa/Algiers"))

    age_hours = (now - sent_at.astimezone(now.tzinfo)).total_seconds() / 3600
    return 0 <= age_hours <= JOB_DUPLICATE_WINDOW_HOURS


def already_sent_same_job(
    current_job,
    current_title,
    emails
):

    sent_records = load_sent_records()
    now = datetime.now(ZoneInfo("Africa/Algiers"))

    for old_record in sent_records:

        if not isinstance(old_record, dict) or not _sent_record_is_recent(old_record, now):
            continue

        old_email = old_record.get(
            "email",
            ""
        )

        candidate_emails = emails or [
            old_email
        ]

        for email in candidate_emails:

            if same_job_identity(
                current_job,
                current_title,
                email,
                old_record
            ):

                return True, old_record

    return False, None


# ============================================================
# AI SELECTS FACT IDS ONLY
# ============================================================

def extract_email_matches(
    job,
    advertisement
):

    job_title = job.get(
        "job_title",
        "Job Application"
    )

    facts_description = """

AVAILABLE VERIFIED FACTS

1. EDUCATION_MECHANICAL_ENERGY

Evidence: education

French:
Licence en Génie Mécanique et Énergétique

English:
Bachelor's degree in Mechanical and Energy Engineering


2. KNOWLEDGE_MECHANICAL_SYSTEMS

Evidence: knowledge

French:
knowledge of mechanical systems

English:
knowledge of mechanical systems


3. KNOWLEDGE_INDUSTRIAL_MAINTENANCE

Evidence: knowledge

French:
knowledge of industrial maintenance

English:
knowledge of industrial maintenance


4. KNOWLEDGE_MECHANICAL_EQUIPMENT

Evidence: knowledge

French:
knowledge of mechanical equipment

English:
knowledge of mechanical equipment


5. KNOWLEDGE_CENTRIFUGAL_PUMPS

Evidence: knowledge

French:
knowledge of centrifugal pumps

English:
knowledge of centrifugal pumps


6. KNOWLEDGE_FLUID_SYSTEMS

Evidence: knowledge

French:
knowledge of fluid systems

English:
knowledge of fluid systems


7. KNOWLEDGE_TECHNICAL_DRAWINGS

Evidence: knowledge

French:
knowledge of technical drawing interpretation

English:
knowledge of technical drawings


8. KNOWLEDGE_TROUBLESHOOTING

Evidence: knowledge

French:
basic knowledge of mechanical troubleshooting

English:
basic knowledge of mechanical troubleshooting


9. KNOWLEDGE_TECHNICAL_DOCUMENTATION

Evidence: knowledge

French:
knowledge of technical documentation

English:
knowledge of technical documentation


10. TRAINING_SOLIDWORKS

Evidence: training

French:
training in mechanical design with SolidWorks

English:
training in mechanical design with SolidWorks


11. KNOWLEDGE_SOLIDWORKS

Evidence: knowledge

French:
knowledge of SolidWorks

English:
knowledge of SolidWorks
"""

    prompt = """

You are a recruitment matching AI.

DO NOT write an email.

DO NOT write candidate sentences.

DO NOT rewrite facts.

Your ONLY task is to select the IDs of verified candidate facts that
genuinely match the job advertisement.

JOB TITLE:

""" + job_title + """

JOB ADVERTISEMENT:

""" + advertisement + """

REAL CANDIDATE:

""" + CANDIDATE + """

""" + facts_description + """

============================================================
STRICT RULES
============================================================

1. Return a maximum of 3 fact IDs.

2. Select only facts that are genuinely relevant to THIS position.

3. Do not select a fact merely because it exists in the profile.

4. Do not invent a new fact.

5. Do not modify a fact.

6. Do not upgrade knowledge into experience.

7. Do not upgrade training into professional experience.

8. Do not upgrade knowledge into mastery.

9. The candidate has no professional engineering experience.

10. Do not call the candidate a fresh graduate or recent graduate.

11. SolidWorks only if the position genuinely involves mechanical
design, drafting, CAD, bureau d'études or similar work.

12. Centrifugal pumps only when pumps or fluid equipment are relevant.

13. Technical drawings only when drawings, plans, engineering
documentation or similar requirements are relevant.

14. For unrelated positions return an empty list.

15. One strong match is better than three weak matches.

16. EDUCATION_MECHANICAL_ENERGY is valid when the job genuinely accepts
mechanical/energy engineering education.

17. Do not select facts merely because the advertisement mentions those
skills as employer requirements.

============================================================
OUTPUT
============================================================

Return ONLY valid JSON:

{
  "fact_ids": [
    "FACT_ID"
  ]
}
"""

    messages = [

        {
            "role": "system",
            "content": (
                "Select only valid fact IDs. "
                "Never write email text. "
                "Never invent facts. "
                "Never call the candidate a fresh graduate or recent "
                "graduate. "
                "Return only JSON."
            )
        },

        {
            "role": "user",
            "content": prompt
        }

    ]

    try:

        result = groq_json_request(
            messages=messages,
            temperature=0,
            max_completion_tokens=500
        )

    except Exception as e:

        print(
            "EMAIL MATCH JSON ERROR:",
            repr(e)
        )

        return []

    fact_ids = result.get(
        "fact_ids",
        []
    )

    if not isinstance(
        fact_ids,
        list
    ):

        return []

    clean_ids = []

    for fact_id in fact_ids:

        fact_id = str(
            fact_id
        ).strip()

        if (
            fact_id in VALID_FACT_IDS
            and fact_id not in clean_ids
        ):

            clean_ids.append(
                fact_id
            )

    return clean_ids[:3]


# ============================================================
# NATURAL EMAIL BUILDING
#
# No AI-generated email.
# Only verified facts + safe fixed wording.
# ============================================================

def stable_template_index(
    job_title
):

    value = sum(
        ord(char)
        for char in str(job_title)
    )

    return value % 3


def build_email(
    job,
    language,
    advertisement,
    application_title=None
):

    if not application_title:

        application_title = (
            get_application_job_title(
                job,
                language
            )
        )

    job_title = str(
        application_title
    ).strip()

    fact_ids = extract_email_matches(
        job,
        advertisement
    )

    print(
        "SELECTED FACT IDS:",
        fact_ids
    )

    selected_facts = []

    for fact_id in fact_ids:

        fact = FACTS.get(
            fact_id
        )

        if fact:

            selected_facts.append(
                fact
            )

    selected_facts = selected_facts[:2]

    if not selected_facts:

        selected_facts = [

            FACTS[
                "EDUCATION_MECHANICAL_ENERGY"
            ]

        ]

    template_index = stable_template_index(
        job_title
    )

    # ========================================================
    # ENGLISH
    # ========================================================

    if language == "en":

        subject = (
            f"Application – {job_title}"
        )

        openings = [

            (
                f"I would like to submit my application "
                f"for the {job_title} position."
            ),

            (
                f"I am writing to apply for the "
                f"{job_title} position."
            ),

            (
                f"I would like to be considered for the "
                f"{job_title} position."
            )
        ]

        bridges = [

            (
                "I am looking to put these technical "
                "foundations into practice in a professional "
                "environment."
            ),

            (
                "I would like to apply these technical "
                "foundations in a professional setting and "
                "continue developing my experience."
            ),

            (
                "I am seeking an opportunity to put my "
                "technical knowledge into practice in an "
                "industrial environment."
            )
        ]

        attachments = [

            "Please find my CV attached for your consideration.",

            "I have attached my CV for your review.",

            "My CV is attached for your consideration."
        ]

        lines = [

            "Hello,",
            "",
            openings[template_index],
            ""
        ]

        for fact in selected_facts:

            lines.append(
                fact["en"] + "."
            )

        lines.extend(
            [

                "",
                bridges[template_index],
                "",
                attachments[template_index],
                "",
                "Best regards,",
                "",
                CANDIDATE_NAME
            ]
        )

    # ========================================================
    # FRENCH
    # ========================================================

    else:

        subject = (
            f"Candidature - {job_title}"
        )

        openings = [

            (
                f"Je souhaite vous adresser ma candidature "
                f"pour le poste de {job_title}."
            ),

            (
                f"Je vous contacte au sujet du poste de "
                f"{job_title} et souhaite vous soumettre "
                f"ma candidature."
            ),

            (
                f"Je souhaite proposer ma candidature au "
                f"poste de {job_title}."
            )
        ]

        bridges = [

            (
                "Je souhaite aujourd’hui mettre ces "
                "connaissances en pratique dans un "
                "environnement professionnel."
            ),

            (
                "Je souhaite mettre ces bases techniques "
                "en pratique et continuer à développer "
                "mon expérience dans un environnement "
                "professionnel."
            ),

            (
                "Je recherche une opportunité me permettant "
                "de mettre mes connaissances techniques "
                "en pratique dans un environnement industriel."
            )
        ]

        attachments = [

            (
                "Vous trouverez mon CV en pièce jointe "
                "pour examen de ma candidature."
            ),

            (
                "Je joins mon CV à ce message pour vous "
                "permettre d’examiner ma candidature."
            ),

            (
                "Mon CV est joint à ce message pour "
                "examen de ma candidature."
            )
        ]

        lines = [

            "Bonjour,",
            "",
            openings[template_index],
            ""
        ]

        for fact in selected_facts:

            lines.append(
                fact["fr"] + "."
            )

        lines.extend(
            [

                "",
                bridges[template_index],
                "",
                attachments[template_index],
                "",
                "Cordialement,",
                "",
                CANDIDATE_NAME
            ]
        )

    body = "\n".join(
        lines
    )

    return (
        subject,
        body
    )


# ============================================================
# GMAIL SEND
#
# ORIGINAL WORKING METHOD
# SMTP port 587
# STARTTLS
# NEW CONNECTION FOR EACH EMAIL
# ============================================================

def send_application(
    email_address,
    job,
    cv_path,
    language,
    advertisement,
    application_title
):

    subject, body = build_email(
        job,
        language,
        advertisement,
        application_title
    )

    message = EmailMessage()

    message["From"] = GMAIL_ADDRESS
    message["To"] = email_address
    message["Subject"] = subject

    message.set_content(
        body
    )

    with open(
        cv_path,
        "rb"
    ) as f:

        cv_data = f.read()

    message.add_attachment(
        cv_data,
        maintype="application",
        subtype="pdf",
        filename=cv_attachment_filename(
            cv_path
        )
    )

    print("Sending application (recipient and subject hidden in public logs)")

    server = None
    accepted = False

    try:

        server = smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30
        )

        server.ehlo()

        server.starttls()

        server.ehlo()

        server.login(
            GMAIL_ADDRESS,
            GMAIL_APP_PASSWORD
        )

        refused = server.send_message(
            message
        )

        if refused:

            raise smtplib.SMTPRecipientsRefused(
                refused
            )

        accepted = True

        print(
            "EMAIL SENT OK"
        )

    except smtplib.SMTPResponseException as e:

        if e.smtp_code == 250:

            accepted = True

            print(
                "EMAIL SENT OK"
            )

        else:

            raise

    finally:

        if server is not None:
            # A successful send_message() response (SMTP 250) confirms acceptance.
            # QUIT is only session cleanup; letting QUIT fail after acceptance makes
            # the caller think sending failed and can trigger a duplicate application.
            try:
                server.quit()
            except Exception:
                pass

    if not accepted:

        raise RuntimeError(
            "Email was not confirmed as sent"
        )


# ============================================================
# SAVE RESULT
# ============================================================

def save_result(
    data
):

    with open(
        RESULTS_FILE,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            json.dumps(
                data,
                ensure_ascii=False
            ) + "\n"
        )


def log_job_result_safely(record, contact_review_reason=None):
    """Write useful diagnostics without exposing job or candidate details in public Actions logs."""
    print("JOB RESULT: identifying details redacted from public workflow logs")
    print("DECISION:", record.get("decision"))
    print("ORIGINAL AI DECISION:", record.get("original_ai_decision"))
    print("SCORE:", record.get("score"))
    print("LOCATION ELIGIBILITY:", record.get("location_status"))
    print("APPLICATION LANGUAGE:", record.get("application_language"))
    print("REQUESTED CV LANGUAGE:", record.get("application_cv_language"))
    print("SCOPED EMAIL COUNT:", len(record.get("emails") or []))
    print("JOB DUPLICATE:", bool(record.get("job_duplicate")))
    print("SENT:", bool(record.get("sent")))
    if contact_review_reason:
        print("CONTACT REVIEW: present (details redacted)")


# ============================================================
# A generic group heading or career slogan such as "عروض عمل" / "فرصة عمل أفضل"
# is not proof that this post itself contains an available vacancy. The fallback
# to manual review should trigger only on explicit hiring intent or vacancy wording.
VACANCY_SIGNAL_RE = re.compile(
    r"(?:"
    r"offre\s+d['’ ]?emploi|offre\s+de\s+recrutement|avis\s+de\s+recrutement|"
    r"nous\s+recrutons|recrutons\s+(?:un|une|des)|we\s+are\s+hiring|"
    r"job\s+opening|vacanc(?:y|ies)|position[s]?\s+à\s+pourvoir|poste[s]?\s+à\s+pourvoir|"
    r"profil\s+recherché|"
    r"إعلان\s+توظيف|اعلان\s+توظيف|عرض\s+عمل|وظيفة\s+شاغرة|وظائف\s+شاغرة|"
    r"فتح\s+باب\s+التوظيف|"
    r"نبحث\s+عن\s+.{0,40}(?:تقني|مهندس|عامل|موظف|منصب)|"
    r"مطلوب\s+(?:تقني|مهندس|عامل|موظف|سائق|ميكانيكي|فني|محاسب)|"
    r"تعلن.{0,80}(?:توظيف|منصب|مناصب|عمال|عامل|موظف|تقني|مهندس)"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)


def looks_like_vacancy_ad(text):
    """Require actual hiring intent, not a teaser or generic job-opportunity slogan."""
    return bool(VACANCY_SIGNAL_RE.search(str(text or "")))


_SOCIAL_PRAYER_SIGNAL_RE = re.compile(
    r"(?:اللهم|دعواتكم|ربي.{0,10}يسمع|خبر.{0,10}الخير)",
    flags=re.IGNORECASE | re.DOTALL,
)

_MIGRATION_DISTRESS_SIGNAL_RE = re.compile(
    r"(?:قوارب.{0,12}الهجرة|الهجرة.{0,10}غير.{0,10}الشرعية|"
    r"خرجو.{0,30}قوارب|خرجوا.{0,30}قوارب|مفقودين|"
    r"ما.{0,3}زال.{0,50}(?:بان|ظهر)|عائلاتهم.{0,50}(?:خبر|دعوات))",
    flags=re.IGNORECASE | re.DOTALL,
)

# Do not treat a generic channel/header phrase such as "عروض عمل" as proof
# that the message itself is a vacancy. Strong details take precedence.
_EXPLICIT_JOB_DETAIL_RE = re.compile(
    r"(?:recrutement|recrute|recrutons|job\s+offer|job\s+vacanc(?:y|ies)|"
    r"we\s+are\s+hiring|hiring|postuler|candidature|envoyer|send\s+(?:your\s+)?(?:cv|resume)|"
    r"apply\s+now|curriculum\s+vitae|\bcv\b|\be-?mail\b|"
    r"إعلان\s+توظيف|اعلان\s+توظيف|توظيف|مطلوب(?:ة|ين)?|منصب|مناصب|وظيفة|"
    r"وظائف\s+شاغرة|سيرة\s+ذاتية|الترشح|التقديم|أرسل(?:وا)?|إرسال|"
    r"technicien|technician|maintenance|m[ée]canicien|m[ée]canique|"
    r"ing[ée]nieur|engineer|op[ée]rateur|production|electricien|électricien|"
    r"soudeur|magasinier|superviseur|supervisor|commercial|"
    r"مهندس|تقني|فني|ميكانيكي|صيانة|إنتاج|عامل|موظف|مشرف|مسؤول)",
    flags=re.IGNORECASE | re.DOTALL,
)


# Service advertisements can mention "Recrutement", CVs, and new opportunities
# without hiring anyone. Require BOTH a CV/career-service signal and a marketing
# call-to-action, while protecting explicit vacancy/hiring language.
_SERVICE_CV_SIGNAL_RE = re.compile(
    r"(?:\bcv\s+(?:professionnel(?:le)?|pro|احترافي(?:ة)?)\b|"
    r"(?:إعداد|كتابة|تصميم|تحسين|تطوير)\s*(?:الـ?cv|السيرة\s+الذاتية|سيرة\s+ذاتية)|"
    r"(?:تطوير|تحسين|إعداد)\s+ملفك?\s+المهني|"
    r"(?:création|rédaction|optimisation|refonte)\s+(?:de\s+)?cv|"
    r"resume\s+writing|resume\s+service|career\s+coaching|"
    r"job\s+search\s+(?:support|assistance))",
    flags=re.IGNORECASE | re.DOTALL,
)

_SERVICE_PROMOTION_SIGNAL_RE = re.compile(
    r"(?:خدماتنا|نرافقوك|نرافقك|نساعدك|تواصل\s+معنا|اكتشف\s+خدمات|"
    r"حاب\s+تطور|طو[ّ]?ر\s+من\s+نفسك|استثمر\s+في\s+مستقبلك|"
    r"nous\s+vous\s+accompagnons|nos\s+services|d[ée]couvrez\s+(?:nos\s+)?services|"
    r"contactez[- ]nous|we\s+help\s+you|our\s+services|"
    r"discover\s+our\s+services|contact\s+us)",
    flags=re.IGNORECASE | re.DOTALL,
)

_ACTUAL_HIRING_INTENT_RE = re.compile(
    r"(?:offre\s+d['’ ]?emploi|offre\s+de\s+recrutement|avis\s+de\s+recrutement|"
    r"nous\s+recrutons|recrutons\s+(?:un|une|des)|we\s+are\s+hiring|"
    r"job\s+opening|vacanc(?:y|ies)|position[s]?\s+à\s+pourvoir|poste[s]?\s+à\s+pourvoir|"
    r"profil\s+recherché|"
    r"إعلان\s+توظيف|اعلان\s+توظيف|عرض\s+عمل|وظيفة\s+شاغرة|وظائف\s+شاغرة|"
    r"نبحث\s+عن\s+.{0,40}(?:تقني|مهندس|عامل|موظف|منصب)|"
    r"مطلوب\s+(?:تقني|مهندس|عامل|موظف|سائق|ميكانيكي|فني|محاسب))",
    flags=re.IGNORECASE | re.DOTALL,
)


def looks_like_non_job_service_promotion(text):
    """Detect CV/career-service marketing posts without mistaking real vacancies."""
    body = str(text or "").strip()
    if not body:
        return False
    if _ACTUAL_HIRING_INTENT_RE.search(body):
        return False
    return bool(
        _SERVICE_CV_SIGNAL_RE.search(body)
        and _SERVICE_PROMOTION_SIGNAL_RE.search(body)
    )


_FUTURE_JOB_POST_TEASER_RE = re.compile(
    r"(?:"
    r"(?:رايح|راح|راني|رح|سوف|سأ|سن)\s*"
    r"(?:نحط|ننشر|نشر|نزل|نهبط|نشارك|نضع|نقدم|نمد).{0,80}"
    r"(?:عروض\s+عمل|عروض\s+التوظيف|فرص\s+عمل|وظائف|مناصب|إعلانات?\s+توظيف)|"
    r"(?:going\s+to|we\s+will|i['’]?ll)\s+"
    r"(?:post|publish|share).{0,80}(?:job\s+offers|vacancies|openings)"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)

_CONCRETE_JOB_DETAIL_RE = re.compile(
    r"(?:\btechnicien\b|\btechnician\b|\bmaintenance\b|"
    r"m[ée]canicien|ing[ée]nieur|\bengineer\b|op[ée]rateur|\boperator\b|"
    r"production|electricien|électricien|soudeur|magasinier|superviseur|supervisor|"
    r"commercial|مهندس|تقني|فني|ميكانيكي|صيانة|إنتاج|عامل|موظف|مشرف|مسؤول|سائق|محاسب|"
    r"postuler|candidature|apply\s+now|"
    r"envoy(?:er|ez).{0,30}\b(?:cv|resume)\b|send.{0,30}\b(?:cv|resume)\b|"
    r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b)",
    flags=re.IGNORECASE | re.DOTALL,
)


def looks_like_non_job_job_teaser(text):
    """Detect a promise to post job offers later when no concrete vacancy details exist."""
    body = str(text or "").strip()
    if not body or not _FUTURE_JOB_POST_TEASER_RE.search(body):
        return False
    return not bool(_CONCRETE_JOB_DETAIL_RE.search(body))


def looks_like_non_job_social_post(text):
    """Detect high-confidence social/distress posts without actual job details."""
    body = str(text or "").strip()
    if not body:
        return False
    if _EXPLICIT_JOB_DETAIL_RE.search(body):
        return False
    return bool(
        _SOCIAL_PRAYER_SIGNAL_RE.search(body)
        and _MIGRATION_DISTRESS_SIGNAL_RE.search(body)
    )


def emit_unparsed_vacancy_review(post, advertisement):
    """Keep a recruitment-like post visible for manual review; never auto-send it."""
    title = "إعلان توظيف يحتاج إلى مراجعة يدوية"
    source = str(post.get("source") or post.get("channel") or "")
    emails = extract_emails(advertisement)
    record = {
        "source": source,
        "channel": source,
        "message_id": post.get("message_id"),
        "job_title": title,
        "application_job_title": title,
        "application_title_fr": "Annonce de recrutement à vérifier manuellement",
        "application_title_en": "Recruitment advertisement requiring manual review",
        "company": "",
        "location": "",
        "location_status": "unknown_location",
        "location_reason": "The advertisement was not parsed reliably.",
        "accommodation_provided": "unknown",
        "language": "mixed",
        "application_language": "fr",
        "application_language_reason": "Manual review only; no automatic application.",
        "application_cv_language": "",
        "cv": "",
        "emails": emails,
        "decision": "REVIEW",
        "original_ai_decision": "REVIEW",
        "score": None,
        "reason": (
            "تعذّر استخراج المناصب آليًا رغم وجود مؤشرات توظيف. "
            "أُحيل الإعلان للمراجعة اليدوية؛ لن يُرسل أي بريد تلقائي من هذه البطاقة."
        ),
        "sent": False,
        "job_duplicate": False,
        "contact_scope_matched": False,
        "analysis_fallback": True,
    }
    save_result(record)
    if JOB_EVENT_CALLBACK is None:
        print("VACANCY EXTRACTION FALLBACK: callback unavailable; post will retry")
        return False
    event = {
        "post": post,
        "record": record,
        "job": {"job_title": title, "company": "", "location": ""},
        "advertisement": str(advertisement or ""),
    }
    try:
        JOB_EVENT_CALLBACK(event)
    except Exception as exc:
        print("VACANCY EXTRACTION FALLBACK CALLBACK ERROR:", type(exc).__name__)
        return False
    if JOB_DECISION_AUDIT_CALLBACK is not None:
        try:
            JOB_DECISION_AUDIT_CALLBACK(event)
        except Exception as exc:
            print("DECISION AUDIT ERROR:", type(exc).__name__, "; post will retry")
            return False
    print("VACANCY EXTRACTION FALLBACK: queued for manual review; automatic send disabled")
    return True


# PROCESS ONE POST
# ============================================================

def process_post(
    post
):

    text = post.get(
        "text",
        ""
    )

    if not str(
        text
    ).strip():

        return True

    if looks_like_non_job_service_promotion(text):
        print("NON-JOB SERVICE PROMOTION FILTER: CV/career-service advertisement; ignored")
        return True

    if looks_like_non_job_job_teaser(text):
        print("NON-JOB FUTURE-POST TEASER FILTER: offers are announced for later; no current vacancy details")
        return True

    if looks_like_non_job_social_post(text):
        print("NON-JOB SOCIAL POST FILTER: obvious prayer/migration-distress content; ignored")
        return True

    print(
        "\nAnalyzing new job post..."
    )

    try:

        result = analyze_offer(
            text
        )

    except Exception as e:

        print(
            "AI ERROR:",
            type(e).__name__
        )

        return False

    if not isinstance(result, dict):
        print("AI ERROR: analysis response was not a JSON object")
        return False
    jobs = result.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []
    jobs = [job for job in jobs if isinstance(job, dict)]
    listed_role_titles = extract_compact_listed_role_titles(text)
    vacancy_rescue_attempted = False

    # Split independently headed notices before assigning contact details.
    job_sections = split_advertisement_sections(text)
    has_multiple_sections = len(job_sections) > 1
    print(f"Detected {len(job_sections)} recruitment section(s)")

    # Schema-valid empty output can silently drop a real job ad. Retry once
    # with an explicit extraction prompt; retain short title lists for review if
    # the model still omits their positions.
    if not jobs and looks_like_vacancy_ad(text):
        print("AI EMPTY EXTRACTION: vacancy signals found; retrying extraction once")
        vacancy_rescue_attempted = True
        try:
            retry_result = analyze_offer(text, vacancy_rescue=True)
            retry_jobs = retry_result.get("jobs", []) if isinstance(retry_result, dict) else []
            if isinstance(retry_jobs, list):
                retry_jobs = [job for job in retry_jobs if isinstance(job, dict)]
            else:
                retry_jobs = []
        except Exception as exc:
            print("AI VACANCY RESCUE ERROR:", type(exc).__name__)
            if isinstance(exc, AIProviderUnavailable):
                print("AI PROVIDER UNAVAILABLE: preserving this post for a later retry")
                return False
            retry_jobs = []

        if retry_jobs:
            result = retry_result
            jobs = retry_jobs
            print(f"AI VACANCY RESCUE: extracted {len(jobs)} position(s)")
        elif not listed_role_titles:
            # Do not discard the post silently, and never automatically apply
            # using a synthetic/uncertain job record.
            return emit_unparsed_vacancy_review(post, text)
        else:
            print(
                f"COMPACT ROLE LIST RECOVERY: found {len(listed_role_titles)} separate title lines; "
                "will preserve omitted titles as review-only records"
            )

    if listed_role_titles:
        def _coverage(candidates):
            return sum(
                1 for title in listed_role_titles
                if any(_compact_list_title_matches(title, candidate) for candidate in candidates)
            )

        covered_before = _coverage(jobs)
        if covered_before < len(listed_role_titles) and not vacancy_rescue_attempted:
            print(
                f"AI UNDER-EXTRACTION: matched {covered_before}/{len(listed_role_titles)} listed titles; "
                "retrying multi-position extraction once"
            )
            vacancy_rescue_attempted = True
            try:
                retry_result = analyze_offer(text, vacancy_rescue=True)
                retry_jobs = retry_result.get("jobs", []) if isinstance(retry_result, dict) else []
                if not isinstance(retry_jobs, list):
                    retry_jobs = []
                retry_jobs = [job for job in retry_jobs if isinstance(job, dict)]
                retry_coverage = _coverage(retry_jobs)
                if retry_coverage > covered_before:
                    result = retry_result
                    jobs = retry_jobs
                    covered_before = retry_coverage
                    print(f"AI MULTI-POSITION RESCUE: matched {covered_before}/{len(listed_role_titles)} titles")
            except Exception as exc:
                print("AI MULTI-POSITION RESCUE ERROR:", type(exc).__name__)
                # Retain the original decision set; missing listed titles are
                # added below for manual review only.

        jobs, recovered_count = _recover_missing_compact_list_roles(listed_role_titles, jobs)
        if recovered_count:
            print(
                f"COMPACT ROLE LIST RECOVERY: added {recovered_count} omitted title(s) "
                "as REVIEW-only records; automatic send was not inferred"
            )

    print(f"Found {len(jobs)} job position(s)")

    # ========================================================
    # PROCESS EACH DISTINCT POSITION
    # ========================================================

    post_success = True

    for job in jobs:

        if not isinstance(
            job,
            dict
        ):

            continue

        job_section_text, scope_matched, scope_reason = resolve_job_section(
            job,
            text,
            job_sections,
            len(jobs),
        )

        print(
            "JOB CONTACT SCOPE:",
            "matched" if scope_matched else "ambiguous; automatic send disabled",
        )

        position_scope_text, position_scope_matched, position_scope_reason = resolve_job_position_scope(
            job,
            job_section_text if scope_matched else "",
            jobs,
        )
        print(
            "JOB POSITION SCOPE:",
            "matched" if position_scope_matched else "ambiguous; no cross-position inference",
        )

        language_context = job_section_text if scope_matched else ""
        application_language, language_reason = determine_application_language(
            language_context,
            {"jobs": [job]},
        )

        print("APPLICATION LANGUAGE:", application_language)
        print("LANGUAGE REASON: details redacted from public workflow logs")

        decision = str(
            job.get(
                "decision",
                "REJECT"
            )
        ).upper().strip()

        if decision not in {
            "APPLY",
            "REVIEW",
            "REJECT"
        }:

            decision = "REJECT"

        original_ai_decision = decision

        # Deterministic fit checks override the AI when the score is weak
        # or the role title is clearly unrelated. A broad technical title
        # cannot authorize an automatic application by itself.
        cv_match_score, cv_matched_skill_groups = calculate_candidate_cv_match(
            job,
            position_scope_text if position_scope_matched else "",
        )
        decision_before_fit = decision
        decision, role_fit_reason = enforce_candidate_fit_rule(
            job,
            decision,
            position_scope_text if position_scope_matched else "",
            cv_match_score=cv_match_score,
        )
        role_fit_overrode_ai = decision != decision_before_fit

        # ====================================================
        # LOCATION SAFETY LAYER
        # ====================================================

        scoped_job = dict(job)
        scoped_job["accommodation_provided"] = scoped_accommodation_value(
            job,
            position_scope_text if position_scope_matched else "",
            has_multiple_sections,
        )

        (
            decision,
            location_status,
            location_reason
        ) = enforce_location_rule(
            scoped_job,
            decision,
            position_scope_text if position_scope_matched else "",
            section_verified=(scope_matched and position_scope_matched),
        )

        # ====================================================
        # POSITION-SCOPED EMAILS ONLY
        # Do not combine AI-returned emails or addresses from the whole post.
        # ====================================================

        scoped_emails = extract_emails(job_section_text) if scope_matched else []
        emails = []
        seen_emails = set()

        for email in scoped_emails:
            clean = str(email).strip()
            key = clean.lower()
            if clean and key not in seen_emails:
                seen_emails.add(key)
                emails.append(clean)

        # Automatic emailing is only permitted when both the job section
        # and exactly one email recipient are unambiguous.
        contact_review_reason = ""
        if decision == "APPLY":
            if not scope_matched:
                decision = "REVIEW"
                contact_review_reason = (
                    "Could not safely identify this position's own advertisement "
                    "section; automatic sending is disabled."
                )
            elif len(emails) == 0:
                decision = "REVIEW"
                contact_review_reason = (
                    "No email address is present in this position's own section. "
                    "The advertisement may require WhatsApp or another manual method."
                )
            elif len(emails) > 1:
                decision = "REVIEW"
                contact_review_reason = (
                    "Multiple email addresses appear in this position's section; "
                    "the intended recipient is ambiguous, so nothing was sent."
                )

        final_reason = (
            str(role_fit_reason or "").strip()
            if role_fit_overrode_ai and role_fit_reason
            else str(job.get("reason") or "").strip()
        )
        if contact_review_reason:
            final_reason = (
                (final_reason + " ") if final_reason else ""
            ) + contact_review_reason
        if role_fit_reason and not role_fit_overrode_ai:
            final_reason = (
                (final_reason + " ") if final_reason else ""
            ) + role_fit_reason
        if decision == "REVIEW" and cv_match_score >= 30:
            matched_text = ", ".join(cv_matched_skill_groups[:5]) or "role/title overlap"
            final_reason = (
                (final_reason + " ") if final_reason else ""
            ) + (
                f"مؤشر تطابق السيرة: {cv_match_score}/100 (مؤشر كلمات قابل للتفسير وليس احتمال قبول). "
                f"المجموعات المطابقة: {matched_text}. لا إرسال آلي قبل المراجعة."
            )

        # ====================================================
        # APPLICATION TITLE
        # ====================================================

        application_title = (
            get_application_job_title(
                job,
                application_language
            )
        )

        # Check manual rejection history before any email can be sent.
        # Fail closed if the guard fails, to prevent an unsafe application.
        pre_send_guard_reason = ""
        if decision != "REJECT" and JOB_PRE_SEND_GUARD is not None:
            try:
                pre_send_guard_reason = str(
                    JOB_PRE_SEND_GUARD(job, application_title, emails, post) or ""
                ).strip()
            except Exception as exc:
                print("PRE-SEND SAFETY CHECK ERROR:", type(exc).__name__)
                pre_send_guard_reason = (
                    "تم منع الإرسال تلقائيًا لتعذر التحقق من سجل الرفض السابق."
                )
            if pre_send_guard_reason:
                decision = "REJECT"
                final_reason = (
                    (final_reason + " ") if final_reason else ""
                ) + pre_send_guard_reason

        # ====================================================
        # CV
        # ====================================================

        if decision == "REJECT":

            cv = None

        else:

            cv = choose_cv(
                application_language
            )

        # ====================================================
        # JOB IDENTITY
        # ====================================================

        identity_email = (
            emails[0]
            if emails
            else ""
        )

        identity_key = job_identity_key(
            job,
            application_title,
            identity_email
        )

        # ====================================================
        # RECORD
        # ====================================================

        record = {

            "source":
                post.get(
                    "source"
                ),

            "message_id":
                post.get(
                    "message_id"
                ),

            "channel":
                post.get(
                    "channel"
                ),

            "date":
                post.get(
                    "date"
                ),

            "job_title":
                job.get(
                    "job_title"
                ),

            "application_job_title":
                application_title,

            "company":
                job.get(
                    "company",
                    ""
                ),

            "location":
                job.get(
                    "location",
                    ""
                ),

            "accommodation_provided":
                scoped_job.get(
                    "accommodation_provided",
                    "unknown"
                ),

            "location_status":
                location_status,

            "location_reason":
                location_reason,

            "language":
                job.get(
                    "language",
                    "mixed"
                ),

            "application_language":
                application_language,

            "application_language_reason":
                language_reason,

            "application_cv_language":
                application_language,

            "decision":
                decision,

            "original_ai_decision":
                original_ai_decision,

            "score":
                job.get(
                    "score"
                ),

            "cv_match_score":
                cv_match_score,

            "cv_matched_skill_groups":
                cv_matched_skill_groups,

            "reason":
                final_reason,

            "emails":
                emails,

            "cv":
                cv,

            "identity_key":
                identity_key,

            "contact_scope":
                scope_reason,

            "contact_scope_matched":
                scope_matched,

            "sent":
                False,

            "job_duplicate":
                False
        }

        # ====================================================
        # SEND ONLY APPLY
        # ====================================================

        send_attempted = False

        if decision == "APPLY":

            if not cv:

                print(
                    "SEND SKIPPED: CV not found"
                )

            elif not emails:

                print(
                    "SEND SKIPPED: No email found"
                )

            else:

                # ------------------------------------------------
                # FIRST CHECK:
                # Is this the SAME REAL-WORLD JOB already sent?
                #
                # This catches:
                #
                # Channel A -> same job
                # Channel B -> same job
                # Same channel later -> same job
                # Different wording -> same job
                # ------------------------------------------------

                same_job, old_record = (
                    already_sent_same_job(
                        job,
                        application_title,
                        emails
                    )
                )

                if same_job:

                    record["job_duplicate"] = True
                    record["sent"] = False

                    print(
                        "SKIPPED SAME JOB DUPLICATE:",
                        application_title
                    )

                else:

                    # ------------------------------------------------
                    # Normal per-email sending
                    # ------------------------------------------------

                    for email_address in emails:

                        key = application_key(
                            post,
                            job,
                            email_address
                        )

                        # --------------------------------------------
                        # Exact Telegram-post duplicate
                        # --------------------------------------------

                        if already_sent(
                            key
                        ):

                            print(
                                "SKIPPED EXACT DUPLICATE:",
                                email_address
                            )

                            continue

                        try:

                            send_attempted = True

                            send_application(
                                email_address,
                                job,
                                cv,
                                application_language,
                                text,
                                application_title
                            )

                            save_sent(
                                key,
                                {

                                    "source":
                                        post.get(
                                            "source"
                                        ),

                                    "message_id":
                                        post.get(
                                            "message_id"
                                        ),

                                    "job_title":
                                        job.get(
                                            "job_title"
                                        ),

                                    "application_job_title":
                                        application_title,

                                    "company":
                                        job.get(
                                            "company",
                                            ""
                                        ),

                                    "location":
                                        job.get(
                                            "location",
                                            ""
                                        ),

                                    "email":
                                        email_address,

                                    "cv":
                                        cv,

                                    "application_language":
                                        application_language,

                                    "identity_key":
                                        identity_key
                                }
                            )

                            record["sent"] = True

                        except Exception as e:

                            # Public Actions logs must not expose addresses or SMTP details.
                            print(
                                "GMAIL ERROR:",
                                type(e).__name__
                            )

        # If sending was attempted and no recipient accepted the application,
        # leave the Telegram cursor on this post so the next scheduled run retries it.
        if (
            decision == "APPLY"
            and emails
            and cv
            and send_attempted
            and not record["sent"]
        ):
            post_success = False

        # ====================================================
        # SAVE RESULT
        # ====================================================

        save_result(
            record
        )

        # Emit structured events for the Telegram control bot. Notification failures
        # never change whether the source post is considered processed.
        event = {
            "post": post,
            "record": record,
            "job": job,
            "advertisement": job_section_text if scope_matched else text,
        }
        if JOB_EVENT_CALLBACK is not None:
            try:
                JOB_EVENT_CALLBACK(event)
            except Exception as exc:
                print("JOB EVENT CALLBACK ERROR:", type(exc).__name__)

        # This separate callback is mandatory for historical quality statistics.
        # It runs after the Telegram callback may apply a manual-rejection guard,
        # so the audit records the final bot decision and not just the model output.
        if JOB_DECISION_AUDIT_CALLBACK is not None:
            try:
                JOB_DECISION_AUDIT_CALLBACK(event)
            except Exception as exc:
                print("DECISION AUDIT ERROR:", type(exc).__name__, "; post will retry")
                post_success = False

        print()
        log_job_result_safely(record, contact_review_reason)

    return post_success


# ============================================================
# MANUALLY APPROVED REVIEW APPLICATION
# ============================================================

def send_approved_review(item):
    """Send only a validated, explicitly approved real job review."""
    if not isinstance(item, dict):
        raise RuntimeError("Review item is invalid")
    record = item.get("record")
    job = item.get("job")
    post = item.get("post")
    if not isinstance(record, dict) or not isinstance(job, dict) or not isinstance(post, dict):
        raise RuntimeError("Review data is incomplete")
    # This is a recovery card for an unparsed advertisement, not an extracted job.
    # Keep this invariant in the sending layer, even if persisted review fields change.
    if record.get("analysis_fallback") is True:
        raise RuntimeError("An extraction-fallback card cannot be submitted as a job application")
    emails = record.get("emails") or []
    title = str(record.get("application_job_title") or "").strip()
    cv_path = str(record.get("cv") or "").strip()
    if item.get("can_auto_send") is not True:
        raise RuntimeError("This review is not safe for automatic email submission")
    if record.get("contact_scope_matched") is not True:
        raise RuntimeError("The job's advertisement section was not matched safely")
    if len(emails) != 1 or not str(emails[0]).strip():
        raise RuntimeError("A single unambiguous email recipient is required")
    if not str(record.get("location_status") or "").startswith("allowed_"):
        raise RuntimeError("Location eligibility must be confirmed before sending")
    if not cv_path or not os.path.isfile(cv_path):
        raise RuntimeError("The selected CV is unavailable")
    if not title:
        raise RuntimeError("Job title is missing")
    email_address = str(emails[0]).strip()
    key = application_key(post, job, email_address)
    same_job, _old_record = already_sent_same_job(job, title, [email_address])
    if same_job or already_sent(key):
        duplicate_record = dict(record)
        duplicate_record.update({
            "decision": "APPLY",
            "sent": False,
            "job_duplicate": True,
            "manual_approval": True,
            "manual_approval_status": "duplicate_skipped",
        })
        save_result(duplicate_record)
        return {"status": "duplicate"}
    send_application(
        email_address,
        job,
        cv_path,
        str(record.get("application_language") or "fr"),
        str(item.get("advertisement") or ""),
        title,
    )
    save_sent(key, {
        "source": post.get("source"),
        "message_id": post.get("message_id"),
        "job_title": job.get("job_title"),
        "application_job_title": title,
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "email": email_address,
        "cv": cv_path,
        "application_language": record.get("application_language"),
        "identity_key": record.get("identity_key"),
        "manual_approval": True,
    })
    sent_record = dict(record)
    sent_record.update({
        "decision": "APPLY",
        "sent": True,
        "job_duplicate": False,
        "manual_approval": True,
        "manual_approval_status": "sent",
        "manual_approval_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    save_result(sent_record)
    return {"status": "sent"}


# ============================================================
# MONITOR
# ============================================================

def monitor():

    print(
        "=" * 60
    )

    print(
        "JOBFINDER AI AUTOMATIC ANALYZER"
    )

    print(
        "=" * 60
    )

    print(
        "Watching:",
        JOBS_FILE
    )

    # --------------------------------------------------------
    # Start at the current end of the file.
    #
    # Old Telegram advertisements already present in jobs.jsonl
    # are NOT processed again on startup.
    # --------------------------------------------------------

    if os.path.exists(
        JOBS_FILE
    ):

        position = os.path.getsize(
            JOBS_FILE
        )

    else:

        position = 0

    print(
        "Waiting for new Telegram job posts..."
    )

    while True:

        try:

            if not os.path.exists(
                JOBS_FILE
            ):

                time.sleep(
                    3
                )

                continue

            current_size = os.path.getsize(
                JOBS_FILE
            )

            # ------------------------------------------------
            # If file was truncated/recreated
            # ------------------------------------------------

            if current_size < position:

                position = 0

            # ------------------------------------------------
            # New data
            # ------------------------------------------------

            if current_size > position:

                with open(
                    JOBS_FILE,
                    "r",
                    encoding="utf-8"
                ) as f:

                    f.seek(
                        position
                    )

                    new_data = f.read()

                    position = f.tell()

                lines = new_data.splitlines()

                for line in lines:

                    if not line.strip():

                        continue

                    try:

                        post = json.loads(
                            line
                        )

                        process_post(
                            post
                        )

                    except Exception as e:

                        print(
                            "POST ERROR:",
                            repr(e)
                        )

            time.sleep(
                3
            )

        except KeyboardInterrupt:

            print(
                "\nJobFinder stopped."
            )

            break

        except Exception as e:

            print(
                "MONITOR ERROR:",
                repr(e)
            )

            time.sleep(
                5
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    monitor()
