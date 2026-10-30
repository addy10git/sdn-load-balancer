"""Stand-in for `ryu-manager` when the os-ken fallback is installed.

Current os-ken releases are a library without a manager command, so this does
what ryu-manager does: monkey-patch with eventlet, parse the same CLI options
(--observe-links, --ofp-tcp-listen-port, ...) and run the given apps.

    python -m controller.osken_launcher --observe-links --ofp-tcp-listen-port 6653 \
        controller.sdn_controller
"""
import os

# os-ken 4.x defaults to native OS threads; this controller (like Ryu) is written
# for eventlet's cooperative green threads, so select the eventlet hub. It must be
# set before os_ken.lib.hub is imported.
os.environ.setdefault("OSKEN_HUB_TYPE", "eventlet")

from os_ken.lib import hub  # noqa: E402

hub.patch(thread=False)

import logging  # noqa: E402
import sys  # noqa: E402

from os_ken import cfg  # noqa: E402
from os_ken.base.app_manager import AppManager  # noqa: E402  (must precede controller import)
import os_ken.controller.controller  # noqa: E402,F401  (registers --ofp-* options)
import os_ken.topology.switches  # noqa: E402,F401  (registers --observe-links)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    takes_value = ("--ofp-tcp-listen-port", "--ofp-listen-host")
    options, apps = [], []
    args = iter(argv)
    for arg in args:
        if arg in takes_value:
            options += [arg, next(args)]
        elif arg.startswith("-"):
            options.append(arg)
        else:
            apps.append(arg)
    cfg.CONF(options, project="os_ken")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    AppManager.run_apps(apps or ["controller.sdn_controller"])


if __name__ == "__main__":
    main()
