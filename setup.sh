#!/usr/bin/env bash
# Install every dependency of the SDN load balancer.
#   ./setup.sh            Ubuntu 20.04/22.04: Python 3.8 + Ryu 4.34 (requirements.txt)
#                         Kali / Debian / other: system Python 3 + os-ken (requirements-modern.txt)
#   ./setup.sh --modern   force the os-ken path on Ubuntu too
# Creates ./venv and writes the controller backend (ryu | os-ken) to .controller_backend.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- choose path
DISTRO="unknown"
[ -r /etc/os-release ] && DISTRO="$(. /etc/os-release && echo "${ID:-unknown}")"
MODE=classic
if [ "${1:-}" = "--modern" ] || [ "$DISTRO" != "ubuntu" ]; then
    MODE=modern
fi
say "Detected distro: $DISTRO -> using the $MODE install path"

# ---------------------------------------------------------------- system packages
say "Installing system packages (Mininet, Open vSwitch, traffic tools)"
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iperf iperf3 hping3 curl net-tools git \
    python3-pip python3-venv python3-dev
# A test controller, if present, would grab port 6653; we use our own controller.
sudo systemctl stop openvswitch-testcontroller 2>/dev/null || true
sudo systemctl disable openvswitch-testcontroller 2>/dev/null || true
sudo systemctl enable --now openvswitch-switch

# Old apt Mininet (2.2.x on Ubuntu 20.04) is too old; the project needs 2.3+.
MN_VERSION="$(sudo python3 -c 'import mininet.net as m; print(m.VERSION)' 2>/dev/null || echo 0)"
if ! printf '2.3\n%s\n' "$MN_VERSION" | sort -V -C; then
    say "Mininet $MN_VERSION is older than 2.3, installing Mininet 2.3 from source"
    rm -rf /tmp/mininet-src
    git clone --depth 1 --branch 2.3.1b4 https://github.com/mininet/mininet /tmp/mininet-src
    (cd /tmp/mininet-src && sudo PYTHON=python3 util/install.sh -n)
fi

# ---------------------------------------------------------------- python env
install_modern() {
    # $1 = python interpreter for the venv
    rm -rf venv
    "$1" -m venv venv
    # shellcheck disable=SC1091
    source venv/bin/activate
    pip install --upgrade pip wheel
    if ! pip install -r requirements-modern.txt; then
        say "Pinned versions not available for $(python --version); installing latest compatible versions"
        sed -E 's/==.*$//' requirements-modern.txt | grep -vE '^\s*(#|$)' > /tmp/sdnlb_req_unpinned.txt
        pip install -r /tmp/sdnlb_req_unpinned.txt
    fi
    # Import through the project's own module: os_ken.topology.api inspects its
    # importer's stack frame and crashes when imported straight from `python -c`.
    PYTHONPATH="$(pwd)" python -c "import controller.compat as c; assert c.BACKEND == 'os-ken'" \
        || { echo "os-ken install check failed"; exit 1; }
    BACKEND=os-ken
}

if [ "$MODE" = modern ]; then
    PY=python3
    say "Creating virtualenv ./venv with $($PY --version) and os-ken"
    install_modern "$PY"
else
    say "Finding Python 3.8/3.9 for the Ryu virtualenv"
    PY=""
    for candidate in python3.8 python3.9; do
        if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
    done
    if [ -z "$PY" ]; then
        say "Installing Python 3.8 from the deadsnakes PPA"
        sudo apt-get install -y software-properties-common
        sudo add-apt-repository -y ppa:deadsnakes/ppa
        sudo apt-get update
        sudo apt-get install -y python3.8 python3.8-venv python3.8-dev
        PY=python3.8
    fi
    sudo apt-get install -y "${PY}-venv" "${PY}-dev" 2>/dev/null || true

    say "Creating virtualenv ./venv with $PY and Ryu"
    rm -rf venv
    "$PY" -m venv venv
    # shellcheck disable=SC1091
    source venv/bin/activate
    pip install --upgrade "pip<24.1"
    # Ryu's setup.py breaks with modern setuptools; pin an old one before installing it.
    pip install "setuptools==57.5.0" wheel
    BACKEND=ryu
    if ! pip install --no-build-isolation -r requirements.txt \
            || ! python -c "import ryu.app.wsgi, ryu.topology.api" 2>/dev/null; then
        say "Ryu failed to install or import, switching to the os-ken path"
        deactivate || true
        install_modern python3
    fi
fi
echo "$BACKEND" > .controller_backend

mkdir -p logs data ai/data ai/models experiments/results
chmod +x run.sh stop.sh demo.sh

say "Checking the install"
python -c "import networkx, sklearn, flask_socketio; print('python deps OK')"
sudo mn --version
echo "controller backend: $BACKEND"
say "Setup complete. Start everything with ./run.sh"
