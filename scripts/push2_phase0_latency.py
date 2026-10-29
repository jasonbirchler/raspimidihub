import argparse
import math
import select
import statistics
import sys
import time
from collections import defaultdict, deque
from contextlib import suppress
from pathlib import Path


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
        choices = ", ".join(
            f"{device.client_id}:{port.port_id} {device.name}/{port.name}"
            for device in devices
            for port in device.ports
            if port.is_input
        )
        if not choices:
            choices = "none"
        raise RuntimeError(
            f"Could not uniquely select Push 2 User port {port_id}; candidates: {choices}. "
            "Use --client-id with the client number from aconnect -l."
        )

    return candidates[0]


def event_key(event):
    note = event.data.note
    return note.channel, note.note, note.velocity


def pending_count(pending):
    return sum(len(timestamps) for timestamps in pending.values())


def run_test(args):
    AlsaSeq, MidiEventType = load_alsa_api()
    proxy = None
    tap = None
    user_input = None
    tap_input = None
    source = None
    subscribed = False
    pending = defaultdict(deque)
    latencies_ms = []
    input_count = 0
    forwarded_count = 0
    unmatched_count = 0
    send_failures = 0

    try:
        proxy = AlsaSeq("Push2 Phase0 Proxy")
        tap = AlsaSeq("Push2 Phase0 Tap", default_ports=False)
        source_device, source_port = resolve_source(proxy, args.client_id, args.user_port)
        source = (source_device.client_id, source_port.port_id)
        user_input = proxy.create_port("Push User input", writable=True)
        tap_input = tap.create_port("Latency sink", writable=True)

        proxy.subscribe(source[0], source[1], proxy.client_id, user_input)
        subscribed = True

        print(f"Push source: {source[0]}:{source[1]} {source_device.name}/{source_port.name}")
        print(f"Virtual output: {proxy.client_id}:{proxy._output_port} (Push2 Phase0 Proxy:output)")
        print(f"Duration: {args.duration:g}s. Press Push pads repeatedly during the test.")
        print("Mute or disconnect any existing Push-to-synth routes before testing.", flush=True)

        proxy_fd = proxy.fileno()
        tap_fd = tap.fileno()
        poller = select.poll()
        poller.register(proxy_fd, select.POLLIN)
        poller.register(tap_fd, select.POLLIN)

        stop_at = time.monotonic() + args.duration
        drain_until = stop_at + args.drain_seconds
        while True:
            now = time.monotonic()
            outstanding = pending_count(pending)
            if now >= stop_at and (outstanding == 0 or now >= drain_until):
                break

            active_remaining = stop_at - now
            drain_remaining = drain_until - now
            wait_ms = max(1, min(100, int(max(active_remaining, drain_remaining) * 1000)))
            for fd, _ in poller.poll(wait_ms):
                if fd == proxy_fd and time.monotonic() < stop_at:
                    while True:
                        event = proxy.read_event()
                        if event is None:
                            break
                        if event.type != MidiEventType.NOTEON or event.data.note.velocity == 0:
                            continue

                        key = event_key(event)
                        input_time = time.perf_counter_ns()
                        input_count += 1
                        if proxy.send_event(event, tap.client_id, tap_input):
                            pending[key].append(input_time)
                            forwarded_count += 1
                        else:
                            send_failures += 1

                if fd == tap_fd:
                    while True:
                        event = tap.read_event()
                        if event is None:
                            break
                        if event.type != MidiEventType.NOTEON or event.data.note.velocity == 0:
                            continue

                        key = event_key(event)
                        timestamps = pending[key]
                        if not timestamps:
                            unmatched_count += 1
                            continue
                        input_time = timestamps.popleft()
                        latencies_ms.append((time.perf_counter_ns() - input_time) / 1_000_000)

        return {
            "latencies_ms": latencies_ms,
            "input_count": input_count,
            "forwarded_count": forwarded_count,
            "unmatched_count": unmatched_count,
            "send_failures": send_failures,
            "unmatched_inputs": pending_count(pending),
            "proxy_overflows": proxy.fifo_overflows,
            "tap_overflows": tap.fifo_overflows,
        }
    finally:
        if subscribed and source is not None and user_input is not None:
            with suppress(OSError):
                proxy.unsubscribe(source[0], source[1], proxy.client_id, user_input)
        if proxy is not None:
            if user_input is not None:
                with suppress(OSError):
                    proxy.delete_port(user_input)
            proxy.close()
        if tap is not None:
            if tap_input is not None:
                with suppress(OSError):
                    tap.delete_port(tap_input)
            tap.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Measure Push 2 note-on proxy latency")
    parser.add_argument(
        "--client-id", type=int, help="Push ALSA client ID if auto-discovery is ambiguous"
    )
    parser.add_argument("--user-port", type=int, default=1)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--drain-seconds", type=float, default=1.0)
    parser.add_argument("--minimum-samples", type=int, default=30)
    args = parser.parse_args()

    if any(not math.isfinite(value) or value <= 0 for value in (args.duration, args.drain_seconds)):
        parser.error("duration and drain-seconds must be positive")
    if args.user_port < 0:
        parser.error("user-port must be non-negative")
    if args.minimum_samples < 1:
        parser.error("minimum-samples must be positive")
    return args


def report(result, minimum_samples):
    samples = result["latencies_ms"]
    print(f"Input note-ons: {result['input_count']}")
    print(f"Forwarded to virtual output: {result['forwarded_count']}")
    print(f"Matched output note-ons: {len(samples)}")
    print(
        f"Unmatched input/output events: {result['unmatched_inputs']}/{result['unmatched_count']}"
    )
    print(f"Send failures: {result['send_failures']}")
    print(f"ALSA FIFO overflows: proxy={result['proxy_overflows']}, tap={result['tap_overflows']}")

    if len(samples) < minimum_samples:
        print(f"INCONCLUSIVE: need at least {minimum_samples} matched note-ons")
        return 2

    ordered = sorted(samples)
    p95 = ordered[min(math.ceil(0.95 * len(ordered)) - 1, len(ordered) - 1)]
    median = statistics.median(ordered)
    print(f"Latency: median={median:.3f} ms, p95={p95:.3f} ms, max={ordered[-1]:.3f} ms")

    passed = (
        median < 2.0
        and result["send_failures"] == 0
        and result["unmatched_inputs"] == 0
        and result["unmatched_count"] == 0
        and result["proxy_overflows"] == 0
        and result["tap_overflows"] == 0
    )
    print("PASS: median latency is under 2 ms" if passed else "FAIL: proxy latency gate not met")
    return 0 if passed else 1


def main():
    args = parse_args()
    try:
        result = run_test(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"Latency test failed: {exc}", file=sys.stderr)
        return 1
    return report(result, args.minimum_samples)


if __name__ == "__main__":
    raise SystemExit(main())
