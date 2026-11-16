# Mira Bluetooth for Home Assistant

Controls and monitors the **Mira Platinum** digital shower over Bluetooth LE, through a local
Bluetooth adapter or an ESPHome Bluetooth proxy. Mira Activate and current-generation Mira
Mode valves speak the same protocol and are expected to work, but nobody has run this
integration on one yet: see [Which valves](#which-valves). No cloud, no account. Outlets, temperature, flow and the valve's stored presets become Home
Assistant entities, with the water temperature, flow, the last run and a water total as
sensors.

Built on [mira-ble](https://github.com/83noit/mira-ble), whose
[acknowledgements](https://github.com/83noit/mira-ble#acknowledgements) credit the projects it
draws on.

> **Status: early, run on one real valve.** It has run in one Home Assistant against one Mira
> Platinum, through an ESPHome Bluetooth proxy: pairing, polling, presets, outlets and stop.
> Everything else (other models, a local Bluetooth adapter) has only run against a simulated
> valve. See [What has been verified](#what-has-been-verified).

> **This drives real plumbing that produces hot water.** Home Assistant is not a safety
> system. Read [Safety](#safety) before writing an automation that starts water.

This project is not affiliated with Mira Showers or Kohler. Mira and Kohler are trademarks of
their owners.

## Which valves

| Valve | Status |
| --- | --- |
| Mira Platinum | Built for it and run on one, through an ESPHome proxy |
| Mira Activate, current Mira Mode | **Expected to work, untested.** Their protocol layer here is tested only against replies recorded from those valves by the projects below |
| Older Mira Mode (service `bccb0001-…`) | Not supported: a different protocol |

**If you have an Activate or a Mode**, the field-proven choice today is the project written
on your valve: [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) for the
Activate, and [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) (or the
[mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) library) for the Mode.

**Testers wanted.** If you are willing to try this integration on an Activate or a Mode,
please [open an issue](https://github.com/83noit/ha-mira-ble/issues) first. Start with pairing
and the read-only entities, attended, and attach the diagnostics download: it carries the raw
frames, which is what is needed to confirm or correct the layouts for your valve.

## What you need

- A Mira valve whose Bluetooth name looks like `Mira 0051#…` or `Mira 004C Bathroom` and that
  offers the service `267f0001-eb15-43f5-94c3-67d2221188f7`. Older Mira Mode valves offer
  `bccb0001-…` and speak an unrelated protocol; they are recognised and refused.
- Home Assistant 2026.11 or newer.
- **One** Bluetooth adapter or proxy that can make connections and hold a pairing, close to
  the valve: a local adapter on the Home Assistant host, or an ESPHome device with
  `bluetooth_proxy: active: true`. Proxies that only relay advertisements (Shelly) cannot be
  used. The pairing lives on the adapter that made it, and Home Assistant chooses the adapter
  for each connection by signal, so if two can connect near the valve only one of them will
  work: make the others scan-only.
- **On an ESPHome proxy, mind the firmware and the signal.** A proxy can lose the pairing
  without the valve being at fault; see [Known issues](#known-issues) before you choose
  where to put it and which ESPHome version to run.

## Install

**Marketplace (Home Assistant 2026.11+):** Settings → Marketplace → *Add from link* →
`https://github.com/83noit/ha-mira-ble` → install, then restart Home Assistant.

**Manually:** copy `custom_components/mira_ble` into your `config/custom_components/` and
restart Home Assistant.

Home Assistant installs the Python library `mira-ble` from PyPI automatically.

## Pair the valve

The valve only talks to devices it is paired with, and only accepts a new pairing while it is
in pairing mode.

1. Home Assistant discovers the valve (Settings → Devices & services). Select *Add*. You can
   also use *Add integration* → Mira Bluetooth and pick it from the list.
2. The first screen lists the adapters and proxies that hear the valve and can connect. If
   there is none, fix that first.
3. If pairing fails, close the Mira app on phones nearby and try again. A Mira Platinum was
   measured to accept two connections at once; other models are reported to take only one.
4. Put the valve in pairing mode. On a controller with a button, press and hold it for five
   seconds until it flashes; on a dial controller use Menu → Settings → Connect. Your manual
   has the steps for your model. Switching the valve's power off and on is reported to leave
   it pairable for a short while too.
5. Select **Submit** within about 30 seconds. Home Assistant connects, pairs, and reads the
   valve's name, serial number, outlets and presets. On a slow link this can take a couple of
   minutes.

If the adapter is already paired with the valve (for example you paired it with
`bluetoothctl`), skip step 4.

Later, *Reconfigure* (the integration's menu) pairs again and re-reads the outlets and
presets: use it after moving or replacing the adapter, or after changing presets in the Mira
app. It tries the existing pairing first and removes nothing unless the valve refuses it, so
running it by mistake does no harm.

## What you get

| Entity | Type | Notes |
| --- | --- | --- |
| One per outlet: *Hand shower*, *Overhead shower* or *Bath filler* when the valve says what the outlet feeds and no two are alike, otherwise *Outlet 1*, *Outlet 2* | Switch | On while the valve reports that outlet open. Turning the last one off stops the valve. |
| Target temperature, Target flow | Number | What the next start **from Home Assistant** uses. Kept across restarts; never taken from the valve, so a start does not inherit what the last person used at the wall. While water runs under an outlet they show the valve's own values, and changing one applies at once. |
| Stop | Button | Closes every outlet, including a running preset. Always available, retried up to three times. |
| Start *preset name*, one per preset stored in the valve | Button | Checks the preset first (see Safety), then sends the preset command once, as the start button of the wall controller does. See *Presets* below. |
| Running | Binary sensor | On while water runs: an outlet is open or flow is measured. A valve that is only holding (a Platinum pauses for two minutes after a warm-up or a stop) is off. |
| Fault | Binary sensor (diagnostic) | On when the valve reports an error. |
| Water temperature, Flow | Sensor | Measured at the valve. |
| Phase | Sensor | *Idle*, *Warming up*, *Paused* (held with the water off: ready after a warm-up, or just stopped), *Running a preset* or *Running*. |
| Running preset, Preset time remaining | Sensor | The preset the valve says is running, and the time it has left by the valve's own count. Unknown otherwise. |
| Water used this run | Sensor | Litres since the valve last left rest, as the valve counts them. Measured on a Mira Platinum; other models may always report zero. |
| Last run duration, volume, temperature | Sensor | The valve's own record of its last finished run. |
| Water used | Sensor | Litres, counted from those records; usable in the Water dashboard. |
| Outlet *n* maximum temperature, maximum run time | Sensor (diagnostic) | The valve's own limits, as set in the Mira app. |
| Error code | Sensor (diagnostic, disabled by default) | 1 means no error. |

**Action `mira_ble.start`**, on an outlet switch, with optional `temperature` and `flow`:
opens that outlet at values given for this start only. They are checked against the outlet's
own limits before anything is sent, and are not remembered.

**Action `mira_ble.run_preset`**, on a preset button: starts the preset and carries it past
its warm-up without another press (see below).

### Presets

On a Mira Platinum (measured) a preset runs in two steps, over Bluetooth as at the wall:

1. The preset command runs water until it is up to temperature (10 to 40 seconds), then the
   valve shuts it off and waits. *Phase* shows *Warming up*, then *Paused*. If nothing
   happens the valve goes back to rest by itself after two minutes.
2. The same command again starts the preset proper. The valve counts the preset's duration
   down (*Preset time remaining*), and *Phase* shows *Running a preset*.

So:

- **Pressing a Start button** warms the shower up and leaves it waiting for you. Press it
  again, or use the outlet button at the wall, when you are there.
- **`mira_ble.run_preset`** does both steps: it starts the preset again as soon as the
  warm-up has ended (checked every five seconds, for up to five minutes). Water then runs for
  the preset's whole duration. Stop, or any other command, cancels the second step.
- **An outlet switch, or the wall's outlet button, during the pause** starts a plain run: no
  preset number and no countdown, so only the outlet's maximum run time applies.
- The wall controller shows its timer only for a preset started on the controller itself.

A valve that runs a preset at once, with no warm-up, needs only the one command either way.

If the valve does not answer when asked for its presets (one Platinum at rest was seen not to; another answered),
the integration cannot list or check them. It then creates *Start preset 1* to *3*, disabled;
enable the ones your valve has. They run the slot unchecked.

*Water used* counts the valve's last-run record each time a new one appears. The record seen
when Home Assistant starts is a baseline and is not counted, two identical runs in a row count
once, and runs that finish while the valve is out of reach collapse into the latest.

## Safety

What the integration does:

- **Limits.** A target temperature is never sent above the outlet's own maximum (as set in
  the Mira app, re-read every half hour), and never above 48 °C whatever the valve allows.
  A preset is read before it runs and refused if it has been renamed, deleted, or set hotter
  than its outlets allow.
- **Confirmation.** A command counts as done only when the valve acknowledged it and its
  state shows it within a few seconds. "Stopped" means no open outlet and no measured flow,
  twice in a row. The stop is the vendor app's own (a pause that keeps the valve's settings);
  if water still runs, two other kinds of stop are tried in turn.
- **No stale commands.** The valve's state is read on the same connection before every
  command. Changing outlets on a running shower re-sends the temperature and flow the valve
  is running at, never a stored value.
- **Truthful failures.** If a command fails after it was sent, the entities show the last
  state the valve reported, polling speeds up and a fresh reading is taken at once. A start
  that could not be confirmed is followed by a stop, and the error says whether that stop
  was confirmed. If it says *CHECK THE SHOWER*, go and look.
- **No configuration writes.** The integration never changes limits, presets, credentials or
  commissioning state.

What it cannot do, and what you should do:

- **Never start water with nobody there.** Start from something a person does: a button, a
  notification they tap. Pair every remote start with an automatic stop.
- **The valve's own maximum run time is the real backstop** (shown per outlet; 30 minutes as
  shipped). If Home Assistant, the proxy or the link fails while water runs, that timer is
  what ends it. Unloading the integration or stopping Home Assistant does not stop the water.
- Do not raise the valve's limits because of this integration. Where children or vulnerable
  people shower, lower the outlet maximum in the Mira app; the integration follows it.
- Watch it behave before you rely on it.

## Use cases and examples

Offer to warm the shower, and start it only when someone accepts:

```yaml
automation:
  - alias: Offer to warm the shower
    triggers:
      - trigger: time
        at: "06:45:00"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: Warm the shower?
          data:
            actions:
              - action: WARM_SHOWER
                title: Yes
  - alias: Warm the shower when asked
    triggers:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: WARM_SHOWER
    actions:
      - action: mira_ble.start
        target:
          entity_id: switch.bathroom_overhead_shower
        data:
          temperature: 39
```

Stop it if nobody turns up (pair this with any remote start):

```yaml
automation:
  - alias: Stop the shower if nobody came
    triggers:
      - trigger: state
        entity_id: binary_sensor.bathroom_running
        to: "on"
        for: "00:05:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.bathroom_motion
        state: "off"
    actions:
      - action: button.press
        target:
          entity_id: button.bathroom_stop
```

A hard time limit, shorter than the valve's own:

```yaml
automation:
  - alias: Shower time limit
    triggers:
      - trigger: state
        entity_id: binary_sensor.bathroom_running
        to: "on"
        for: "00:15:00"
    actions:
      - action: button.press
        target:
          entity_id: button.bathroom_stop
```

Tell me when it is warm:

```yaml
automation:
  - alias: Shower is ready
    triggers:
      - trigger: numeric_state
        entity_id: sensor.bathroom_water_temperature
        above: 36
    conditions:
      - condition: state
        entity_id: binary_sensor.bathroom_running
        state: "on"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: The shower is warm.
```

*Running* stays on through one or two missed readings, so a `for:` timer is not reset by a
brief radio hiccup. *Water used* can feed the Water dashboard or a utility meter.

## How data is updated

The integration polls: it does not receive pushes.

- **Idle:** every 60 seconds, over one short connection that is dropped afterwards, so the
  Mira app can still connect in between.
- **Water running:** every 5 seconds, again over a connection of its own each time. Runs
  started at the wall are noticed at the next idle poll, within a minute.
- **Paused** (the two minutes a Mira Platinum holds after a warm-up or a stop): every 30
  seconds. Every poll is a connection, and connections are kept to what is needed.
- **Commands** connect on demand and are confirmed before they return. Expect a few seconds
  between pressing Stop and the water stopping: most of it is the connection.
- **The connection is never held open.** Measured through an ESPHome proxy, a link that has
  been up for a few seconds answers in 7 to 10 seconds where a new one answers in a third of
  a second, so a held link made Stop slow exactly when it mattered.
- **A missed reading** is tolerated: entities keep their last value for up to two failed
  polls and go unavailable on the third. *Stop*, the numbers and the preset buttons are
  always available. If a reading is missed while water runs, polling stays at 5 seconds for
  a while before backing off.
- **Reads the valve may not support** (the last-run record, the outlet limits) never fail a
  poll. A valve that stays silent on one is asked again after 1, 2, 4 … up to 32 polls.

Replies from the valve carry no identifier, so a request that goes unanswered closes the
connection: a late reply must not be taken for the answer to the next request. That is why a
slow valve shows up as a reconnect, not a wrong reading.

## What has been verified

| | Status |
| --- | --- |
| Frame format, checksum, state layout, outlet command, the all-zero stop, presets, outlet limits | Tested against replies and frames recorded from real Activate and Mode valves by [mira-mode-control](https://github.com/ryan-shaw/mira-mode-control), [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) and [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) |
| A Platinum's state through warm-up, hold, shower and stop | Measured on one Platinum with the library and built into the tests |
| "Run preset 0" as a stop | Measured on an earlier Platinum, before this library existed |
| The library on a Mira Platinum: pairing, reads, preset start, outlet command, pause and all-zero stop | Run by hand from macOS on one unit |
| Outlet count, outlet type, error code | Read from the vendor app's code only |
| This integration on a Mira Platinum through an ESPHome proxy (AtomS3, ESPHome 2026.9.1, ESP-IDF 5.5.5) | Run in one Home Assistant: pairing, the bond surviving proxy and Home Assistant restarts, idle polling, presets in both steps, outlet on and off, stop. A real shower was followed poll by poll. Stop on a running valve and `mira_ble.run_preset` were run on an earlier build with a different connection strategy and, on this one, only with the valve at rest (Stop acknowledged in about 2.4 s). The proxy lost the pairing once: see Known issues |
| Starting outlets directly on a Mira Platinum | Works on one unit, through the library. The vendor app does not do this: it controls the Platinum through the cloud |
| Local BlueZ adapter | **Not yet run** (the pairing code is shared with an integration that is) |
| Activate and Mode valves | **Not yet run** with this integration; the protocol layer is tested against their recordings. See [Which valves](#which-valves) |

## Known issues

### An ESPHome proxy can lose the pairing

Seen once, during a real shower, on an ESPHome proxy (ESP32-S3, ESP-IDF 5.5.5) that heard the
valve at -76 dBm: one connection dropped while it was being encrypted, and from then on the
valve refused every connection until it was paired again in pairing mode. Restarting the
proxy did not help.

The cause is in the proxy's Bluetooth stack, not in the valve or this integration. ESP-IDF
erases a stored pairing from flash whenever an encryption attempt fails, and a link that
drops at that moment counts as a failure. Every connection is a small chance of it, and this
integration connects once a minute when idle and every few seconds while water runs (see
[How data is updated](#how-data-is-updated) for why it does not hold the connection).

What helps:

- **A strong signal.** Put the proxy close to the valve. This makes a dropped link rare, on
  any firmware.
- **A firmware setting, from ESP-IDF 5.5.6** (ESPHome 2026.11 or later, which also works
  around the 5.5.6 fault below). Running on this project's proxy since 2026-11-16 (ESPHome
  2026.11.0b4, ESP-IDF 5.5.6): the pairing survives restarts and normal polling; a dropped
  link has not happened on it yet, so the setting's actual purpose is still unobserved:

  ```yaml
  esp32:
    framework:
      type: esp-idf
      sdkconfig_options:
        CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL: "n"
  ```

  With it the proxy keeps the pairing after a failed encryption. The other side of that: if
  the valve itself forgets the pairing (a reset, a replacement), the proxy goes on offering
  the old one, and this integration cannot yet make a proxy forget it.
- **A local Bluetooth adapter** on the Home Assistant host, where one can hear the valve,
  does not have this behaviour.

When it happens, Home Assistant asks you to pair again; nothing else brings the valve back.

### ESPHome 2026.10.x forgets pairings (ESP-IDF 5.5.6)

A separate fault ([esphome#21880](https://github.com/esphome/esphome/issues/21880)): after
the first connection the proxy forgets the pairing until it restarts. Pin the proxy to
ESP-IDF 5.5.5 (`esp32: framework: type: esp-idf, version: 5.5.5`) or use ESPHome 2026.11 or
later, which works around it.

## Known limitations

- How many connections a valve accepts at once varies. A Mira Platinum served two (a Mac and
  Home Assistant through a proxy) for five minutes without either failing, and kept
  advertising meanwhile; how many more it takes is not known. The Mira Mode is reported to take
  only one, so there the Mira app and this integration can block each other.
- Whether the integration's connections interfere with a wireless wall controller is not
  known; none was seen on a Mira Platinum. If your controller misbehaves while Home
  Assistant watches a run, please open an issue.
- Runs started at the wall can take up to a minute to appear.
- Presets cannot be created or edited from Home Assistant, and after changing them in the
  Mira app you need *Reconfigure* for the buttons to follow.
- The valve's Bluetooth address can change after a power cut, and its pairing does not
  follow the address. The valve then shows up as a new discovery saying it is already set
  up; pairing it once moves the existing entry, with its entities and history, to the new
  address.
- A proxy with few connection slots can be full when the valve is polled; the valve uses a
  slot for a few seconds at a time, every 5 seconds while it is active.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| *The valve refused to pair* | It was not in pairing mode, or the window had closed. Put it in pairing mode again and submit promptly. |
| *The link … could not be encrypted* | The proxy holds no pairing with the valve, or its pairing store is stuck. Restart the proxy; then pair with the valve in pairing mode. |
| *Could not connect* during setup, or GATT errors 133 or 62 in the log | Something else may hold the valve's connections, usually the Mira app. Close it everywhere and try again. |
| Pairing fails with *error 129* | That proxy cannot pair. Use a proxy that can, or a local adapter. |
| *No Bluetooth adapter or proxy that can make connections* | The proxy is scan-only, lacks `active: true`, has no free connection slot, or is out of range. |
| Entities unavailable after working | The Mira app is connected, the valve lost power, the proxy stopped scanning, or Home Assistant now reaches the valve through an adapter that holds no pairing. |
| Repair: *has not answered for an hour* | Follow it in order: close the app, restart the proxy, check only one proxy can connect. It deliberately does not ask you to pair again. |
| Repair: *the Bluetooth proxy is losing its pairing* | ESP-IDF 5.5.6 on the proxy. Pin 5.5.5 or update; pairing again will not help. |
| A repair asks you to pair again | The valve refused the stored pairing three polls in a row through the adapter that holds it. Through an ESPHome proxy this is usually the proxy having lost the pairing: see [Known issues](#known-issues). Pair again. |
| An action fails with *did not follow* or *stopped as a precaution* | The valve accepted the frame but its state did not change in time. Download diagnostics and open an issue. |
| A value looks wrong | Download diagnostics: they carry the raw state bytes and the last frames exchanged. |

Diagnostics (the integration's menu → *Download diagnostics*) have Bluetooth addresses, the
serial number, the valve's name and preset names removed, and name the proxy's firmware.

## Remove

Settings → Devices & services → Mira Bluetooth → the valve's menu → *Delete*. To also remove
the pairing, forget the valve on the adapter that holds it (`bluetoothctl remove <address>` on
a local adapter).

## Development

```sh
nix develop          # or: uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Until `mira-ble` is published, the tests run against a checkout of it in `../mira-ble`, so
this repository's CI cannot pass yet. `custom_components/mira_ble/quality_scale.yaml` records
the self-assessment against Home Assistant's integration quality scale.

## Licence

[MIT](LICENSE).
