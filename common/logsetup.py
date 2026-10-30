"""Logging setup: every component writes a timestamped log file into logs/."""
import logging
import os
import time

import config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(component, level=logging.INFO):
    """Attach a file handler (logs/<component>_<timestamp>.log) to the root logger.

    Safe to call more than once: a second call for the same component is ignored.
    Returns the path of the log file.
    """
    os.makedirs(config.LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_sdn_component", None) == component:
            return handler.baseFilename

    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, "%s_%s.log" % (component, stamp))
    file_handler = logging.FileHandler(path)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler._sdn_component = component
    root.addHandler(file_handler)

    # Make sure something also reaches the console when nothing else configured it.
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)

    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path
