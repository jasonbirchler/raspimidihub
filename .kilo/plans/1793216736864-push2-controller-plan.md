# Plan: Ableton Push 2 as a dedicated RaspiMIDIHub controller

## Goal

A separate `raspimidihub-push2` service drives an Ableton Push 2 attached to a
Pi 4B / Pi 5 running RaspiMIDIHub. It must:

1. Keep Push controls and their mapped hub UI controls in two-way sync
   (press on Push toggles the hub control, change in the hub updates the Push LED/display).
2. Let the user create, edit, and delete Push-to-UI mappings from either the Push or the web UI.
3. Provide a dedicated Push UI for the device matrix: add, remove, and edit connections.

## Decisions (resolved in planning)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Separate systemd service `raspimidihub-push2` (Python, same deb). It talks to the hub only via the public REST + SSE API on `127.0.0.1:80`. | Blocking USB display transfers (~327 KB/frame) and Push MIDI must not share the asyncio MIDI loop or GIL. A USB stall must not affect routing. |
| D2 | Mappings persist in a new `push2` block in `config.json`, edited via REST. | Follows the Save / Load / Export / backup tiers. Survives hard power cuts. Single source of truth for web and Push. |
| D3 | **Proxy signal path.** The service owns the physical Push User port and exposes one virtual device, `Ableton Push 2`, to the matrix. Every control is either *consumed* (mapped or reserved; never forwarded) or *forwarded* (unmapped, or mapped with `forward: true`). | Push controls send on fixed channels (spec: pads `0x90`, buttons `0xB0`, all channel 1), so per-control isolation is impossible with ALSA multicast or matrix filters. The matrix cannot block one CC or note. |
| D4 | **Modal matrix edit.** The `Select` button toggles matrix mode. In matrix mode every Push control is consumed (nothing forwarded), so pads never reach synths. Pads form an 8x8 grid of matrix cells. | Matches the requested UX and removes note blips into synths during setup. |
| D5 | Physical Push ports (Live and User) are **hidden** from the matrix and from all-to-all / default auto-connect. Only the virtual device is routable. | The service is the only legitimate consumer of the physical device. |
| D6 | The Push stays in **User MIDI mode** (SysEx `0x0A 0x01`) at all times. The service reads and writes only the User port (port 2). | Keeps one active port. Matrix mode is a service-side state, not a Push MIDI mode. |
| D7 | Display: direct USB bulk via libusb (`pyusb`, VID `0x2982` PID `0x1967`, interface 0, bulk OUT `0x01`). 16-byte header `FF CC AA 88 00..`, then 160 lines of 2048 bytes (960 px BGR565 + 128 filler). Each byte XORed with `E7 F3 E7 FF` repeating. Resend a frame at least every 1.5 s, because the display goes black after 2 s without one. Cap redraws at about 15 fps. | Per Ableton's interface manual and the open-source push2-python protocol. |
| D8 | Pi support: **Pi 4B and Pi 5 only.** Pi 3B+ and Zero 2 W are out of scope for v1. | Shared USB 2.0 bus on the 3B+ with the display stream risks stuck notes (documented in chapter 2). |
| D9 | Virtual device stable id: `push2-<usb serial>`. The device scan must admit the virtual client explicitly. | `device_id.py` only admits card-less user clients that declare a UMP endpoint (`ump_virtual`). Without an opt-in the virtual device is invisible. |
| D10 | Virtual device exists only while the physical Push is present. On unplug it is removed and its connections fall back to saved-offline (existing behaviour). | Avoids a phantom device in the matrix. |
| D11 | Virtual device is excluded from `default_routing: "all"` auto-connect. | Otherwise every Push pad would auto-route to every synth on plug-in. |
| D12 | MIDI 1.0 legacy event API on all of the service's ALSA clients. | Legacy-shaped events avoid kernel UMP conversion surprises. AGENTS rule: never assume UMP exists. |
| D13 | Dependencies: `python3-usb`, `python3-numpy`, `python3-pil` in `debian/control` **Recommends** (not Depends). Other code uses stdlib only (`http.client`, `json`, `threading`). | Matches the existing `python3-dbus-next` pattern. The hub still installs without them. |
| D14 | The service writes nothing to disk. No remount-rw window needed. | Read-only root FS. Mappings live in config via the hub API. |

