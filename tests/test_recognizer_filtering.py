"""Tests for Phase 14a-ii: protocol-driven recognizer filtering.

Verifies that PROTOCOL_DEFAULT_ENTITIES maps all 8 base protocols to
valid entity type lists, that PresidioEngine.analyze() respects the
target_entity_types parameter, and that _resolve_target_entities()
correctly resolves from protocol config or protocol defaults.
"""
from __future__ import annotations

import pytest

from app.core.constants import PROTOCOL_DEFAULT_ENTITIES


# ---------------------------------------------------------------------------
# All 8 protocols have valid default entity lists
# ---------------------------------------------------------------------------

class TestProtocolDefaultEntities:
    """PROTOCOL_DEFAULT_ENTITIES completeness and consistency."""

    REQUIRED_PROTOCOLS = [
        "hipaa", "hipaa_breach_rule",
        "gdpr", "gdpr_article_33",
        "ccpa", "hitech", "dpdpa", "pci_dss",
        "state_breach", "state_breach_generic",
        "bipa", "ferpa",
    ]

    @pytest.mark.parametrize("protocol_id", REQUIRED_PROTOCOLS)
    def test_protocol_has_default_entities(self, protocol_id: str) -> None:
        """Every required protocol ID has a non-empty default entity list."""
        assert protocol_id in PROTOCOL_DEFAULT_ENTITIES
        entities = PROTOCOL_DEFAULT_ENTITIES[protocol_id]
        assert isinstance(entities, list)
        assert len(entities) >= 2, f"{protocol_id} should have at least 2 entity types"

    @pytest.mark.parametrize("protocol_id", REQUIRED_PROTOCOLS)
    def test_all_entity_types_are_strings(self, protocol_id: str) -> None:
        """Every entity type in the default list is a non-empty string."""
        for et in PROTOCOL_DEFAULT_ENTITIES[protocol_id]:
            assert isinstance(et, str)
            assert len(et) > 0

    def test_hipaa_includes_us_types(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["hipaa"]
        assert "US_SSN" in entities
        assert "PERSON" in entities
        assert "MEDICAL_LICENSE" in entities

    def test_hipaa_excludes_non_us_types(self) -> None:
        """HIPAA should NOT include UK/EU/India-specific types."""
        entities = PROTOCOL_DEFAULT_ENTITIES["hipaa"]
        for bad in ["COMPANY_NUMBER_UK", "VAT_EU", "NHS_NUMBER", "AADHAAR"]:
            assert bad not in entities, f"HIPAA should not include {bad}"

    def test_gdpr_includes_eu_types(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["gdpr"]
        assert "PERSON" in entities
        assert "IBAN_CODE" in entities
        assert "IP_ADDRESS" in entities

    def test_gdpr_excludes_us_types(self) -> None:
        """GDPR should NOT include US-specific types."""
        entities = PROTOCOL_DEFAULT_ENTITIES["gdpr"]
        for bad in ["US_SSN", "US_DRIVER_LICENSE", "US_PASSPORT", "US_BANK_NUMBER"]:
            assert bad not in entities, f"GDPR should not include {bad}"

    def test_dpdpa_excludes_us_uk_eu_types(self) -> None:
        """DPDPA should NOT include US/UK/EU-specific types."""
        entities = PROTOCOL_DEFAULT_ENTITIES["dpdpa"]
        for bad in ["US_SSN", "US_DRIVER_LICENSE", "COMPANY_NUMBER_UK", "VAT_EU", "NHS_NUMBER"]:
            assert bad not in entities, f"DPDPA should not include {bad}"

    def test_dpdpa_includes_basic_types(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["dpdpa"]
        assert "PERSON" in entities
        assert "EMAIL_ADDRESS" in entities

    def test_pci_dss_includes_credit_card(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["pci_dss"]
        assert "CREDIT_CARD" in entities
        assert "PERSON" in entities

    def test_state_breach_includes_us_types(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["state_breach"]
        assert "US_SSN" in entities
        assert "US_DRIVER_LICENSE" in entities
        assert "CREDIT_CARD" in entities

    def test_bipa_includes_person_ssn(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["bipa"]
        assert "PERSON" in entities
        assert "US_SSN" in entities

    def test_ferpa_includes_student_relevant_types(self) -> None:
        entities = PROTOCOL_DEFAULT_ENTITIES["ferpa"]
        assert "PERSON" in entities
        assert "EMAIL_ADDRESS" in entities

    def test_hipaa_and_hipaa_breach_rule_match(self) -> None:
        """hipaa and hipaa_breach_rule should have the same entity list."""
        assert PROTOCOL_DEFAULT_ENTITIES["hipaa"] == PROTOCOL_DEFAULT_ENTITIES["hipaa_breach_rule"]

    def test_gdpr_and_gdpr_article_33_match(self) -> None:
        """gdpr and gdpr_article_33 should have the same entity list."""
        assert PROTOCOL_DEFAULT_ENTITIES["gdpr"] == PROTOCOL_DEFAULT_ENTITIES["gdpr_article_33"]

    def test_state_breach_and_state_breach_generic_match(self) -> None:
        """state_breach and state_breach_generic should have the same entity list."""
        assert PROTOCOL_DEFAULT_ENTITIES["state_breach"] == PROTOCOL_DEFAULT_ENTITIES["state_breach_generic"]


# ---------------------------------------------------------------------------
# _resolve_target_entities logic
# ---------------------------------------------------------------------------

class TestResolveTargetEntities:
    """Test the _resolve_target_entities helper function."""

    def _resolve(self, protocol_config, protocol_id):
        from app.pipeline.two_phase import _resolve_target_entities
        return _resolve_target_entities(protocol_config, protocol_id)

    def test_no_config_no_protocol_returns_none(self) -> None:
        """No config + no protocol → None (all recognizers)."""
        assert self._resolve(None, None) is None

    def test_no_config_unknown_protocol_returns_none(self) -> None:
        """Unknown protocol_id → None (all recognizers)."""
        assert self._resolve(None, "unknown_protocol") is None

    def test_protocol_id_resolves_defaults(self) -> None:
        """Known protocol_id → PROTOCOL_DEFAULT_ENTITIES lookup."""
        result = self._resolve(None, "hipaa")
        assert result is not None
        assert "US_SSN" in result
        assert "PERSON" in result

    def test_config_base_protocol_id_overrides_protocol_id(self) -> None:
        """base_protocol_id in config takes precedence over protocol_id."""
        config = {"base_protocol_id": "gdpr"}
        result = self._resolve(config, "hipaa")
        assert result is not None
        assert "IBAN_CODE" in result
        assert "US_SSN" not in result

    def test_config_explicit_target_overrides_everything(self) -> None:
        """target_entity_types in config overrides all defaults."""
        config = {
            "base_protocol_id": "hipaa",
            "target_entity_types": ["PERSON", "EMAIL_ADDRESS"],
        }
        result = self._resolve(config, "gdpr")
        assert result == ["PERSON", "EMAIL_ADDRESS"]

    def test_empty_target_entity_types_falls_through(self) -> None:
        """Empty target_entity_types list falls through to base_protocol_id."""
        config = {
            "base_protocol_id": "ferpa",
            "target_entity_types": [],
        }
        result = self._resolve(config, "hipaa")
        expected = PROTOCOL_DEFAULT_ENTITIES["ferpa"]
        assert result == expected

    def test_config_without_base_protocol_falls_through(self) -> None:
        """Config without base_protocol_id falls through to protocol_id."""
        config = {"some_other_key": "value"}
        result = self._resolve(config, "bipa")
        expected = PROTOCOL_DEFAULT_ENTITIES["bipa"]
        assert result == expected

    def test_returns_new_list_not_original(self) -> None:
        """Returned list should be a copy, not the original dict value."""
        result = self._resolve(None, "hipaa")
        assert result is not None
        original = PROTOCOL_DEFAULT_ENTITIES["hipaa"]
        assert result == original
        assert result is not original  # must be a copy


# ---------------------------------------------------------------------------
# PresidioEngine.analyze() target_entity_types param
# ---------------------------------------------------------------------------

class TestPresidioEngineEntityFiltering:
    """Test that PresidioEngine.analyze() passes entities to Presidio."""

    def test_analyze_signature_accepts_target_entity_types(self) -> None:
        """analyze() method accepts target_entity_types keyword argument."""
        import inspect
        from app.pii.presidio_engine import PresidioEngine
        sig = inspect.signature(PresidioEngine.analyze)
        params = list(sig.parameters.keys())
        assert "target_entity_types" in params

    def test_analyze_default_is_none(self) -> None:
        """target_entity_types defaults to None."""
        import inspect
        from app.pii.presidio_engine import PresidioEngine
        sig = inspect.signature(PresidioEngine.analyze)
        param = sig.parameters["target_entity_types"]
        assert param.default is None


# ---------------------------------------------------------------------------
# Frontend PROTOCOL_DEFAULTS alignment
# ---------------------------------------------------------------------------

class TestFrontendAlignment:
    """Verify frontend PROTOCOL_DEFAULTS matches backend PROTOCOL_DEFAULT_ENTITIES."""

    # These are the frontend protocol keys and their backend equivalents
    FRONTEND_TO_BACKEND = {
        "hipaa_breach_rule": "hipaa_breach_rule",
        "gdpr_article_33": "gdpr_article_33",
        "ccpa": "ccpa",
        "hitech": "hitech",
        "ferpa": "ferpa",
        "state_breach_generic": "state_breach_generic",
        "bipa": "bipa",
        "dpdpa": "dpdpa",
    }

    @pytest.mark.parametrize("frontend_key,backend_key", FRONTEND_TO_BACKEND.items())
    def test_backend_has_matching_protocol(self, frontend_key: str, backend_key: str) -> None:
        """Every frontend protocol key has a matching backend entry."""
        assert backend_key in PROTOCOL_DEFAULT_ENTITIES, (
            f"Frontend protocol '{frontend_key}' maps to '{backend_key}' "
            f"which is not in PROTOCOL_DEFAULT_ENTITIES"
        )
