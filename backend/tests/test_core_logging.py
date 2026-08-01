from app.core.logging import configure_logging, get_logger


def test_configure_logging_idempotent():
    configure_logging()
    configure_logging()


def test_get_logger_binds_kwargs():
    log = get_logger(service="t", run_id="r1")
    assert log is not None
    log.info("hello", x=1)
