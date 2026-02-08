import logging

from app.core.logging import PIISafeFilter


def test_pii_filter_redacts_email_and_ssn(caplog):
    logger = logging.getLogger("test.pii")
    logger.setLevel(logging.INFO)
    logger.filters = []
    logger.addFilter(PIISafeFilter())

    with caplog.at_level(logging.INFO, logger="test.pii"):
        logger.info("Contact john.doe@example.com SSN 123-45-6789")

    assert "john.doe@example.com" not in caplog.text
    assert "123-45-6789" not in caplog.text
    assert "[REDACTED]" in caplog.text
