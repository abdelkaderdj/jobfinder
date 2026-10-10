import os
import json
import time
import re
import smtplib
import unicodedata
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


if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set")

if not GMAIL_APP_PASSWORD:
    raise RuntimeError("GMAIL_APP_PASSWORD is not set")

if not GMAIL_ADDRESS:
    raise RuntimeError("GMAIL_ADDRESS is not set")


client = Groq(
    api_key=GROQ_API_KEY
)


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


def _scope_title_tokens(value: str) -> set[str]:
    tokens = normalize_text(value).split()
    return {
        token for token in tokens
        if len(token) >= 2 and token not in _SCOPE_STOP_WORDS
    }


def _job_section_score(job: dict, section: str) -> tuple[float, float]:
    """Return (total score, title score); title relevance is mandatory."""
    normalized_section = normalize_text(section)
    section_tokens = set(normalized_section.split())
    title_candidates = []
    for field in (
        "job_title",
        "application_title_fr",
        "application_title_en",
    ):
        value = str(job.get(field) or "").strip()
        if value and value not in title_candidates:
            title_candidates.append(value)

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


def scoped_accommodation_value(
    job: dict,
    job_section: str,
    has_multiple_sections: bool,
):
    """
    Revalidate AI accommodation extraction against the mapped section.
    In multi-notice posts, accommodation in another section is not evidence
    for this role. Explicit local signals override a leaked AI true value.
    """
    if not has_multiple_sections:
        return job.get("accommodation_provided", "unknown")

    signal = accommodation_signal_in_text(job_section)
    if signal is True:
        return "true"
    if signal is False:
        return "false"

    if accommodation_value(job) is True:
        return "unknown"

    return job.get("accommodation_provided", "unknown")


def accommodation_signal_in_text(text: str):
    """Return True/False for explicit local housing signals, else None."""
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
    ]
    if any(normalize_text(marker) in normalized for marker in negative_markers):
        return False

    positive_markers = ACCOMMODATION_MARKERS + [
        "hébergement assuré",
        "hebergement assure",
        "logement pris en charge",
        "hébergement pris en charge",
        "hebergement pris en charge",
        "accommodation provided",
        "housing provided",
        "full accommodation",
        "تكفل تام بالسكن",
        "التكفل بالسكن",
        "التكفل بالإقامة",
        "التكفل بالاقامة",
    ]
    if any(normalize_text(marker) in normalized for marker in positive_markers):
        return True

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


def groq_json_request(
    messages,
    temperature=0,
    max_completion_tokens=1000
):

    base_kwargs = {

        "model":
            "openai/gpt-oss-120b",

        "messages":
            messages,

        "temperature":
            temperature,

        "max_completion_tokens":
            max_completion_tokens,

        "reasoning_effort":
            "low",

        "reasoning_format":
            "hidden"
    }

    # ========================================================
    # JSON MODE
    # ========================================================

    try:

        response = client.chat.completions.create(
            **base_kwargs,
            response_format={
                "type": "json_object"
            }
        )

        return parse_json_response(
            response.choices[0]
            .message
            .content
        )

    except Exception as first_error:

        print(
            "JSON MODE RETRY:",
            repr(first_error)
        )

    # ========================================================
    # NORMAL MODE FALLBACK
    # ========================================================

    response = client.chat.completions.create(
        **base_kwargs
    )

    return parse_json_response(
        response.choices[0]
        .message
        .content
    )


# ============================================================
# AI ANALYSIS
# ============================================================

def analyze_offer(text):

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

- accommodation
- housing
- lodging
- residence
- camp
- base de vie
- accommodation + meals when it clearly means on-site living

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

- a mandatory requirement is not met
- experience is mandatory and candidate lacks it
- required certification is missing
- location eligibility is not met
- education level is not met
- role is unrelated
- seniority is clearly above junior level

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

Score from 0 to 100.

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


