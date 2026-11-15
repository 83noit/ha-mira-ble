# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, a minor
release may change entities or behaviour.

## [Unreleased]

Nothing here has yet run on a real valve.

### Added

- Discovery by name and service, guided pairing, re-pairing (reauthentication) and reconfigure.
- Outlet switches, target temperature and flow (restored across restarts), stop and preset
  buttons, running and fault binary sensors, water temperature, flow, last-run, water-total
  and per-outlet limit sensors.
- `mira_ble.start` action: open an outlet at a temperature and flow for that start only.
- One short connection per poll while idle; a held link while the valve is active.
- Commands confirmed by reading the valve back; temperatures capped at each outlet's own
  maximum; presets checked before they run; an unconfirmed start is stopped.
- Repair issues for a valve that is heard but has not answered for an hour, and for a proxy
  that forgets its pairing (ESP-IDF 5.5.6).
- A valve is identified by its serial number and moves to a new Bluetooth address when it is
  paired there.
