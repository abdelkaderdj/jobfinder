"""Canonical owner-defined JobFinder candidate policy.

This module is the single source for experience thresholds and the policy wording
shared by the extraction prompt, independent APPLY reviewer, and deterministic
decision gate. Changes to candidate scope must be reflected here and in the
regression tests before a workflow can be approved.
"""

from __future__ import annotations

import math
from typing import Any


# The owner explicitly asked JobFinder to consider applications requiring up to
# and including four years of experience. Experience alone is not a rejection
# reason at or below this limit.
MAX_EXPERIENCE_YEARS_APPLY = 4.0


# Human review is used when the employer explicitly requires experience but
# gives no usable number. The candidate's eligibility cannot be safely inferred.
UNQUANTIFIED_MANDATORY_EXPERIENCE_DECISION = "review"


def classify_experience_requirement(years: Any) -> str | None:
    """Classify an explicit minimum/lower-bound experience requirement."""
    if years is None or isinstance(years, bool):
        return None
    try:
        value = float(years)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if value <= MAX_EXPERIENCE_YEARS_APPLY:
        return "waive_experience"
    return "reject"


def experience_policy_prompt_text() -> str:
    """Render the same thresholds into every model prompt that uses this policy."""
    limit = f"{MAX_EXPERIENCE_YEARS_APPLY:g}"
    return (
        "EXPERIENCE ELIGIBILITY POLICY — AUTHORITATIVE; THE CODE ENFORCES THESE THRESHOLDS\n"
        "- Do not invent a minimum experience requirement when the advertisement does not state one.\n"
        f"- An explicit minimum of up to and including {limit} years: experience alone MUST NOT cause REJECT or REVIEW. If the role otherwise fits and no independent blocker applies, choose APPLY.\n"
        "- Do not treat absence of professional experience as a second reason to reject or review when it is the same waived minimum-years condition.\n"
        f"- More than {limit} years as an explicit minimum: REJECT on experience grounds.\n"
        "- For a stated range, preserve the lower/minimum endpoint; the deterministic policy layer applies the threshold.\n"
        "- An optional/preferred experience value is not a mandatory minimum and must not trigger the experience rejection rule.\n"
        "- An upper bound such as 'up to 4 years', '4 years maximum', or '4 ans maximum' is not a minimum requirement.\n"
        "- If professional experience is explicitly mandatory but no number or usable lower bound is stated, choose REVIEW; do not invent a threshold or reject automatically.\n"
        "- This policy applies to experience ONLY. It does not override a mismatched discipline, mandatory degree/certificate/licence, seniority, location, or any other eligibility rule.\n"
        "- Extract and report the employer's actual experience wording. Do not independently override the thresholds above based on intuition."
    )


def candidate_role_policy_prompt_text() -> str:
    """Shared candidate-scope rules for extraction and independent APPLY review."""
    return (
        "CANDIDATE ROLE-SCOPE POLICY — OWNER-DEFINED; APPLY TO EACH POSITION SEPARATELY\n"
        "- Candidate qualification: Licence/Bac+3 in Mechanical and Energy Engineering (2021); not an engineering diploma. The candidate has no professional engineering work experience. Knowledge, coursework, and training are not professional experience.\n"
        "- Primary target lanes: mechanical maintenance; industrial/mechanical equipment; refrigeration, froid, frigoriste, HVAC and thermal systems; thermodynamics and heat transfer; centrifugal pumps and fluid systems; junior mechanical design, CAD, SolidWorks, and technical drawings.\n"
        "- Closely adjacent junior technical/operator roles may be considered only when the duties in THAT position's own section show a genuine connection to the target lanes. A company's industry or a shared word elsewhere in the post is not evidence.\n"
        "- Hard exclusions: sales/commercial, marketing, teaching/training, administration, accounting, HR, laboratory/biology/chemistry, electrical-only, electromechanical, instrumentation/automation, civil engineering, HSE, aviation/aircraft, information technology/IT support/computer systems/networks/telecommunications, procurement/logistics/supply-chain, security, and other unrelated disciplines.\n"
        "- Any position titled Ingénieur, Engineer, or مهندس is REJECT unless THAT position's own advertisement explicitly accepts Licence/Bac+3 as sufficient and does not make a higher engineering degree mandatory. A generic mention of Licence elsewhere in a multi-position post is not enough.\n"
        "- Production technician/production operator positions are REVIEW only; never auto-send solely because the title is technical or the employer is industrial.\n"
        "- Broad generalist/polyvalent roles are REVIEW only when local and when their own duties do not establish a clear target-lane match. Never APPLY based only on a factory/industrial employer.\n"
        "- Never borrow tasks, qualifications, experience, education, location, accommodation, or skills from a neighbouring position. Shared conditions may apply only when the advertisement clearly states they apply to all positions.\n"
        "- A keyword-overlap score is an explainable screening aid, NOT a probability of being accepted and NOT a substitute for title, position-specific duties, or hard eligibility rules."
    )


