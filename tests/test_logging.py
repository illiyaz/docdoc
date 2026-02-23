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


def test_pii_filter_redacts_raw_value_assignment(caplog):
    logger = logging.getLogger("test.raw")
    logger.setLevel(logging.INFO)
    logger.filters = []
    logger.addFilter(PIISafeFilter())

    with caplog.at_level(logging.INFO, logger="test.raw"):
        logger.info("processing raw_value=john.doe@example.com for extraction")

    assert "john.doe@example.com" not in caplog.text
    assert "raw_value=[REDACTED]" in caplog.text
