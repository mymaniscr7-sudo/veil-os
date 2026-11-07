# VEIL systemd units (host services)

The broker and Security Center died 4 times in ~24h on the dev host
(2026-11-06), each time needing a manual restart. These units make
systemd the watchdog instead.

## Units

- `veil-broker.service` — `veil-core/broker_service.py --serve`,
  the permission broker. `Restart=always`, 10s delay, no start-rate
  limit.
- `veil-security-center.service` — `veil-desktop/server.py --port 8765`,
  the control UI. `Requires` + `After` the broker, and waits up to 30s
  for the broker socket to answer before starting, so it never races
  the broker unit and spawns a second broker. systemd also restarts
  the Security Center when the broker restarts, so it always talks to
  the fresh broker (verified live with `kill -9` tests, 2026-11-06).

## Install / manage

```bash
bash systemd/install.sh          # copy to /etc/systemd/system, enable, start
systemctl status veil-broker veil-security-center
systemctl restart veil-security-center
systemctl stop veil-broker veil-security-center     # stop both (broker first)
systemctl disable veil-broker veil-security-center  # stop auto-start on boot
```

## Honest limits

- A restart wipes in-memory approvals by design. The watchdog makes
  the outage short; it does not preserve approvals.
- The cause of the deaths is not proven (tight host memory makes the
  OOM killer plausible, no dmesg evidence). If the host keeps killing
  them, the units will keep restarting them — watch
  `systemctl status` / `journalctl -u veil-broker` for a restart loop.
- `/etc/systemd/system` does **not** survive a VM replacement (only
  `~` does). The unit files live here in the repo, and
  `setup-dev-host.sh` reinstalls them, so recovery is one script.
- Do not run the Docker image's container with host-published port
  8765 while these units are up — the port is already taken. Stop the
  units first, or let the container be the running VEIL.
