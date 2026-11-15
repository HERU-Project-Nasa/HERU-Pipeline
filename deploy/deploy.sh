#!/bin/sh
set -eu

main() {
    cd "$(dirname "$0")/.."
    sudo -u heru git pull --ff-only
    sudo -u heru .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
    sudo -u heru .venv/bin/python -m pytest -q -p no:cacheprovider
    install -m 644 deploy/heru-atualizar.service /etc/systemd/system/heru-atualizar.service
    systemctl daemon-reload
    systemctl try-restart heru-atualizar
    systemctl --no-pager --lines=0 status heru-atualizar || true
}

main "$@"
exit
