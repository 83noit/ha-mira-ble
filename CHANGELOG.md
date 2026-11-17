# Changelog

Notable changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, a minor
release may change entities or behaviour.

## [Unreleased]

## [0.1.3] - 2026-11-17

### Changed

- The HACS check now runs in full. It used to skip the brand-image check, left over from
  before the integration had its own icon; the icon is there, so nothing is skipped. No
  change to the integration itself.

## [0.1.2] - 2026-11-16

### Changed

- *Target flow* moves in whole litres per minute, like the Mira app's own flow scale. With
  half-litre steps the slider showed "12.0 L/min", which spilled out of Home Assistant's value
  box. The `mira_ble.start` action's flow field steps the same way; it still accepts any value
  within the outlet's limits.

## [0.1.1] - 2026-11-16

### Changed

- The README now says what's actually been tried: the integration is for the Mira Platinum
  first. Activate and Mode valves should work but haven't been tried; the README points to the
  projects built on them and asks for testers.
- A Mira Platinum was seen taking two connections at once, so the "close the Mira app, the
  valve takes one connection" advice is softened.
- The Known issues section is organised around what you'd actually see (Home Assistant asking
  you to pair again, or pairing failing while the valve is in pairing mode), with the cause
  and your options for each. The recommended ESPHome proxy setup moves up to "What you need".
- The README is rewritten in plainer English.

### Fixed

- The repair you get when a proxy loses the pairing within an hour of pairing told you to go
  back to ESP-IDF 5.5.5 and said pairing again wouldn't help. It now names both known causes
  and tells you to pair again once the proxy is sorted.
- The pairing error now mentions that, after a valve reset, a proxy may still be holding an
  old key.

## [0.1.0] - 2026-11-16

First release. It runs in one Home Assistant, on one Mira Platinum, through an ESPHome proxy.
Read the README's Known issues first: an ESPHome proxy can lose the pairing.

### Changed

- A valve that's only paused is checked every 30 s instead of every 5 s. Every check is a
  connection, and during a real shower a connection that dropped while it was being set up
  cost an ESPHome proxy its pairing.
- The request to pair again points to the new Known issues section in the README.

### Fixed

- *Phase* no longer shows *Running* for the moment the last water drains at the end of a
  warm-up (mira-ble 0.1.1).

## [0.1.0b1] - 2026-11-16

First beta. Runs in one Home Assistant, on one Mira Platinum, through an ESPHome proxy; not
tried yet with a local Bluetooth adapter or with an Activate or Mode valve.

### Added

- Finding the valve by its name and Bluetooth service, guided pairing, pairing again, and
  Reconfigure.
- A switch per outlet, target temperature and flow (kept across restarts), Stop and preset
  buttons, Running and Fault sensors, water temperature, flow, last shower, a water total and
  each outlet's limits.
- The `mira_ble.start` action: open an outlet at a given temperature and flow, for that start
  only.
- Phase, running preset, preset time remaining and water-used-this-shower sensors, straight
  from the valve.
- The `mira_ble.run_preset` action: start a preset and take it past the warm-up, for valves
  that warm up first and then wait (seen on a Mira Platinum). Pressing a preset button sends
  the preset once, just like the wall controller's start button.
- "Enable debug logging" also covers the Bluetooth connection code (`ble_bond`). The
  message-by-message lines are on `ble_bond.frames`, which stays quiet unless you set it to
  debug.
- A fresh, short connection for every check and every command, never one kept open: through
  an ESPHome proxy, a connection kept open more than a few seconds was measured to take 7 to
  10 s to answer.
- Commands are checked by reading the valve back; temperatures are capped at each outlet's own
  maximum; presets are checked before they run; a start that can't be confirmed is followed
  by a stop.
- Repairs for a valve that Home Assistant can hear but hasn't been able to read for an hour,
  and for a proxy that forgets its pairing.
- The valve is identified by its serial number, so if its Bluetooth address changes, pairing
  it once moves your existing setup to the new address.
