# Mira Bluetooth for Home Assistant

Control and watch a **Mira Platinum** digital shower from Home Assistant, over Bluetooth. No
cloud, no account. It talks to the valve directly, through a Bluetooth adapter on your Home
Assistant machine or an ESPHome Bluetooth proxy.

You get a switch per outlet, the target temperature and flow, a button for each preset stored
in the valve, a Stop button, and sensors for the water temperature, the flow, the last shower
and how much water you've used.

Mira Activate and current Mira Mode valves speak the same language and should work too, but
nobody has tried this integration on one yet. See [Which valves](#which-valves).

It's built on the [mira-ble](https://github.com/83noit/mira-ble) library, which
[credits](https://github.com/83noit/mira-ble#acknowledgements) the projects it learned from.

> **Status: early days.** It runs in one Home Assistant, on one Mira Platinum, through an
> ESPHome proxy: pairing, readings, presets, outlets and Stop all work there. Anything else
> (other valves, a local Bluetooth adapter) has only been tried against a simulated valve. See
> [What has been verified](#what-has-been-verified).

> **This turns real hot water on and off.** Home Assistant is not a safety system. Read
> [Safety](#safety) before you write an automation that starts the shower.

This project has nothing to do with Mira Showers or Kohler. Mira and Kohler are their
trademarks.

## Which valves

| Valve | Status |
| --- | --- |
| Mira Platinum | Written for it, and running on one through an ESPHome proxy |
| Mira Activate, current Mira Mode | **Should work, not tried yet.** The code is tested against replies that other projects recorded from these valves |
| Older Mira Mode (service `bccb0001-…`) | Not supported: it speaks a different language |

**Got an Activate or a Mode?** For now, the safe bet is the project built on your valve:
[Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) for the Activate, and
[ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) (or the
[mira-mode-control](https://github.com/ryan-shaw/mira-mode-control) library) for the Mode.

**Happy to try this one instead?** That would help a lot. Please
[open an issue](https://github.com/83noit/ha-mira-ble/issues) first. Start with pairing and
the readings (no water), stay near the shower, and attach the diagnostics download: it holds
the raw messages, which is what's needed to check the details for your valve.

## What you need

- A Mira valve whose Bluetooth name looks like `Mira 0051#…` or `Mira 004C Bathroom`, and
  which offers the Bluetooth service `267f0001-eb15-43f5-94c3-67d2221188f7`. Older Mira Mode
  valves use `bccb0001-…` instead; the integration spots them and says no.
- Home Assistant 2026.11 or newer.
- **One** Bluetooth adapter or proxy near the valve that can connect and keep a pairing:
  either an adapter on the Home Assistant machine, or an ESPHome device with
  `bluetooth_proxy: active: true`. Proxies that only listen (Shelly devices, for example)
  can't be used. The pairing lives on the adapter that made it, and Home Assistant picks
  whichever adapter hears the valve best for each connection. So if two adapters near the
  valve can connect, only one of them will work: set the others to listen only.
- **If you use an ESPHome proxy, set it up like this** (the reasons are in
  [Known issues](#known-issues)):
  - **ESPHome 2026.11 or later.** ESPHome 2026.10 forgets pairings after one connection.
  - **Put it close to the valve.** On standard firmware, every dropped connection is a small
    chance of losing the pairing.
  - **Add this setting, so that a dropped connection doesn't wipe the pairing:**

    ```yaml
    esp32:
      framework:
        type: esp-idf
        sdkconfig_options:
          CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL: "n"
    ```

    It has one downside, if the valve is ever reset. [Known issues](#known-issues) explains
    it, and an optional second setting that deals with it.

## Install

**Marketplace (Home Assistant 2026.11+):** Settings → Marketplace → *Add from link* →
`https://github.com/83noit/ha-mira-ble` → install, then restart Home Assistant.

**By hand:** copy `custom_components/mira_ble` into your `config/custom_components/` and
restart Home Assistant.

Home Assistant fetches the `mira-ble` library from PyPI by itself.

## Pair the valve

The valve only talks to devices it's paired with, and it only accepts a new pairing while
it's in pairing mode.

1. Home Assistant finds the valve on its own (Settings → Devices & services). Select *Add*.
   You can also go *Add integration* → Mira Bluetooth and pick it from the list.
2. The first screen lists the adapters and proxies that can hear the valve and connect to
   it. If the list is empty, sort that out first.
3. Put the valve in pairing mode. On a controller with a button, hold it for five seconds
   until it flashes; on a dial controller, go Menu → Settings → Connect. Your manual has the
   steps for your model. Turning the valve's power off and on is said to leave it pairable
   for a little while too.
4. Select **Submit** within about 30 seconds. Home Assistant connects, pairs, and reads the
   valve's name, serial number, outlets and presets. On a weak signal this can take a couple
   of minutes.

If pairing fails, try closing the Mira app on phones nearby. A Mira Platinum happily takes
two connections at once, but other models are said to take only one.

If the adapter is already paired with the valve (say you paired it with `bluetoothctl`),
skip step 3.

Later on, *Reconfigure* (in the integration's menu) pairs again and re-reads the outlets and
presets. Use it after moving or replacing the adapter, or after changing presets in the Mira
app. It tries the existing pairing first and only removes it if the valve refuses it, so
running it by mistake does no harm.

## What you get

| Entity | Type | What it does |
| --- | --- | --- |
| One per outlet: *Hand shower*, *Overhead shower* or *Bath filler* if the valve says what each outlet is and they're all different, otherwise *Outlet 1*, *Outlet 2* | Switch | On while that outlet is open. Turning the last one off stops the shower. |
| Target temperature, Target flow | Number | What the next start **from Home Assistant** will use. They survive restarts and are never copied from the valve, so a start doesn't inherit whatever the last person picked at the wall. While water is running they show what the valve is doing, and changing one takes effect straight away. |
| Stop | Button | Closes every outlet, presets included. Always available, and tried up to three times. |
| Start *preset name*, one per preset in the valve | Button | Checks the preset first (see [Safety](#safety)), then sends the preset command once, just like the wall controller's start button. See [Presets](#presets). |
| Running | Binary sensor | On while water is coming out: an outlet is open or the valve measures flow. A valve that's only paused (a Platinum waits two minutes after a warm-up or a stop) counts as off. |
| Fault | Binary sensor (diagnostic) | On when the valve reports an error. |
| Water temperature, Flow | Sensor | As measured by the valve. |
| Phase | Sensor | *Idle*, *Warming up*, *Paused* (water off, waiting: either ready after a warm-up, or just stopped), *Running a preset* or *Running*. |
| Running preset, Preset time remaining | Sensor | Which preset the valve says is running, and how long it has left by the valve's own count. Unknown the rest of the time. |
| Water used this run | Sensor | Litres since the valve last woke up, as the valve counts them. Seen working on a Mira Platinum; other models may always say zero. |
| Last run duration, volume, temperature | Sensor | The valve's own record of its last finished shower. |
| Water used | Sensor | A running total in litres, added up from those records. Works with the Water dashboard. |
| Outlet *n* maximum temperature, maximum run time | Sensor (diagnostic) | The valve's own limits, as set in the Mira app. |
| Error code | Sensor (diagnostic, off by default) | 1 means all is well. |

**Action `mira_ble.start`**, on an outlet switch, with optional `temperature` and `flow`:
opens that outlet with those values, for this start only. They're checked against the
outlet's own limits before anything is sent, and they aren't remembered.

**Action `mira_ble.run_preset`**, on a preset button: starts the preset and takes it past the
warm-up without a second press (see below).

### Presets

On a Mira Platinum a preset runs in two steps, whether you start it from Home Assistant or
from the wall:

1. **Warm-up.** Water runs until it's hot enough (10 to 40 seconds), then the valve turns it
   off and waits. *Phase* shows *Warming up*, then *Paused*. If nothing happens, the valve
   goes back to sleep after two minutes.
2. **The shower itself.** Sending the same preset again starts it for real. The valve counts
   the preset's time down (*Preset time remaining*) and *Phase* shows *Running a preset*.

What that means in practice:

- **Pressing a Start button** warms the shower up and leaves it waiting for you. When you're
  there, press it again, or press the outlet button on the wall.
- **`mira_ble.run_preset`** does both steps for you: as soon as the warm-up is done (it checks
  every five seconds, for up to five minutes) it starts the preset. The water then runs for
  the whole length of the preset. Stop, or any other command, cancels the second step.
- **Turning on an outlet switch, or pressing the wall's outlet button, while it's waiting**
  starts a plain shower instead: no preset number and no countdown, so only the outlet's own
  maximum run time applies.
- The wall controller only shows its timer for presets it started itself.

A valve that starts presets straight away, with no warm-up, only ever needs the one command.

If the valve won't answer when asked for its presets (one Platinum didn't while idle, another
did), the integration can't list or check them. It then adds *Start preset 1* to *3*, turned
off. Turn on the ones your valve actually has; they start that slot without checking it.

*Water used* adds up the valve's last-shower record each time a new one appears. The record
already there when Home Assistant starts isn't counted, two identical showers in a row count
once, and showers that end while the valve is out of reach only count the latest.

## Safety

What the integration does for you:

- **Temperature limits.** It never asks for more than the outlet's own maximum (as set in the
  Mira app, re-read every half hour), and never more than 48 °C, whatever the valve allows. A
  preset is read before it runs, and refused if it's been renamed, deleted, or set hotter than
  its outlets allow.
- **Checks that commands happened.** A command only counts as done when the valve has said
  yes *and* its state shows the change within a few seconds. "Stopped" means no outlet open
  and no flow, twice in a row. Stop uses the same "pause" the Mira app uses (it keeps the
  valve's settings); if water is still running after that, it tries two other ways to stop.
- **No stale commands.** It reads the valve's state on the same connection before every
  command. When you switch outlets mid-shower, it re-sends the temperature and flow the valve
  is actually running at, never a remembered value.
- **Tells you when it isn't sure.** If something goes wrong after a command was sent, the
  entities show the last thing the valve reported, readings speed up, and it reads the valve
  again straight away. If a start can't be confirmed, it sends a stop, and the error says
  whether that stop worked. If you see *CHECK THE SHOWER*, go and look.
- **Leaves the valve's settings alone.** It never changes limits, presets, Wi-Fi details or
  installer settings.

What it can't do, and what you should do:

- **Never start water with nobody there.** Start the shower from something a person does: a
  button, or a notification they tap. Give every remote start an automatic stop.
- **The valve's own maximum run time is the last line of defence** (shown per outlet; 30
  minutes out of the box). If Home Assistant, the proxy or the connection fails while water
  is running, that timer is what turns it off. Removing the integration or stopping Home
  Assistant doesn't stop the water.
- Don't raise the valve's limits for this integration. Where children or vulnerable people
  shower, lower the outlet maximum in the Mira app; the integration follows it.
- Watch how it behaves before you rely on it.

## Examples

Offer to warm the shower, and only start it if someone says yes:

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

Tell me when it's warm:

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

*Running* stays on through one or two missed readings, so a brief radio hiccup won't reset a
`for:` timer. *Water used* can feed the Water dashboard or a utility meter.

## How data is updated

The valve doesn't send updates by itself, so the integration asks it.

- **Idle:** once a minute, with a quick connection that's closed straight after. Showers
  started at the wall show up at the next check, so within a minute.
- **Water running:** every 5 seconds, each time with a fresh connection.
- **Paused** (the two minutes a Mira Platinum waits after a warm-up or a stop): every 30
  seconds. Each check is a connection, so it doesn't make more than it needs.
- **Commands** connect when needed and wait until the valve confirms them. Expect a few
  seconds between pressing Stop and the water stopping; most of that is connecting.
- **It never keeps the connection open.** Through an ESPHome proxy, a connection that's been
  open for a few seconds took 7 to 10 seconds to answer, where a fresh one answers in a third
  of a second. Keeping it open made Stop slow exactly when it mattered.
- **A missed reading is fine:** entities keep their last value through up to two failed
  checks and go unavailable on the third. *Stop*, the numbers and the preset buttons are
  always available. If a reading is missed while water is running, it keeps checking every
  5 seconds for a while before slowing down.
- **Some reads are optional** (the last-shower record, the outlet limits) and never make a
  check fail. If the valve ignores one, it's asked again after 1, 2, 4… up to 32 checks.

The valve's replies don't say which question they answer. So if a question goes unanswered,
the integration closes the connection rather than risk taking a late reply as the answer to
the next question. That's why a slow valve shows up as a reconnect, not as a wrong reading.

## What has been verified

| | Status |
| --- | --- |
| Message format, checksum, the state reading, the outlet command, the all-zero stop, presets, outlet limits | Tested against replies and messages that [mira-mode-control](https://github.com/ryan-shaw/mira-mode-control), [ha-mira-mode](https://github.com/HaydenGriffin/ha-mira-mode) and [Mira-Activate-HA](https://github.com/jonwilliams84/Mira-Activate-HA) recorded from real Activate and Mode valves |
| A Platinum's state through warm-up, waiting, showering and stopping | Recorded from one Platinum, and part of the tests |
| "Run preset 0" as a way to stop | Seen working on an earlier Platinum, before this library existed |
| The library on a Mira Platinum: pairing, readings, starting a preset, the outlet command, both ways of stopping | Run by hand from a Mac on one valve |
| Outlet count, outlet type, error code | Only read from the Mira app's code |
| This integration on a Mira Platinum through an ESPHome proxy (AtomS3) | Running in one Home Assistant: pairing, the pairing surviving restarts of the proxy and of Home Assistant, idle readings, presets in both steps, outlets on and off, Stop. A real shower was followed reading by reading. Stop on a running valve and `mira_ble.run_preset` were tried on an earlier version that kept the connection open, and on this one only with the valve idle (Stop answered in about 2.4 s). The proxy lost the pairing once: see [Known issues](#known-issues) |
| Starting outlets directly on a Mira Platinum | Works on one valve, through the library. The Mira app doesn't do this: it controls the Platinum through the cloud |
| A local Bluetooth adapter (BlueZ) | **Not tried yet** (the pairing code is shared with another integration where it works) |
| Activate and Mode valves | **Not tried yet** with this integration; the code is tested against their recordings. See [Which valves](#which-valves) |

## Known issues

The valve only talks over an encrypted connection, using a pairing that both it and your
adapter remember. Home Assistant connects once a minute, and every few seconds while water is
running (see [How data is updated](#how-data-is-updated) for why it doesn't just stay
connected), and every connection uses that pairing. Through an ESPHome proxy, two things can
go wrong.

### Home Assistant asks you to pair the valve again

**Why.** On standard firmware, an ESPHome proxy deletes a pairing whenever using it fails,
and a connection that drops at the wrong moment counts as failing. That's how ESP-IDF's
Bluetooth code works; it isn't a fault in the valve or in this integration. It happened once
on this project's proxy, which heard the valve at -76 dBm (not a strong signal). After that
the valve turned down every connection until it was paired again. Restarting the proxy
doesn't bring the pairing back.

**What to do.** Put the valve in pairing mode and follow the request to pair again.

**How to avoid it.** Use the [recommended proxy setup](#what-you-need). The setting
`CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL: "n"` makes the proxy keep the pairing
when a connection drops, and a strong signal makes dropped connections rare. This project's
proxy has run with that setting since 2026-11-16, through restarts and everyday use; no
connection has dropped on it yet, so the setting hasn't been put to the test. A Bluetooth
adapter on the Home Assistant machine doesn't have this problem.

ESPHome 2026.10 has a different bug with the same symptom: it forgets every pairing after one
connection, until the proxy restarts
([esphome#21880](https://github.com/esphome/esphome/issues/21880)). ESPHome 2026.11 fixes it.
If Home Assistant asks you to pair again within an hour of pairing, the message says so.

### Pairing fails even though the valve is in pairing mode

**Why.** With the setting above, the proxy never throws a pairing away on its own. If the
valve forgets its half (after a reset, or if it's replaced), the proxy keeps offering the
old key, the valve keeps saying no, and pairing through that proxy fails every time. Rare,
but it won't fix itself.

**What to do**, either:

- **Let the proxy drop a key the valve says it doesn't have.** Add this under the first
  setting:

  ```yaml
  CONFIG_BT_BLE_SMP_UNBOND_ON_KEY_MISSING: "y"
  ```

  When the valve answers "I don't have that key", the proxy forgets its copy and pairing
  works straight away; a connection that just drops still keeps the pairing. The catch,
  according to Espressif: a device in Bluetooth range pretending to be the valve could send
  the same answer, make the proxy forget the pairing, and then pair with it using weaker
  security. That's why it's off by default. Nobody has checked yet that a reset Mira valve
  actually sends that answer.
- **Or clear the old key once.** Rebuild the proxy without
  `CONFIG_BT_BLE_SMP_REMOVE_BOND_ON_PAIR_FAIL_AS_CENTRAL`, pair the valve (the first failed
  try wipes only the valve's old key, so try again if the first one fails), then rebuild with
  the setting back in. Anything else paired with the proxy keeps its pairing.

A Bluetooth adapter on the Home Assistant machine needs neither: the pairing screen makes it
forget the old key by itself.

## Known limitations

- How many connections a valve takes at once varies. A Mira Platinum handled two (a Mac and
  Home Assistant through a proxy) for five minutes without trouble, and kept advertising the
  whole time; nobody knows how many more it would take. The Mira Mode reportedly takes only
  one, so there the Mira app and this integration can get in each other's way.
- Nobody knows yet whether these connections bother a wireless wall controller; nothing odd
  was seen on a Mira Platinum. If yours misbehaves while Home Assistant is watching a shower,
  please open an issue.
- Showers started at the wall can take up to a minute to appear.
- You can't create or edit presets from Home Assistant, and after changing them in the Mira
  app you need *Reconfigure* for the buttons to catch up.
- The valve's Bluetooth address can change after a power cut, and its pairing doesn't follow
  it. The valve then shows up as a new discovery that says it's already set up; pairing it
  once moves your existing setup, entities and history included, to the new address.
- A proxy with few connection slots can be full when the valve is checked. The valve uses a
  slot for a few seconds at a time, every 5 seconds while water is running.

## Troubleshooting

| What you see | Likely cause and fix |
| --- | --- |
| *The valve refused to pair* | It wasn't in pairing mode, or the window closed. Put it back in pairing mode and press Submit promptly. If it keeps failing through an ESPHome proxy while the valve really is in pairing mode, the proxy may have an old key: see [Known issues](#known-issues). |
| *The link … could not be encrypted* | The proxy has no pairing with the valve, or its pairing store is stuck. Restart the proxy, then pair with the valve in pairing mode. |
| *Could not connect* while setting up, or GATT errors 133 or 62 in the log | Something else may be connected to the valve, usually the Mira app. Close it everywhere and try again. |
| Pairing fails with *error 129* | That proxy can't pair. Use one that can, or a local adapter. |
| *No Bluetooth adapter or proxy that can make connections* | The proxy only listens, is missing `active: true`, has no free connection slot, or is out of range. |
| Entities go unavailable after working | The Mira app is connected, the valve lost power, the proxy stopped scanning, or Home Assistant is now reaching the valve through an adapter that isn't paired with it. |
| Repair: *has not answered for an hour* | Work through it in order: close the app, restart the proxy, check that only one proxy can connect. It deliberately doesn't ask you to pair again. |
| Repair: *the Bluetooth proxy lost its pairing soon after pairing* | ESPHome 2026.10 does this. Update the proxy to ESPHome 2026.11 or later, then pair again. |
| A repair asks you to pair again | The valve turned down the stored pairing three times in a row through the adapter that holds it. Through an ESPHome proxy, that usually means the proxy lost the pairing. Pair again, and see [Known issues](#known-issues) to stop it happening. |
| An action fails with *did not follow* or *stopped as a precaution* | The valve accepted the command but its state didn't change in time. Download the diagnostics and open an issue. |
| A value looks wrong | Download the diagnostics: they include the raw readings and the last messages exchanged. |

The diagnostics (integration menu → *Download diagnostics*) leave out Bluetooth addresses,
the serial number, the valve's name and the preset names, and include the proxy's firmware
version.

## Remove

Settings → Devices & services → Mira Bluetooth → the valve's menu → *Delete*. To remove the
pairing too, make the adapter forget the valve (`bluetoothctl remove <address>` on a local
adapter).

## Development

```sh
nix develop          # or: uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The tests use the released `mira-ble` from PyPI, the version the manifest asks for.
`custom_components/mira_ble/quality_scale.yaml` records how the integration measures up
against Home Assistant's integration quality scale.

## Licence

[MIT](LICENSE).
