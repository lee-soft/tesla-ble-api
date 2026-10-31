# tesla-ble-api

A tiny, dependency-free HTTP REST API that drives a Tesla over **Bluetooth Low
Energy** — so anything on your LAN (a smart-home trigger, a doorbell, a button,
a cron job) can "open the trunk" or "flash the lights" with a single HTTP call.

It wraps [`tesla-control`](https://github.com/teslamotors/vehicle-command)
(Tesla's official vehicle-command tool) and keeps **one long-lived BLE session**
open, which is the whole trick to making BLE control reliable on cheap SBC
hardware. No Tesla cloud account, no Fleet API billing, no internet required —
the commands are signed locally and sent straight to the car over Bluetooth.

> Built on a Khadas Edge2 sitting on a windowsill next to the driveway, driving
> a 2024 Model 3 Highland. It started life as the brain of a Halloween "treats
> in the trunk" prop and turned into something generally useful. Should work on
> any Linux box with Bluetooth near the car (Raspberry Pi, mini-PC, SBC).

## Why BLE and not the cloud API?

2021+ vehicles (and all 2024 Highland / Juniper cars) require **every command to
be cryptographically signed**. The old Owner API is dead. Your options are the
Fleet API (cloud, needs a developer account, rate-limited, bills you) or the
**local BLE transport** — which carries the *exact same signed command protobuf*
as the Fleet API, just over Bluetooth instead of the internet. BLE is free,
offline, low-latency, and you own the whole path. The only cost is you need a
Linux box with Bluetooth parked within ~10 m of the car.

## Architecture

```
your trigger (doorbell / button / cron / Home Assistant / ...)
      │  HTTP + bearer token, over the LAN
      ▼
tesla-ble-api  (this server, on a Linux box near the car)
      │  one persistent `tesla-control -ble` interactive session
      ▼  signed VehicleAction protobufs over Bluetooth LE
   your Tesla
```

## The one design decision that matters

**Keep a single persistent `tesla-control` session; do not spawn one per
command.** Here's why, because it cost real debugging to learn:

- Every time `tesla-control` starts, it opens the BLE adapter in Linux
  *user-channel* mode, which forces an HCI **down → up** transition on the
  controller.
- On several common onboard **Wi-Fi+BT combo chips** (e.g. Broadcom AP6275P /
  BCM43752 on Amlogic/Rockchip SBCs) that transition **wedges the controller**
  after a handful of cycles: `hciconfig hciX up` starts returning
  `Function not implemented (ENOSYS)` and the radio is dead until a **full
  reboot**. No `rfkill` dance, no re-attach, nothing short of a reboot brings
  it back.
- Running `tesla-control` once per command hits that down/up churn constantly
  and wedges within minutes.

Running the interactive shell (`tesla-control` with **no command argument**)
inits the adapter **once** and reuses the same connection for every verb. The
server (`server.py`) owns that subprocess, writes verbs to its stdin, and parses
output up to the `> ` prompt. If the transport dies it respawns the session.

### Two things make the persistent session work

1. **A patch to `tesla-control`.** Upstream only configures credentials when a
   command is on the command line, so the bare interactive shell demands an
   OAuth token and refuses to start (`could not load token: ... keyring`). The
   one-paragraph fix is in [`patches/`](patches/) — it tells `tesla-control` to
   use BLE + private-key + VIN credentials in interactive mode. **You must apply
   it and rebuild** `tesla-control`.

2. **Use a USB Bluetooth dongle, not the onboard combo chip, if yours wedges.**
   The persistent session avoids *most* down/up churn, but the car still drops
   the BLE link when it goes to sleep (~5–7 min idle), and the next command
   forces one reconnect — enough to wedge a bad combo chip. The clean fix: plug
   in **any cheap USB BT dongle** (a Broadcom BCM20702A0 / `0a5c:21ec` was
   tested). It enumerates as `hci1`, the `btusb` driver handles down/up cleanly,
   and the churn that wedged the onboard `hci0` at cycle #1 ran 20/20 with zero
   wedges. Set `TESLA_BT_ADAPTER=hci1` and forget about it.

If you genuinely can't add a dongle, two fallbacks exist (both inferior):
the **keep-awake** option (fire `wake` every 180 s so the car never sleeps so
the link never drops — see `TESLA_KEEPALIVE_*`), and the **wedge watchdog**
(reboots the box when it detects the ENOSYS signature — see `watchdog/`).

## Prerequisites

1. A Linux box with Bluetooth, within BLE range of the car, on your LAN.
2. Python 3 (standard library only — no pip packages).
3. [`tesla-control`](https://github.com/teslamotors/vehicle-command) built from
   source **with the patch in [`patches/`](patches/) applied**, and granted the
   BLE capabilities:
   ```bash
   git clone https://github.com/teslamotors/vehicle-command
   cd vehicle-command
   git apply /path/to/tesla-ble-api/patches/0001-*.patch
   go build -o ~/bin/tesla-control ./cmd/tesla-control
   # go-ble uses raw HCI sockets, so the binary needs caps (runs as a normal user):
   sudo setcap 'cap_net_admin,cap_net_raw+eip' ~/bin/tesla-control
   ```
4. An **enrolled key pair** on the car. Generate a key with `tesla-keygen`,
   then add it to the car (tap your phone/key card to the reader when prompted):
   ```bash
   mkdir -p ~/tesla && cd ~/tesla
   tesla-keygen -key-file private_key.pem create > public_key.pem
   tesla-control -ble -vin YOURVIN add-key-request public_key.pem owner cloud_key
   # then tap an already-paired phone or key card to the car's card reader
   ```
   This `private_key.pem` is **a spare key to your car** — guard it like one.
   (Full enrollment docs: the vehicle-command repo README.)

## Install

```bash
# 1. put the server somewhere, e.g. ~/tesla-ble-api
git clone https://github.com/lee-soft/tesla-ble-api ~/tesla-ble-api

# 2. create an API token
openssl rand -hex 24 > ~/tesla/api_token
chmod 600 ~/tesla/api_token

# 3. install the service (edit the .example first: your user, VIN, adapter)
cp ~/tesla-ble-api/systemd/tesla-ble-api.service.example \
   /etc/systemd/system/tesla-ble-api.service
sudoedit /etc/systemd/system/tesla-ble-api.service      # set User/VIN/adapter
sudo systemctl enable --now tesla-ble-api.service

# 4. check it
curl -s http://127.0.0.1:8710/health | python3 -m json.tool
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TESLA_VIN` | *(required)* | Your car's 17-char VIN. |
| `TESLA_BT_ADAPTER` | `hci0` | BLE adapter. Use `hci1` for a USB dongle (recommended). |
| `TESLA_DIR` | `~/tesla` | Dir holding `private_key.pem`, `cache.json`, `api_token`. |
| `TESLA_CONTROL_BIN` | `~/bin/tesla-control` | Path to the patched binary. |
| `TESLA_API_PORT` | `8710` | Listen port. |
| `TESLA_API_BIND` | `0.0.0.0` | Bind address (use `127.0.0.1` to keep it local). |
| `TESLA_API_TOKEN_FILE` | `$TESLA_DIR/api_token` | Bearer-token file. |
| `TESLA_KEY_FILE` / `TESLA_CACHE_FILE` | under `$TESLA_DIR` | Override key/cache paths. |
| `TESLA_KEEPALIVE_AUTOSTART_MIN` | `0` (off) | Minutes to keep the car awake on boot (combo-chip fallback). |
| `TESLA_KEEPALIVE_INTERVAL` | `180` | Seconds between keep-awake wakes. |

## API

**Auth:** every endpoint except `GET /health` and `GET /actions` needs
`Authorization: Bearer <token>` (or `?token=<token>`).

### Endpoints

| Method | Path | Does |
|---|---|---|
| GET  | `/health` | Status, session liveness, configured adapter/VIN, verb list. |
| GET  | `/actions` | Full verb + combo list with descriptions. |
| GET  | `/state` | Body-controller state (awake/asleep, closures). |
| POST | `/warm` | Wake the car + warm the session so the next command is instant. |
| POST | `/<action>` | Run a single verb (see below). |
| POST | `/scare`, `/treats` | Combos. Override order with `{"steps":[...]}`. |
| POST | `/lights-on` | Flash-pulse loop; body `{"seconds":N,"every":N}`. |
| POST | `/lights-off` | Stop the flash-pulse loop. |

### Verbs

- **Closures:** `trunk-open`, `trunk-close` (powered tailgates only),
  `frunk-open`, `charge-port-open`, `charge-port-close`,
  `windows-vent`, `windows-close`
- **Noise & light:** `honk`, `flash` / `flash-lights`
- **Climate / media:** `climate-on`, `climate-off`, `media-toggle`,
  `media-volume-up`, `media-volume-down`
- **Security / power:** `lock`, `unlock`, `sentry-on`, `sentry-off`,
  `wake`, `ping`

> There is **no steady-headlights-on** command on a Tesla — `lights-on`
> emulates it by flashing on an interval.

### Examples

```bash
TOK=$(cat ~/tesla/api_token); API=http://127.0.0.1:8710
curl -H "Authorization: Bearer $TOK"        $API/actions          # list verbs
curl -H "Authorization: Bearer $TOK"        $API/state            # awake? closures?
curl -H "Authorization: Bearer $TOK" -X POST $API/warm            # prep for a burst
curl -H "Authorization: Bearer $TOK" -X POST $API/trunk-open      # pop the trunk
curl -H "Authorization: Bearer $TOK" -X POST $API/scare           # flash + honk
curl -H "Authorization: Bearer $TOK" -X POST $API/lights-on \
     -d '{"seconds":120,"every":3}'                               # 2-min flicker
```

A standard-library Python client is in [`examples/client.py`](examples/client.py).

### Reading the result

```json
{ "ok": true, "reason": "ok", "steps": [ ... ] }
```
- `ok: true` → it worked. That's what to check.
- `reason: "may_have_succeeded"` → sent, but the car's ack didn't arrive in
  time (common on the first command after an idle gap). Usually still worked;
  safe to re-fire if you need certainty.
- `ok: false, reason: "transport"` → couldn't reach the car over BLE; retry in
  a few seconds.

### Sleep / wake behaviour

The car sleeps after a few idle minutes and infotainment commands fail on a
sleeping car. The server handles this: it tries the command **optimistically**
(instant when awake), and only on a sleep/transport failure does it wake the car
and retry once — so there's no wake penalty when the car is already up. Call
`POST /warm` when you *know* a burst is coming (e.g. on a doorbell trigger) to
move that one-off ~5 s wake cost to before it's needed.

## Security

- This is a **LAN tool with a bearer token**. It is not hardened for the public
  internet — don't port-forward it. Keep the box on a trusted network, or bind
  to `127.0.0.1` and reach it over a tunnel/VPN.
- `private_key.pem` is a working key to your car. It's `.gitignore`d here; keep
  it `chmod 600` and off version control.
- Only the whitelisted verbs in `ACTIONS`/`COMBOS` can ever run.

## Repo layout

```
server.py                         the API server (stdlib only)
patches/0001-*.patch              required tesla-control interactive-BLE fix
systemd/tesla-ble-api.service.example   the unit (edit + install)
systemd/tesla-wedge-watchdog.*    optional reboot-on-wedge safety net
watchdog/tesla-wedge-watchdog.sh  optional watchdog script
examples/client.py                tiny stdlib example client
```

## Credits

Stands entirely on [teslamotors/vehicle-command](https://github.com/teslamotors/vehicle-command).
This project is just a thin, opinionated HTTP front end plus the hard-won
hardware notes above. MIT licensed.
