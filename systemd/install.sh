#!/bin/bash
# Install VEIL systemd units (host services). Run as root.
# /etc does NOT survive a VM replacement — these files live in the repo,
# so re-running this script (or setup-dev-host.sh, which calls it)
# after a replacement restores them.
set -e
cd "$(dirname "$0")"
for unit in veil-broker.service veil-security-center.service; do
  cp "$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now veil-broker.service veil-security-center.service
systemctl status veil-broker.service veil-security-center.service --no-pager