ACCOMMODATION_MARKERS = [

    "hebergement",
    "hébergement",
    "logement",
    "residence",
    "résidence",
    "accommodation",
    "housing",
    "lodging",
    "base de vie",
    "base vie",
    "camp",
    "on-site accommodation",
    "on site accommodation",
    "room provided",
    "chambre fournie",
    "سكن",
    "إقامة",
    "اقامة",
    "مبيت",
    "سكن وظيفي",
    "السكن متوفر",
    "الاقامة متوفرة",
    "الإقامة متوفرة"
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
    job_section_text
):

    category = job_location_category(
        job
    )

    accommodation = accommodation_value(
        job
    )

    if category == "local":

        return (
            decision,
            "allowed_local",
            "Local/nearby area accepted"
        )

    if category == "south":

        return (
            decision,
            "allowed_south",
            "Southern/Saharan Algeria accepted"
        )

    if accommodation is True:

        return (
            decision,
            "allowed_housing",
            "Housing/accommodation provided"
        )

    if (
        accommodation is None
        and explicit_accommodation_in_ad(
            job_section_text
        )
    ):

        return (
            decision,
            "allowed_housing_context",
            "Accommodation/housing is explicitly mentioned "
            "in the advertisement"
        )

    if category == "other":

        return (
            "REJECT",
            "rejected_location",
            "Work location is outside the accepted "
            "local/Saharan area and no acceptable "
            "accommodation was identified"
        )

    # Unknown location
    if decision == "APPLY":

        return (
            "REVIEW",
            "unknown_location",
            "Work location is not clear enough "
            "to confirm eligibility"
        )

    return (
        decision,
        "unknown_location",
        "Work location is not clearly identified"
    )


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
    # Same exact email + strong title similarity
    # --------------------------------------------------------

    if (
        same_email
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
        and similarity >= 0.82
    ):

        return True

    return False


def already_sent_same_job(
    current_job,
    current_title,
    emails
):

    sent_records = load_sent_records()

    for old_record in sent_records:

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
        filename=os.path.basename(
            cv_path
        )
    )

    print("Sending application (recipient hidden in public logs)")
    print("Subject:", subject)

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

            try:

                server.quit()

            except smtplib.SMTPResponseException as e:

                if e.smtp_code != 250:

                    raise

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


# ============================================================
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

    jobs = result.get(
        "jobs",
        []
    )

    if not isinstance(
        jobs,
        list
    ):

        jobs = []

    print(
        f"Found {len(jobs)} job position(s)"
    )

    # Separate independent recruitment notices where the message has clear
    # numbered/hiring headings. Contact data is scoped per job, not per message.
    job_sections = split_advertisement_sections(text)
    has_multiple_sections = len(job_sections) > 1
    print(f"Detected {len(job_sections)} recruitment section(s)")

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
            scope_reason,
            "| matched" if scope_matched else "| ambiguous; automatic send disabled",
        )

        language_context = job_section_text if scope_matched else ""
        application_language, language_reason = determine_application_language(
            language_context,
            {"jobs": [job]},
        )

        print("APPLICATION LANGUAGE:", application_language)
        print("LANGUAGE REASON:", language_reason)

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

        # ====================================================
        # LOCATION SAFETY LAYER
        # ====================================================

        scoped_job = dict(job)
        scoped_job["accommodation_provided"] = scoped_accommodation_value(
            job,
            job_section_text if scope_matched else "",
            has_multiple_sections,
        )

        (
            decision,
            location_status,
            location_reason
        ) = enforce_location_rule(
            scoped_job,
            decision,
            job_section_text if scope_matched else "",
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

        final_reason = str(job.get("reason") or "").strip()
        if contact_review_reason:
            final_reason = (
                (final_reason + " ") if final_reason else ""
            ) + contact_review_reason

        # ====================================================
        # APPLICATION TITLE
        # ====================================================

        application_title = (
            get_application_job_title(
                job,
                application_language
            )
        )

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

        print()

        print(
            "JOB:",
            record["job_title"]
        )

        print(
            "APPLICATION JOB TITLE:",
            record["application_job_title"]
        )

        print(
            "COMPANY:",
            record["company"]
        )

        print(
            "LOCATION:",
            record["location"]
        )

        print(
            "LOCATION STATUS:",
            record["location_status"]
        )

        print(
            "ACCOMMODATION:",
            record["accommodation_provided"]
        )

        print(
            "DECISION:",
            record["decision"]
        )

        print(
            "ORIGINAL AI DECISION:",
            record["original_ai_decision"]
        )

        print(
            "SCORE:",
            record["score"]
        )

        print(
            "ADVERTISEMENT LANGUAGE:",
            record["language"]
        )

        print(
            "APPLICATION LANGUAGE:",
            record["application_language"]
        )

        print(
            "LANGUAGE REASON:",
            record["application_language_reason"]
        )

        print(
            "REQUESTED CV LANGUAGE:",
            record["application_cv_language"]
        )

        print(
            "CV:",
            os.path.basename(record["cv"]) if record["cv"] else None
        )

        print(
            "SCOPED EMAILS:",
            f"{len(record['emails'])} address(es) [redacted]"
        )

        if contact_review_reason:
            print("CONTACT REVIEW:", contact_review_reason)

        print(
            "JOB DUPLICATE:",
            record["job_duplicate"]
        )

        print(
            "SENT:",
            record["sent"]
        )

        print(
            "REASON:",
            record["reason"]
        )

        print(
            "LOCATION REASON:",
            record["location_reason"]
        )

    return post_success


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
