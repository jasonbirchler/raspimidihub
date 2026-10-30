import argparse
import math
import select
import sys
import time
from collections import Counter
from pathlib import Path

EXPECTED_PAD_NOTES = set(range(36, 100))
EXPECTED_TOUCH_NOTES = set(range(0, 11))
EXPECTED_ENCODER_CCS = set(range(71, 79))
EXPECTED_BUTTON_CCS = (
    set(range(20, 32))
    | set(range(35, 64))
    | set(range(85, 91))
    | set(range(102, 114))
    | set(range(116, 120))
    | {3, 9}
)


def load_alsa_api():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from raspimidihub.alsa_seq import AlsaSeq, MidiEventType

    return AlsaSeq, MidiEventType


def resolve_source(seq, client_id, port_id):
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
            (port for port in device.ports if port.port_id == port_id and port.is_input),
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
                if port.is_input
            )
            or "none"
        )
        raise RuntimeError(
            f"Could not uniquely select Push 2 User input port {port_id}; candidates: {choices}. "
            "Use --client-id with the client number from aconnect -l."
        )

    return candidates[0]


def format_number_set(numbers):
    return ", ".join(str(number) for number in sorted(numbers)) or "none"


def print_summary(notes_on, notes_off, cc_counts, pitchbend_values, overflow_count):
    pad_notes = {note for _, note in notes_on if note in EXPECTED_PAD_NOTES}
    touch_notes = {note for _, note in notes_on | notes_off if note not in EXPECTED_PAD_NOTES}
    encoder_ccs = {cc for _, cc in cc_counts if cc in EXPECTED_ENCODER_CCS}
    button_ccs = {cc for _, cc in cc_counts if cc in EXPECTED_BUTTON_CCS}

    print(f"Pad notes observed: {len(pad_notes)}/{len(EXPECTED_PAD_NOTES)}")
    print(f"Missing pad notes: {format_number_set(EXPECTED_PAD_NOTES - pad_notes)}")
    print(f"Encoder CCs observed: {format_number_set(encoder_ccs)}")
    print(f"Missing encoder CCs: {format_number_set(EXPECTED_ENCODER_CCS - encoder_ccs)}")
    print(f"Encoder touch/candidate notes: {format_number_set(touch_notes)}")
    print(
        f"Missing expected touch notes 0-10: {format_number_set(EXPECTED_TOUCH_NOTES - touch_notes)}"
    )
    print(f"Button CCs observed: {format_number_set(button_ccs)}")
    print(f"Missing mapped button CCs: {format_number_set(EXPECTED_BUTTON_CCS - button_ccs)}")
    print(f"Select CC48 observed: {'yes' if any(cc == 48 for _, cc in cc_counts) else 'no'}")
    print(
        f"All CCs by channel: {format_number_set(f'ch{channel + 1}:cc{cc}' for channel, cc in cc_counts)}"
    )
    if pitchbend_values:
        print(
            f"Pitch bend samples: {len(pitchbend_values)}, "
            f"range={min(pitchbend_values)}..{max(pitchbend_values)}"
        )
    else:
        print("Pitch bend samples: none")
    print(f"ALSA input FIFO overflows: {overflow_count}")


def capture(args):
    AlsaSeq, MidiEventType = load_alsa_api()
    seq = AlsaSeq("Push2 Phase0 Inventory")
    input_port = None
    source = None
    subscribed = False
    notes_on = set()
    notes_off = set()
    cc_counts = Counter()
    pitchbend_values = []
    event_counts = Counter()

    try:
        device, port = resolve_source(seq, args.client_id, args.user_port)
        source = (device.client_id, port.port_id)
        input_port = seq.create_port("Push User capture", writable=True)
        seq.subscribe(source[0], source[1], seq.client_id, input_port)
        subscribed = True

        print(f"Push source: {source[0]}:{source[1]} {device.name}/{port.name}")
        print(f"Capture window: {args.duration:g}s")
        print(
            "Press each pad and button once; touch/turn each encoder. Ctrl-C ends early.",
            flush=True,
        )

        poller = select.poll()
        poller.register(seq.fileno(), select.POLLIN)
        stop_at = time.monotonic() + args.duration
        try:
            while time.monotonic() < stop_at:
                remaining = stop_at - time.monotonic()
                poller.poll(max(1, min(100, int(remaining * 1000))))
                while True:
                    event = seq.read_event()
                    if event is None:
                        break

                    if event.type == MidiEventType.NOTEON:
                        channel = event.data.note.channel
                        note = event.data.note.note
                        velocity = event.data.note.velocity
                        if velocity:
                            notes_on.add((channel, note))
                            event_counts["note_on"] += 1
                            kind = "NOTE_ON"
                        else:
                            notes_off.add((channel, note))
                            event_counts["note_off"] += 1
                            kind = "NOTE_OFF"
                        if args.verbose:
                            print(f"{kind} ch={channel + 1} note={note} velocity={velocity}")
                    elif event.type == MidiEventType.NOTEOFF:
                        channel = event.data.note.channel
                        note = event.data.note.note
                        notes_off.add((channel, note))
                        event_counts["note_off"] += 1
                        if args.verbose:
                            print(f"NOTE_OFF ch={channel + 1} note={note}")
                    elif event.type == MidiEventType.CONTROLLER:
                        channel = event.data.control.channel
                        cc = event.data.control.param
                        value = event.data.control.value
                        cc_counts[(channel, cc)] += 1
                        event_counts["cc"] += 1
                        if args.verbose:
                            print(f"CC ch={channel + 1} cc={cc} value={value}")
                    elif event.type == MidiEventType.PITCHBEND:
                        pitchbend_values.append(event.data.control.value)
                        event_counts["pitchbend"] += 1
                        if args.verbose:
                            print(f"PITCHBEND value={event.data.control.value}")
        except KeyboardInterrupt:
            print("Capture ended by user.")

        print(
            f"Captured {event_counts['note_on']} note-ons, {event_counts['note_off']} note-offs, "
            f"{event_counts['cc']} CCs, and {event_counts['pitchbend']} pitch bends."
        )
        print_summary(notes_on, notes_off, cc_counts, pitchbend_values, seq.fifo_overflows)
        return 0 if event_counts else 1
    finally:
        if subscribed and source is not None and input_port is not None:
            try:
                seq.unsubscribe(source[0], source[1], seq.client_id, input_port)
            except OSError:
                pass
        if input_port is not None:
            try:
                seq.delete_port(input_port)
            except OSError:
                pass
        seq.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Inventory Push 2 User-mode MIDI controls")
    parser.add_argument(
        "--client-id", type=int, help="Push ALSA client ID if auto-discovery is ambiguous"
    )
    parser.add_argument("--user-port", type=int, default=1)
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument(
        "--verbose", action="store_true", help="Print every note, CC, and pitch-bend event"
    )
    args = parser.parse_args()

    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("duration must be positive")
    if args.user_port < 0:
        parser.error("user-port must be non-negative")
    return args


def main():
    args = parse_args()
    try:
        return capture(args)
    except (OSError, RuntimeError) as exc:
        print(f"Inventory test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