def candidate_location_policy_prompt_text() -> str:
    """Shared location policy used by model prompts."""
    return (
        "LOCATION ELIGIBILITY POLICY — OWNER-DEFINED\n"
        "- Oran, Mostaganem, and the explicitly configured nearby local areas are eligible.\n"
        "- Southern/Saharan Algerian locations are eligible.\n"
        "- Other known Algerian locations require explicit employer-provided accommodation for THAT position; residency, transport pickup points, or a company address are not accommodation.\n"
        "- Unknown or ambiguous work location is REVIEW, never an automatic APPLY.\n"
        "- Do not infer accommodation, residence, transport, or worksite from unrelated lines or another position."
    )


# Deterministic role-fit rules. Keep these patterns in the same module as the
# natural-language policy so the model prompt and Python safety gate share one policy source.
UNRELATED_ROLE_TITLE_PATTERNS = (
    r"\bcommercial\b", r"\btechnico commercial\b", r"\bsales?\b",
    r"\bvente\b", r"\bmarketing\b", r"\bbusiness developer\b",
    # Information technology, helpdesk, computer systems, networks and telecoms
    # are outside the candidate's mechanical/energy engineering target paths.
    r"\b(?:it support|support informatique|technicien support informatique|"
    r"computer support|computer technician|help ?desk|service desk|"
    r"information technology|computer science|informatique|informaticien\w*|"
    r"systemes? d information|information systems|system administrator|"
    r"systems administrator|network technician|technicien reseaux|"
    r"technicien reseaux et telecoms|reseaux informatiques|"
    r"telecommunications?|telecoms?|telecom technician|"
    r"ingenieur reseaux|ingenieur infrastructure|support it)\b",
    r"\b(?:standardiste|agent de communication|communication officer|"
    r"communication and switchboard|standard operator)\b",
    r"\b(?:approvisionnement|approvisionnements|procurement|purchasing|"
    r"supply officer)\b",
    # Packaging/conditioning is not mechanical maintenance.
    r"\bagent de conditionnement\b", r"\boperateur de conditionnement\b",
    r"\bpackaging (?:agent|operator|worker)\b", r"\bpacking (?:agent|operator|worker)\b",
    # Training and administrative-writing roles are outside the candidate's target paths.
    r"\bformateur\b", r"\bformatrice\b", r"\btrainer\b", r"\btraining instructor\b",
    r"\binstructor\b", r"\bteacher\b", r"\bprofessor\b", r"\bprofesseur(?:e)?\b",
    r"\benseignant(?:e)?\b", r"أستاذ", r"أساتذة",
    r"\bredaction administrative\b", r"\badministrative writing\b",
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

# Direct maintenance-technician titles can bypass an experience-only concern
# when the CV match is strong; engineer/leadership titles remain governed separately.
DIRECT_MAINTENANCE_TECHNICIAN_ROLE_PATTERNS = (
    r"\btechnicien(?:ne)? de maintenance\b",
    r"\btechnicien(?:ne)? maintenance\b",
    r"\bmaintenance technician\b",
    r"\btechnician in maintenance\b",
    r"\bفني صيانة\b", r"\bتقني صيانة\b",
)

# Broad local generalist roles can be reviewed, but never auto-sent solely by title.
# These roles are distinct from a direct mechanical-maintenance match.
GENERALIST_ROLE_TITLE_PATTERNS = (
    r"\bagents? polyvalents?\b",
    r"\bpolyvalent agents?\b",
    r"\bmulti[- ]?purpose (?:agents?|workers?)\b",
    r"\bmultiskilled workers?\b",
    r"\bعون متعدد المهام\b",
    r"\bعامل متعدد المهام\b",
    r"\bعمال متعددوا المهام\b",
    r"\bعامل متعدد الاختصاصات\b",
)

# Production roles remain eligible for manual review, but are never auto-sent.
# Keep these patterns separate from hard exclusions such as unrelated disciplines.
PRODUCTION_ROLE_TITLE_PATTERNS = (
    r"\btechnicien(?:ne)? de production\b", r"\btechnicien production\b",
    r"\bproduction technician\b", r"\bproduction technologist\b",
    r"\boperateur de production\b", r"\bproduction operator\b",
    r"\bagent de production\b", r"\bproduction worker\b",
    r"\bتقني انتاج\b", r"\bفني انتاج\b", r"\bعامل انتاج\b", r"\bمشغل انتاج\b",
)

# The candidate holds a Licence/Bac+3, not an engineering diploma.
ENGINEERING_GRADE_ROLE_TITLE_PATTERNS = (
    # Match singular and plural forms after normalize_text removes accents.
    r"\bingenieur\w*\b",
    r"\bengineer\w*\b",
    r"\bمهندس\w*\b",
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
    # The noun must indicate a technical role; descriptive words like "techniques"
    # in administrative-writing courses are not evidence of candidate fit.
    r"\btechnicien\w*\b", r"\btechnician\w*\b",
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


# Location and accommodation signals consumed by the deterministic geography gate.
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
