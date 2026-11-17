"""Presidio analyzer wrapper: the "Presidio + UK NINO" baseline.

Configuration: the stock AnalyzerEngine recognizers with spaCy
``en_core_web_lg``, plus ``UkNinoRecognizer`` (UK National Insurance number),
which ships with presidio-analyzer but is not in the default registry. There
is no score threshold, so the document score keeps Presidio's own
confidence. ``recognizer_config()`` returns the loaded recognizers for the
run record. Span edges are trimmed of whitespace. Only entity types that map onto our label set are kept. Others
(DATE_TIME, NRP, URL and similar) are dropped so they are not counted as
false positives against labels we never annotate.
"""

from __future__ import annotations

from typing import Any, Sequence

from castellan.data.schema import Example
from castellan.evaluate import DocPrediction
from castellan.spans import Finding

ENTITY_MAP: dict[str, str] = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "CARD",
    "US_BANK_NUMBER": "CARD",
    "US_SSN": "GOV_ID",
    "US_ITIN": "GOV_ID",
    "US_PASSPORT": "GOV_ID",
    "US_DRIVER_LICENSE": "GOV_ID",
    "UK_NHS": "GOV_ID",
    "UK_NINO": "GOV_ID",
    "MEDICAL_LICENSE": "GOV_ID",
    "IP_ADDRESS": "IP_ADDRESS",
    "PERSON": "PERSON",
    "LOCATION": "ADDRESS",
    "CRYPTO": "SECRET_GENERIC",
}


def map_results(results: Sequence[Any], text: str | None = None) -> list[Finding]:
    """Presidio RecognizerResult-like objects -> Findings (mapped types only).

    With ``text``, leading and trailing whitespace is trimmed from each span.
    Some stock regexes (UK_NINO, for one) start with an optional space and
    would otherwise include it. Trimming changes no detection decision, only
    the boundaries.
    """
    out = []
    for r in results:
        lab = ENTITY_MAP.get(r.entity_type)
        a, b = int(r.start), int(r.end)
        if text is not None:
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
        if lab is not None and b > a:
            out.append(Finding(a, b, lab, float(r.score)))
    return sorted(out, key=lambda f: (f.start, f.end))


def presidio_version() -> str:
    from importlib.metadata import version

    return version("presidio-analyzer")


class PresidioDetector:
    name = "presidio"  # machine key for file names and CLI flags

    @property
    def display_name(self) -> str:
        """Name used in reports. Not "Presidio defaults": UkNinoRecognizer is added on top."""
        return "Presidio + UK NINO" if self.extra_uk else "Presidio"

    def __init__(self, spacy_model: str = "en_core_web_lg", language: str = "en", extra_uk: bool = True) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import UkNinoRecognizer

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": language, "model_name": spacy_model}],
            }
        )
        self.analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=[language])
        if extra_uk:
            self.analyzer.registry.add_recognizer(UkNinoRecognizer(supported_language=language))
        self.extra_uk = extra_uk
        self.language = language
        self.spacy_model = spacy_model
        self.version = presidio_version()

    def recognizer_config(self) -> list[dict[str, Any]]:
        recs = self.analyzer.registry.get_recognizers(language=self.language, all_fields=True)
        return sorted(
            ({"name": r.name, "entities": list(r.supported_entities)} for r in recs), key=lambda d: d["name"]
        )

    def predict(self, examples: Sequence[Example]) -> list[DocPrediction]:
        preds = []
        for ex in examples:
            res = self.analyzer.analyze(text=ex.text, language=self.language)
            preds.append(DocPrediction(ex.id, map_results(res, ex.text)))
        return preds
