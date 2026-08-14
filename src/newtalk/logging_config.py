import logging


FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("newtalk")
    logger.setLevel(level.upper())
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
        logger.addHandler(handler)

    for handler in logger.handlers:
        handler.setLevel(level.upper())

    return logger
