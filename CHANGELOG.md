# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, a minor
release may change entities or behaviour.

## [Unreleased]

## [0.1.0] - 2026-11-16

First release. Run in one Home Assistant against one Mira Platinum through an ESPHome proxy.
Read the README's Known issues first: an ESPHome proxy can lose the pairing.

### Changed

- A valve that is only paused is polled every 30 s rather than every 5 s: every poll is a
  connection, and a connection that dropped while it was being set up cost an ESPHome proxy
  its record of the pairing during a real shower.
- The request to pair again points to the README's new Known issues section: an ESPHome
  proxy can lose the pairing when a connection drops while it is being encrypted.

### Fixed

- Phase no longer shows Running for the moment the last water drains at the end of a warm-up
  (mira-ble 0.1.1).

## [0.1.0b1] - 2026-11-16

First beta. Run in one Home Assistant against one Mira Platinum through an ESPHome proxy;
not yet run with a local Bluetooth adapter or with an Activate or Mode valve.

### Added

- Discovery by name and service, guided pairing, re-pairing (reauthentication) and reconfigure.
- Outlet switches, target temperature and flow (restored across restarts), stop and preset
  buttons, running and fault binary sensors, water temperature, flow, last-run, water-total
  and per-outlet limit sensors.
- `mira_ble.start` action: open an outlet at a temperature and flow for that start only.
- Phase, running preset, preset time remaining and water-used-this-run sensors, from the
  valve's own state.
- `mira_ble.run_preset` action: start a preset and carry it past its warm-up, for valves
  that warm a preset up and then wait (measured on a Mira Platinum). A press of a preset
  button sends the preset command once, as the wall controller's start button does.
- "Enable debug logging" also covers the Bluetooth link (`ble_bond`); per-frame lines are on
  `ble_bond.frames`, which stays at INFO unless set to debug.
- One short connection per poll and per command, never a held link: through an ESPHome proxy
  a link held for more than a few seconds was measured to answer in 7 to 10 s.
- Commands confirmed by reading the valve back; temperatures capped at each outlet's own
  maximum; presets checked before they run; an unconfirmed start is stopped.
- Repair issues for a valve that is heard but has not answered for an hour, and for a proxy
  that forgets its pairing (ESP-IDF 5.5.6).
- A valve is identified by its serial number and moves to a new Bluetooth address when it is
  paired there.