### Defaults to confirm at review (not blocking)

- **Matrix edit scope on Push:** pad grid connect / disconnect, plus per-connection channel mask and message-type filter via encoders on the display. MIDI mappings (transforms) are web-only in v1.
- **Mapping targets v1:** any plugin param (`Button`, `Radio`, `Knob`, `Fader`, `Wheel`, `Display` read-only), controller drop buttons (`fire` / `capture`), and two global actions (`panic`, `save`). Matrix-level actions are not mappable.
- **Learn flow, both sides:** web arms a learn on a target, Push completes it by pressing a control. Push arms learn with its Learn soft key and browses targets on the display. Both paths use one hub-side transient learn state.
- **Encoders:** relative, one unit per detent for integer params, scaled to the param range for fader/knob params, with simple acceleration from the encoder delta.
- **Mapping cascade:** deleting a plugin instance removes its mappings. The Push and web UI show a missing-target warning otherwise.
- **Push LEDs:** buttons show on/off (or the Radio's palette index). Drop buttons show `captured` (white), `scheduled` (amber, blink animation via LED channel 1..15), and `firing` (flash). Pads show matrix state in matrix mode and are off in play mode unless mapped.
- **MIDI Start on boot** is sent once on the User port so blink/pulse LED animations run (spec: animations are timed by MIDI start/clock on port 2 in User mode).

## Architecture

```
Push 2 (USB)
 ├─ User MIDI port (port 2)  ◄── service owns it (hidden from matrix)
 │      │  consume (mapped / reserved / matrix-mode)  ─► hub REST PATCH /api/plugins/instances/<id>
 │      │  forward (unmapped)                          ─► virtual port "Ableton Push 2" ─► matrix ─► synths
 │      ◄── LED / MIDI Start / SysEx from service state
 │      ◄── matrix-routed messages to virtual input (forwarded to Push unchanged)
 └─ Display (USB bulk, interface 0) ◄── service renderer (Pillow + numpy → BGR565 → XOR → 16 KB transfers)

raspimidihub-push2 (new process)
 ├─ usb_device    discovery / hotplug poll on VID:PID
 ├─ midi_io       ALSA seq: physical port reader/writer + virtual device port (legacy events)
 ├─ proxy         consume-vs-forward decision, matrix-mode gating
 ├─ mapping       control → target; learn state machine; coalesced PATCH (~30 Hz)
 ├─ hub_client    stdlib http.client REST + SSE (/api/events, /api/sse/subscribe), reconnect + resubscribe
 ├─ matrix_view   pad grid, paging, connection / filter edits via /api/connections
 ├─ ui            display screens: home, map browser, matrix
 └─ leds          shadow LED state, diff-only writes

RaspiMIDIHub (existing)
 └─ new: config.push2, api/push2.py routes, SSE events, device hiding + virtual opt-in + auto-connect exclusion
```

Threading: the MIDI proxy runs on its own thread with no blocking calls. Display rendering and USB writes run on a separate thread. HTTP runs on a third thread. Bounded queues connect them. If the display falls behind, frames are dropped, never MIDI events.

## Hub-side API and config contract (new)

Config (`config.py` `DEFAULT_CONFIG`):
```json
"push2": {
  "mappings": [
    {"id": "m1",
     "control": "button:CC102",
     "target": {"type": "param", "instance": "controller_mixer_8-1", "param": "bg"},
     "forward": false,
     "label": ""}
  ]
}
```
Target types: `param` (instance, param), `drop` (instance, button 0..3, action `fire`|`capture`), `action` (`panic`|`save`).
Control ids come from the spec's MIDI mapping figure: `pad:<note>`, `button:CC<n>`, `encoder:CC<n>`, `encoder_touch:<note>`, `touchstrip`, `select` (reserved).

REST (new, `src/raspimidihub/api/push2.py`, registered in `api/__init__.py`; every route gets a `summary=`):
- `GET /api/push2` — status (connected, serial, mode, matrix_mode), mappings, control inventory, learn state.
- `POST /api/push2/mappings` — add. `PATCH /api/push2/mappings/<id>` — edit (`forward`, `label`, `target`). `DELETE /api/push2/mappings/<id>`. Each marks config dirty (not transient).
- `POST /api/push2/learn/start` `{target}` and `POST /api/push2/learn/cancel` — transient learn state, emits `push2-learn`.
- `POST /api/push2/learn/result` `{control}` — service-reported press that completes an armed learn. Creates the mapping.
- `POST /api/push2/status` — service reports connected / serial / mode / matrix_mode. Emits `push2-status`.

SSE vocabulary (add to `SSE_EVENTS` in `web.py`): `push2-status`, `push2-mappings`, `push2-learn`.

Hub-side device handling:
- Hide physical Push ports (VID `0x2982` PID `0x1967`) from the matrix device list in `api/devices.py`.
- Exclude the virtual device from `midi_engine.py` default routing (`default_routing == "all"`, lines around 830 and 903).
- Admit the virtual client in `device_id.py` (opt-in by client name prefix `Ableton Push 2`, stable id `push2-<serial>`).

Existing endpoints reused by the service (no change): `GET /api/devices`, `GET /api/connections`, `POST /api/connections`, `DELETE /api/connections/<id>`, `PATCH /api/connections/<id>`, `PATCH /api/plugins/instances/<id>` (`params`), `POST /api/panic`, `POST /api/config/save`, `GET /api/events` + `POST /api/sse/subscribe` (`instances` for per-instance `plugin-param`).

Resync triggers: `connection-changed` with `action: config-loaded` (emitted by Load, Restore, Import) re-reads everything. `plugin-changed`, `device-connected`, `device-disconnected`, and `connection-changed` refresh the matrix view. The service re-subscribes to `plugin-param` for the current mapped instance set whenever mappings change.

## Ordered tasks

### Phase 0: Hardware spike (gate, not shipped)
Throwaway script on a Pi 4B with a real Push 2. Pass criteria:
1. Port names and numbers on Linux (`Ableton Push 2 NN:0` Live, `NN:1` User). Identify the User port reliably across reboots.
2. SysEx `F0 00 21 1D 01 01 0A 01 F7` switches to User mode. Pads and buttons arrive on port 2 only.
3. Display: claim interface 0 with snd-usb-audio bound (or detach it). Sustained 15 fps for 60 s with no `/api/observatory` `fifo_overflows` increase and no `/api/debug/stalls` entries. Blackout occurs after 2 s without a frame, and recovers.
4. LED control: RGB via note-on palette index on channel 0; blink via channel 11..15 after a MIDI Start on port 2.
5. Proxy latency: note-on in → out through the virtual port adds under 2 ms median.
6. Control inventory: pad notes 36..99, encoder CC 71..78 (touch on notes), button CCs per the spec figure, Select CC identified.
7. Push power draw with all LEDs on is within a Pi 4 USB port budget (spec: under 500 mA).

Gate: if (3) or (5) fails, stop and re-plan. Do not proceed to Phase 1.

### Phase 1: Service skeleton and proxy (no UI)
1. `src/raspimidihub/push2/` package with `__main__.py` (`python3 -m raspimidihub.push2`) and modules from the architecture.
2. `midi_io.py` on the existing `alsa_seq.AlsaSeq` (`create_port`, `subscribe`). Physical User port reader/writer plus the virtual port.
3. `proxy.py`: consume / forward decision. Reserved `select` always consumed. Matrix-mode gating. Unit-testable pure function.
4. `systemd/raspimidihub-push2.service` (`User=root`, `Restart=always`, no `WatchdogSec` unless wired to `sd_notify`). Add to `debian/raspimidihub.install`.
5. Hub changes: hide physical ports, admit virtual client, exclude from auto-connect (D5, D9, D11).

### Phase 2: Hub config, API, and events
1. `config.py`: `push2` defaults. Confirm `_deep_merge` handles old configs.
2. `api/push2.py` with summaries; SSE names in `SSE_EVENTS`.
3. Learn state (transient, not persisted) and `POST /api/push2/learn/result`.
4. Mapping validation: target must exist, control id must be known. Cascade removal on plugin delete (hook in the plugin-removal path).

### Phase 3: Two-way mapping engine
1. Mapping store and lookup by control id. Push press → `PATCH /api/plugins/instances/<id>` with the same value semantics as the web UI (Button toggles, Knob/Fader sets, drops fire/capture).
2. SSE `plugin-param` / `plugin-display` → LED and display state. The service never re-PATCHes values that originated from SSE (loop guard).
3. Encoder coalescing at about 30 Hz. Drop buttons read `drop_states` and drive LED animations.
4. Persistence checks: a press marks the config dirty like the same web action. Performing a quiet drop fire or cancel does not dirty (existing `transient_params` rule, respected by using the same PATCH path).

### Phase 4: Push display UI (renderer + screens)
1. `display.py`: Pillow render → BGR565 → numpy XOR mask → 16-byte header plus 160 x 2048-byte lines. Golden unit tests for header, stride, filler, and XOR pattern.
2. `ui.py` screens: Home (mapped controls, values, status, `Matrix` and `Map` soft keys), Map browser (instance → param, drop, action; encoders scroll, encoder press selects, `Forward` toggle), Matrix (see Phase 5).
3. Heartbeat resend at most every 1.5 s. Redraw only on change, capped at 15 fps.

### Phase 5: Matrix edit mode on Push
1. `Select` toggles matrix mode (service-side). Entering it sets matrix_mode and consumes all controls. Leaving it restores play mode.
2. Pad grid: 8 columns = destination ports, 8 rows = source ports, paged with the arrow buttons. LED colour: green connected, amber filtered, dim unconnected, red for the cursor. Press toggles via `POST` / `DELETE /api/connections`.
3. Encoder 1 moves the cursor on the display. Encoder press opens a connection editor (channel mask and message types) via `PATCH /api/connections/<id>`. Soft key unmap.
4. Self-connections and the virtual device's own ports are hidden from the grid.
5. Verify zero forwarded notes in matrix mode (count forwarded events on the virtual port).

### Phase 6: Web UI
1. Settings → new "Push 2" sub-page (`static/pages/settings.js`, new panel in `static/panels/`, styles in `style.css`). Status badge, mapping table (control, target, forward toggle, label, delete), Add with target picker, and Learn ("press a control on the Push").
2. SSE subscription for `push2-status`, `push2-mappings`, `push2-learn` in `ui/sse-subscriptions.js`.
3. Optional in v1: long-press cell binding popup "Map to Push control". Out of scope unless requested.

### Phase 7: Packaging, docs, tests
1. `debian/control` Recommends: `python3-usb`, `python3-numpy`, `python3-pil`. `debian/raspimidihub.install` adds the new unit.
2. Manual (mandatory per AGENTS.md):
   - New chapter `19-push-2-controller.md` (or add to chapter 8/9 if the reviewer prefers). Add it to `docs/manual/README.md` and `metadata.yaml`.
   - `02-hardware-and-connectors.md`: Push 2 supported on Pi 4B / Pi 5 only, USB bus and power notes.
   - `05-routing-matrix.md`: physical Push ports hidden, virtual device, matrix mode on Push.
   - `11-saving-and-exporting-configs.md`: `push2` key is saved, exported, and backed up.
   - `12-settings.md`: new Push 2 page.
   - `17-technical-reference.md`: `push2` config schema and the service architecture.
   - `E-appendix-rest-and-sse-api.md`: no hand-copied routes (registry is generated). Verify `/docs` shows the new routes and SSE events.
   - `D-appendix-keyboard-shortcuts.md`: not applicable (no keyboard shortcut). `03-interacting-with-the-web-ui.md`: no new nav tab in v1.
   - `Screenshots needed` entries for the Push photo and display shots (hardware-only, not regenerable by `make screenshots`).
   - Bump `docs/manual/README.md` "Documented release" only at release time.
3. `CHANGELOG.txt` one-liner per release.
4. Tests under `tests/` (`pytest -m "not alsa and not e2e"`):
   - display encoding golden tests (header, line stride, filler, XOR);
   - control decode table and proxy decisions (consume / forward / matrix-mode);
   - encoder accumulation and coalescing; LED shadow diff;
   - learn state machine; mapping validation and cascade delete;
   - config defaults and deep-merge with an old config;
   - API route tests for the new `/api/push2` routes;
   - device hiding and auto-connect exclusion for VID `0x2982`.
   Run `make test` and `make lint` (`ruff check src plugins`) and `make fmt-check`.

### Phase 8: Hardware acceptance (Pi 4B and Pi 5)
- R1 two-way: Push button toggles the hub button within 200 ms. Web toggle lights the Push LED within 200 ms. Count PATCH per press (exactly 1, no echo).
- R2 mapping from both sides: create a mapping on the Push and one on the web, verify each. Save, reboot, and verify persistence. Load a backup that lacks the mapping and verify the Push resyncs.
- R3 matrix: Select enters and leaves mode. Pads add and remove connections, reflected in the web matrix within 1 s. Filter edits persist. No pad notes forwarded in matrix mode.
- Failure modes: unplug and replug the Push (virtual device disappears and returns, connections restored). Restart `raspimidihub` while in matrix mode. Kill `raspimidihub-push2` (systemd restarts it, no stuck notes). Delete a mapped plugin instance (mapping removed). Run `/api/debug/stalls` and `/api/observatory` for FIFO overflows during a 10 min soak with display and MIDI traffic.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| snd-usb-audio binds the display interface, blocking libusb claim. | Phase 0 check. If bound, detach kernel driver for that interface only, and document. |
| Proxy adds latency or jitter on pads. | Phase 0 gate (under 2 ms). Display on a separate thread. Pass-through path has no blocking calls. |
| Display USB traffic starves MIDI on a shared bus. | Pi 4/5 only (D8). 15 fps cap. Frames dropped, never MIDI. Measure in soak. |
| Push firmware differs from spec rev 1.1 (2017). | Read identity reply in Phase 0. Gate features (for example RGB palette writes) on firmware version. |
| Hub device-scan change affects other devices. | Opt-in only for the `Ableton Push 2` name and the virtual client. Golden tests for other devices. |
| Config schema growth. | Single `push2` block, versionless (defaults deep-merged). |
| Service crash leaves Push LEDs stuck. | On start, clear the LED shadow with a full LED write. On clean exit, all off and MIDI Stop. |
| Hub API unavailable at boot. | Retry with backoff. Service keeps the proxy running locally so unmapped pads still reach synths after the hub is back. |

## Out of scope for v1

- Pi 3B+ / Zero 2 W support.
- Multiple Push devices.
- Matrix MIDI-transform (mapping) editing on the Push.
- Long-press "Map to Push" popup on web controller cells.
- Live / Ableton Push control-surface mode (the service does not emulate Live).

## Open items to confirm during implementation

- Exact control inventory (note and CC numbers): read from the spec's MIDI mapping figure, verified in Phase 0.
- Whether `Select` emits a CC or note, and the exact channel.
- Whether plugin instance ids survive a restart (`{type}-{n}` in `host.py`). Mappings reference them, so verify with a restore test.
- SSE `plugin-param` payload field names (verify in `web.py` / plugin-host notify path before writing the client).
