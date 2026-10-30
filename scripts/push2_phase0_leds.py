import argparse
import math
import sys
import time
from pathlib import Path


def load_alsa_api():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from raspimidihub.alsa_seq import AlsaSeq, MidiEventType, SndSeqEvent

    return AlsaSeq, MidiEventType, SndSeqEvent


def resolve_destination(seq, client_id, port_id):
    if client_id is None:
        devices = [
            device for device in seq.scan_devices() if "ableton push 2" in device.name.lower()
        ]
    else:
        device = seq.scan_one_client(client_id)
        devices = [device] if device is not None else []

    candidates = []
    for device in devices:
        port = next(
            (port for port in device.ports if port.port_id == port_id and port.is_output),
            None,
        )
        if port is not None:
            candidates.append((device, port))

    if len(candidates) != 1:
        choices = (
            ", ".join(
                f"{device.client_id}:{port.port_id} {device.name}/{port.name}"
                for device in devices
                for port in device.ports
                if port.is_output
            )
            or "none"
        )
        raise RuntimeError(
            f"Could not uniquely select Push 2 User output port {port_id}; candidates: {choices}. "
            "Use --client-id with the client number from aconnect -l."
        )

    return candidates[0]


def send_note(seq, event_type, client_id, port_id, channel, note, velocity, SndSeqEvent):
    event = SndSeqEvent()
    event.type = event_type
    event.data.note.channel = channel
    event.data.note.note = note
    event.data.note.velocity = velocity
    if not seq.send_event(event, client_id, port_id):
        raise RuntimeError(f"Failed to send note {note} to {client_id}:{port_id}")


def send_cc(seq, client_id, port_id, channel, cc, value, MidiEventType, SndSeqEvent):
    event = SndSeqEvent()
    event.type = MidiEventType.CONTROLLER
    event.data.control.channel = channel
    event.data.control.param = cc
    event.data.control.value = value
    if not seq.send_event(event, client_id, port_id):
        raise RuntimeError(f"Failed to send CC {cc} to {client_id}:{port_id}")


def send_transport(seq, client_id, port_id, event_type, SndSeqEvent):
    event = SndSeqEvent()
    event.type = event_type
    if not seq.send_event(event, client_id, port_id):
        raise RuntimeError(f"Failed to send transport event to {client_id}:{port_id}")


def send_clock(seq, client_id, port_id, MidiEventType, SndSeqEvent, seconds, bpm):
    interval = 60.0 / (bpm * 24.0)
    ticks = math.ceil(seconds / interval)
    next_tick = time.monotonic()
    for _ in range(ticks):
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        send_transport(seq, client_id, port_id, MidiEventType.CLOCK, SndSeqEvent)
        next_tick += interval


def run_visual_test(seq, destination, args, MidiEventType, SndSeqEvent):
    client_id, port_id = destination
    colors = (("blue", 125), ("green", 126), ("red", 127))
    print("Watch pad 36 (bottom-left): it should change blue, green, then red.")
    for color, index in colors:
        print(f"Pad 36: {color} (palette index {index})", flush=True)
        send_note(seq, MidiEventType.NOTEON, client_id, port_id, 0, 36, index, SndSeqEvent)
        time.sleep(args.step_seconds)

    print(
        "Pad 36 should now blink green/red; animation channel 14, MIDI clock at 120 BPM.",
        flush=True,
    )
    try:
        send_transport(seq, client_id, port_id, MidiEventType.START, SndSeqEvent)
        send_note(seq, MidiEventType.NOTEON, client_id, port_id, 0, 36, 126, SndSeqEvent)
        send_note(seq, MidiEventType.NOTEON, client_id, port_id, 14, 36, 127, SndSeqEvent)
        send_clock(
            seq,
            client_id,
            port_id,
            MidiEventType,
            SndSeqEvent,
            args.blink_seconds,
            120.0,
        )
    finally:
        send_note(seq, MidiEventType.NOTEON, client_id, port_id, 0, 36, 126, SndSeqEvent)
        send_transport(seq, client_id, port_id, MidiEventType.STOP, SndSeqEvent)

    print("RGB and blink messages sent; pad 36 is left green.")


def set_all_leds(seq, destination, value, MidiEventType, SndSeqEvent):
    client_id, port_id = destination
    for note in range(36, 100):
        send_note(seq, MidiEventType.NOTEON, client_id, port_id, 0, note, value, SndSeqEvent)
    for cc in LED_CC_NUMBERS:
        send_cc(seq, client_id, port_id, 0, cc, value, MidiEventType, SndSeqEvent)


def run_power_setup(seq, destination, args, MidiEventType, SndSeqEvent):
    print(
        f"Lighting 64 pads and {len(LED_CC_NUMBERS)} mapped CC LEDs at value 127 "
        f"for {args.power_hold_seconds:g}s. Measure USB current with an inline meter.",
        flush=True,
    )
    set_all_leds(seq, destination, 127, MidiEventType, SndSeqEvent)
    time.sleep(args.power_hold_seconds)


LED_CC_NUMBERS = (
    3,
    9,
    14,
    15,
    *range(20, 32),
    *range(35, 64),
    *range(71, 80),
    *range(85, 91),
    *range(102, 114),
    *range(116, 120),
)


def parse_args():
    parser = argparse.ArgumentParser(description="Run Push 2 Phase 0 LED and power-load checks")
    parser.add_argument(
        "--client-id", type=int, help="Push ALSA client ID if auto-discovery is ambiguous"
    )
    parser.add_argument("--user-port", type=int, default=1)
    parser.add_argument(
        "--all-on", action="store_true", help="Light all mapped LEDs for a current-meter reading"
    )
    parser.add_argument("--step-seconds", type=float, default=1.0)
    parser.add_argument("--blink-seconds", type=float, default=3.0)
    parser.add_argument("--power-hold-seconds", type=float, default=30.0)
    args = parser.parse_args()

    durations = (args.step_seconds, args.blink_seconds, args.power_hold_seconds)
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        parser.error("step-seconds, blink-seconds, and power-hold-seconds must be positive")
    if args.user_port < 0:
        parser.error("user-port must be non-negative")
    return args


def main():
    args = parse_args()
    AlsaSeq, MidiEventType, SndSeqEvent = load_alsa_api()
    seq = None
    all_on_attempted = False
    destination = None

    try:
        seq = AlsaSeq("Push2 Phase0 LEDs")
        device, port = resolve_destination(seq, args.client_id, args.user_port)
        destination = (device.client_id, port.port_id)
        print(f"Push destination: {device.client_id}:{port.port_id} {device.name}/{port.name}")

        if args.all_on:
            all_on_attempted = True
            run_power_setup(seq, destination, args, MidiEventType, SndSeqEvent)
            print("Power hold complete; clearing LEDs.")
        else:
            run_visual_test(seq, destination, args, MidiEventType, SndSeqEvent)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"LED test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if all_on_attempted and seq is not None and destination is not None:
            try:
                set_all_leds(seq, destination, 0, MidiEventType, SndSeqEvent)
            except (OSError, RuntimeError):
                pass
        if seq is not None:
            seq.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
