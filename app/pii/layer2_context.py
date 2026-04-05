"""Layer 2: context window classification for low-confidence Layer 1 results.

Invoked for any DetectionResult where needs_layer2=True (score < 0.75).
Examines the 100 characters surrounding the match in the block text and
applies deterministic keyword-based context analysis to boost confidence.

Also handles:
- Context negation: "not", "example", "sample" near detection → lower confidence
- Multi-language keywords for GDPR deployments (DE/FR/ES/IT/NL/PL)
- OCR confidence penalty for low-quality handwriting/scan blocks

Phase 1 uses keyword lookup only.  Phase 2 will replace this with a
fine-tuned spaCy text classifier trained on human-reviewed labels.

Safety rule: raw text and context window content are never logged — only
entity_type, score delta, and whether a signal was found.
"""
from __future__ import annotations

import re
import logging

from app.pii.presidio_engine import DetectionResult

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD: float = 0.75
_CONTEXT_WINDOW_CHARS: int = 100
_BOOST_AMOUNT: float = 0.20
_MAX_SCORE: float = 1.0
_INSTITUTIONAL_PENALTY: float = 0.15
_PRIMARY_BOOST: float = 0.05
_NEGATION_PENALTY: float = 0.15
_NEGATION_WINDOW_CHARS: int = 50
_OCR_LOW_CONFIDENCE_THRESHOLD: float = 0.60
_OCR_LOW_CONFIDENCE_PENALTY: float = 0.10

# Keywords that corroborate a given entity type when found in the context window.
_CONTEXT_SIGNALS: dict[str, list[str]] = {
    "SSN": ["ssn", "social security", "sin", "tax id", "tin"],
    # Partial/masked SSN types share SSN context signals
    "SSN_PARTIAL": ["ssn", "social security", "last four", "last 4", "tax id"],
    "SSN_LAST_FOUR": ["ssn", "social security", "last four", "last 4", "tax id"],
    "PERSON": ["name", "employee", "patient", "client", "staff", "person"],
    "EMAIL_ADDRESS": ["email", "e-mail", "contact", "mailto"],
    "PHONE_NUMBER": ["phone", "tel", "telephone", "call", "fax", "mobile", "cell"],
    # International phone types share phone signals
    "PHONE_UK_MOBILE": ["phone", "mobile", "cell", "tel", "contact"],
    "PHONE_UK_LANDLINE": ["phone", "tel", "telephone", "landline", "contact"],
    "PHONE_EU": ["phone", "tel", "telephone", "telefon", "téléphone", "teléfono", "telefono"],
    "PHONE_DE": ["telefon", "tel", "handy", "mobil", "rufnummer", "phone"],
    "PHONE_IN_LANDLINE": ["phone", "tel", "telephone", "landline", "contact"],
    "LOCATION": ["address", "addr", "city", "state", "zip", "postal", "street"],
    "DATE_TIME": ["date", "dob", "born", "birth", "hired", "since"],
    "CREDIT_CARD": ["card", "visa", "mastercard", "amex", "cc", "credit", "payment"],
    "FINANCIAL_ACCOUNT": ["account", "acct", "iban", "routing", "bank", "swift"],
    "IP_ADDRESS": ["ip address", "host", "server", "network"],
    "DRIVER_LICENSE_US": ["license", "licence", "dl", "driver", "dmv"],
    "PASSPORT": ["passport", "travel document"],
    "AADHAAR": ["aadhaar", "aadhar", "uid"],
    "PAN_IN": ["pan", "permanent account"],
    "NI_UK": ["national insurance", "nino"],
}

# ---------------------------------------------------------------------------
# Multi-language context keywords (GDPR deployments)
# ---------------------------------------------------------------------------
# These are merged into _CONTEXT_SIGNALS at module load time so the same
# classify() path handles all languages without branching.

_MULTILANG_SIGNALS: dict[str, list[str]] = {
    # German
    "PERSON": [
        "name", "mitarbeiter", "patient", "kunde", "angestellter", "person",
        "vorname", "nachname", "familienname",
        # French
        "nom", "prénom", "employé", "patient", "client", "personne",
        # Spanish
        "nombre", "apellido", "empleado", "paciente", "cliente",
        # Italian
        "nome", "cognome", "dipendente", "paziente", "cliente",
        # Dutch
        "naam", "werknemer", "patiënt", "klant",
        # Polish
        "imię", "nazwisko", "pracownik", "pacjent", "klient",
    ],
    "PHONE_NUMBER": [
        # German
        "telefon", "tel", "handy", "mobil", "rufnummer", "fernruf",
        # French
        "téléphone", "tél", "portable", "mobile", "numéro",
        # Spanish
        "teléfono", "tel", "móvil", "celular",
        # Italian
        "telefono", "tel", "cellulare", "mobile",
        # Dutch
        "telefoon", "tel", "mobiel",
        # Polish
        "telefon", "tel", "komórkowy",
    ],
    "EMAIL_ADDRESS": [
        "e-mail", "courriel", "correo", "posta elettronica", "e-post",
    ],
    "LOCATION": [
        # German
        "adresse", "anschrift", "straße", "stadt", "postleitzahl", "plz",
        # French
        "adresse", "rue", "ville", "code postal",
        # Spanish
        "dirección", "calle", "ciudad", "código postal",
        # Italian
        "indirizzo", "via", "città", "cap", "codice postale",
        # Dutch
        "adres", "straat", "stad", "postcode",
        # Polish
        "adres", "ulica", "miasto", "kod pocztowy",
    ],
    "DATE_TIME": [
        # German
        "geburtsdatum", "geboren", "geb",
        # French
        "date de naissance", "né(e) le",
        # Spanish
        "fecha de nacimiento", "nacido",
        # Italian
        "data di nascita", "nato/a",
        # Dutch
        "geboortedatum", "geboren",
        # Polish
        "data urodzenia", "urodzony",
    ],
    "FINANCIAL_ACCOUNT": [
        # German
        "konto", "kontonummer", "bankverbindung", "iban", "blz",
        # French
        "compte", "numéro de compte", "rib",
        # Spanish
        "cuenta", "número de cuenta",
        # Italian
        "conto", "numero di conto",
        # Dutch
        "rekening", "rekeningnummer",
    ],
    "CREDIT_CARD": [
        "kreditkarte", "carte de crédit", "tarjeta de crédito",
        "carta di credito", "creditcard",
    ],
    "SSN": [
        # German tax ID
        "steuer-id", "steuernummer", "steueridentifikationsnummer",
        # French
        "numéro de sécurité sociale", "nss",
        # Spanish
        "número de seguridad social",
        # Italian
        "codice fiscale", "numero previdenziale",
    ],
}

# Merge multilang into main signals (deduplicate)
for _entity_type, _keywords in _MULTILANG_SIGNALS.items():
    existing = set(_CONTEXT_SIGNALS.get(_entity_type, []))
    merged = list(existing | set(_keywords))
    _CONTEXT_SIGNALS[_entity_type] = merged

# ---------------------------------------------------------------------------
# Context negation patterns
# ---------------------------------------------------------------------------

_NEGATION_TERMS: frozenset[str] = frozenset({
    "not", "example", "sample", "test", "fake", "dummy",
    "do not use", "placeholder", "for illustration",
    "mock", "demo", "fictitious", "hypothetical",
    # Multi-language negation terms
    "beispiel", "test", "muster",    # German
    "exemple", "test", "fictif",     # French
    "ejemplo", "prueba", "ficticio", # Spanish
    "esempio", "test", "fittizio",   # Italian
})

# Pre-compiled pattern for negation detection (within _NEGATION_WINDOW_CHARS)
_NEGATION_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in sorted(_NEGATION_TERMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


class Layer2ContextClassifier:
    """Apply context window classification to low-confidence Layer 1 results.

    One instance may be shared across calls — this class holds no mutable state.
    """

    def classify(
        self,
        result: DetectionResult,
        full_text: str,
        *,
        entity_role: str | None = None,
    ) -> DetectionResult:
        """Examine a 100-char window around the match and return an updated result.

        Parameters
        ----------
        result:
            Layer 1 DetectionResult, typically with needs_layer2=True.
        full_text:
            Complete text of the block the match came from.  Never logged.
        entity_role:
            Optional entity role from structure analysis.  When provided,
            ``"institutional"`` reduces score by 0.15 and
            ``"primary_subject"`` boosts score by 0.05.

        Returns
        -------
        DetectionResult
            Always has extraction_layer="layer_2_context".
            Score is boosted by _BOOST_AMOUNT (capped at 1.0) when a signal
            keyword is found in the context window; otherwise score unchanged.
            needs_layer2 is recalculated from the new score in __post_init__.
        """
        window_start = max(0, result.start - _CONTEXT_WINDOW_CHARS)
        window_end = min(len(full_text), result.end + _CONTEXT_WINDOW_CHARS)
        context_lower = full_text[window_start:window_end].lower()

        signals = _CONTEXT_SIGNALS.get(result.entity_type, [])
        matched_signal = next((s for s in signals if s in context_lower), None)

        new_score = result.score
        if matched_signal:
            new_score = min(_MAX_SCORE, result.score + _BOOST_AMOUNT)

        # --- Context negation handling ---
        # Check a tighter window (50 chars) for negation terms.
        # Penalize confidence instead of suppressing — let human reviewer decide.
        negation_found = False
        neg_start = max(0, result.start - _NEGATION_WINDOW_CHARS)
        neg_end = min(len(full_text), result.end + _NEGATION_WINDOW_CHARS)
        neg_context = full_text[neg_start:neg_end]
        if _NEGATION_PATTERN.search(neg_context):
            negation_found = True
            new_score = max(0.0, new_score - _NEGATION_PENALTY)

        # --- OCR confidence penalty ---
        # Low OCR confidence (handwriting, poor scans) → reduce detection score
        ocr_penalized = False
        block_ocr_conf = getattr(result.block, "ocr_confidence", None)
        if block_ocr_conf is not None and block_ocr_conf < _OCR_LOW_CONFIDENCE_THRESHOLD:
            ocr_penalized = True
            new_score = max(0.0, new_score - _OCR_LOW_CONFIDENCE_PENALTY)

        # Apply entity role confidence nudge
        if entity_role == "institutional":
            new_score = max(0.0, new_score - _INSTITUTIONAL_PENALTY)
        elif entity_role == "primary_subject":
            new_score = min(_MAX_SCORE, new_score + _PRIMARY_BOOST)

        # SAFETY: never log raw text — only entity_type, scores, and bool flag
        logger.debug(
            "Layer2: entity_type=%s old_score=%.3f new_score=%.3f signal_found=%s "
            "negation=%s ocr_penalty=%s role=%s",
            result.entity_type,
            result.score,
            new_score,
            matched_signal is not None,
            negation_found,
            ocr_penalized,
            entity_role,
        )

        return DetectionResult(
            block=result.block,
            entity_type=result.entity_type,
            start=result.start,
            end=result.end,
            score=new_score,
            pattern_used=result.pattern_used,
            geography=result.geography,
            regulatory_framework=result.regulatory_framework,
            extraction_layer="layer_2_context",
        )
