import argparse
import json
import math
import sys
import time
from contextlib import contextmanager, suppress
from urllib.request import urlopen

try:
    import numpy as np
    import usb.core
    import usb.util
except ImportError as exc:
    raise SystemExit(
        "Install the display-test dependencies: sudo apt install python3-usb python3-numpy"
    ) from exc

VENDOR_ID = 0x2982
PRODUCT_ID = 0x1967
INTERFACE = 0
ENDPOINT_OUT = 0x01
TRANSFER_TIMEOUT_MS = 1000
WIDTH = 960
HEIGHT = 160
FILLER_BYTES = 128
LINE_BYTES = WIDTH * 2 + FILLER_BYTES
FRAME_BYTES = HEIGHT * LINE_BYTES
TRANSFER_BYTES = 16 * 1024
FRAME_HEADER = bytes((0xFF, 0xCC, 0xAA, 0x88)) + bytes(12)
XOR_MASK = np.tile(
    np.frombuffer(bytes((0xE7, 0xF3, 0xE7, 0xFF)), dtype=np.uint8),
    FRAME_BYTES // 4,
)
BAR_COLORS = np.array(
    (
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 255),
        (255, 255, 0),
        (0, 255, 255),
        (255, 0, 255),
        (80, 80, 80),
    ),
    dtype=np.uint8,
)
BAR_COLUMNS = BAR_COLORS[(np.arange(WIDTH) * len(BAR_COLORS)) // WIDTH]
BASE_RGB = np.broadcast_to(BAR_COLUMNS[np.newaxis, :, :], (HEIGHT, WIDTH, 3)).copy()


def encode_frame(frame_index):
    rgb = BASE_RGB.copy()
    marker_width = 24
    marker_x = (frame_index * 18) % (WIDTH - marker_width + 1)
    rgb[:, marker_x : marker_x + marker_width] = (255, 255, 255)

    red = rgb[:, :, 0].astype(np.uint16)
    green = rgb[:, :, 1].astype(np.uint16)
    blue = rgb[:, :, 2].astype(np.uint16)
    pixels = ((blue & 0xF8) << 8) | ((green & 0xFC) << 3) | (red >> 3)

    lines = np.zeros((HEIGHT, WIDTH + FILLER_BYTES // 2), dtype=np.dtype("<u2"))
    lines[:, :WIDTH] = pixels
    payload = lines.view(np.uint8).reshape(-1)
    np.bitwise_xor(payload, XOR_MASK, out=payload)
    return payload.tobytes()


def find_push(serial_number):
    try:
        devices = list(usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=PRODUCT_ID) or [])
    except usb.core.NoBackendError as exc:
        raise RuntimeError("No libusb backend; install libusb-1.0-0") from exc

    matches = []
    for device in devices:
        serial = None
        if device.iSerialNumber:
            with suppress(usb.core.USBError, NotImplementedError):
                serial = usb.util.get_string(device, device.iSerialNumber)
        if serial_number is None or serial == serial_number:
            matches.append((device, serial))

    if not matches:
        suffix = f" with serial {serial_number!r}" if serial_number else ""
        raise RuntimeError(f"Push 2 USB device {VENDOR_ID:04x}:{PRODUCT_ID:04x}{suffix} not found")
    if len(matches) > 1:
        raise RuntimeError("Multiple Push 2 devices found; select one with --serial")
    return matches[0]


@contextmanager
def open_display(device):
    detached = False
    claimed = False
    try:
        configuration = device.get_active_configuration()
        if configuration is None:
            device.set_configuration()
            configuration = device.get_active_configuration()
        if configuration is None:
            raise RuntimeError("Push 2 has no active USB configuration")

        if device.is_kernel_driver_active(INTERFACE):
            device.detach_kernel_driver(INTERFACE)
            detached = True

        usb.util.claim_interface(device, INTERFACE)
        claimed = True
        interface = configuration[(INTERFACE, 0)]
        endpoint = usb.util.find_descriptor(interface, bEndpointAddress=ENDPOINT_OUT)
        if endpoint is None:
            raise RuntimeError(
                f"Bulk OUT endpoint 0x{ENDPOINT_OUT:02x} not found on interface {INTERFACE}"
            )
        if endpoint.bmAttributes & 0x03 != usb.util.ENDPOINT_TYPE_BULK:
            raise RuntimeError(f"Endpoint 0x{ENDPOINT_OUT:02x} is not a bulk endpoint")
        yield endpoint
    finally:
        if claimed:
            with suppress(usb.core.USBError, NotImplementedError):
                usb.util.release_interface(device, INTERFACE)
        if detached:
            with suppress(usb.core.USBError, NotImplementedError):
                device.attach_kernel_driver(INTERFACE)
        with suppress(usb.core.USBError, NotImplementedError):
            usb.util.dispose_resources(device)


def send_frame(endpoint, frame_index, timeout_ms):
    payload = encode_frame(frame_index)
    if len(payload) != FRAME_BYTES:
        raise RuntimeError(f"Encoded frame has {len(payload)} bytes, expected {FRAME_BYTES}")

    written = endpoint.write(FRAME_HEADER, timeout=timeout_ms)
    if written != len(FRAME_HEADER):
        raise RuntimeError(f"Short frame-header write: {written}/{len(FRAME_HEADER)} bytes")

    for offset in range(0, FRAME_BYTES, TRANSFER_BYTES):
        chunk = payload[offset : offset + TRANSFER_BYTES]
        written = endpoint.write(chunk, timeout=timeout_ms)
        if written != len(chunk):
            raise RuntimeError(
                f"Short frame-data write at byte {offset}: {written}/{len(chunk)} bytes"
            )


def stream(endpoint, duration, fps, label, frame_index, timeout_ms):
    interval = 1.0 / fps
    start = time.monotonic()
    stop_at = start + duration
    next_frame_at = start
    frame_times = []

    print(f"{label}: streaming for {duration:g}s at {fps:g} fps; watch the display", flush=True)
    while next_frame_at < stop_at:
        delay = next_frame_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        frame_started = time.monotonic()
        if frame_started >= stop_at:
            break

        send_frame(endpoint, frame_index, timeout_ms)
        frame_times.append(frame_started)
        frame_index += 1
        next_frame_at += interval

        now = time.monotonic()
        if next_frame_at <= now:
            missed = math.floor((now - next_frame_at) / interval) + 1
            next_frame_at += missed * interval

    if len(frame_times) > 1:
        measured_fps = (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
    else:
        measured_fps = 0.0
    print(f"{label}: sent {len(frame_times)} frames; measured {measured_fps:.2f} fps", flush=True)
    return frame_index, measured_fps


def fetch_json(url):
    with urlopen(url, timeout=3) as response:
        return json.load(response)


def capture_metrics(hub_url):
    base_url = hub_url.rstrip("/")
    try:
        observatory = fetch_json(f"{base_url}/api/observatory")
        stalls = fetch_json(f"{base_url}/api/debug/stalls")
    except (OSError, ValueError) as exc:
        print(f"Hub metrics unavailable: {exc}", file=sys.stderr)
        return None

    return {
        "fifo_overflows": observatory.get("alsa", {}).get("fifo_overflows"),
        "stall_episodes": stalls.get("episodes") or [],
    }


def report_metrics(before, after):
    if before is None or after is None:
        print("Hub FIFO/stall check: not measured")
        return False

    fifo_before = before["fifo_overflows"]
    fifo_after = after["fifo_overflows"]
    old_stalls = before["stall_episodes"]
    new_stalls = [episode for episode in after["stall_episodes"] if episode not in old_stalls]
    print(f"Hub FIFO overflows: {fifo_before} -> {fifo_after}")
    print(f"New hub stall episodes: {len(new_stalls)}")
    return (
        fifo_before is not None
        and fifo_after is not None
        and fifo_after <= fifo_before
        and not new_stalls
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Run the Push 2 Phase 0 display-stream test")
    parser.add_argument("--serial", help="USB serial number if more than one Push 2 is attached")
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--blackout-seconds", type=float, default=2.5)
    parser.add_argument("--recovery-seconds", type=float, default=5.0)
    parser.add_argument("--hub-url", default="http://127.0.0.1")
    parser.add_argument("--timeout-ms", type=int, default=TRANSFER_TIMEOUT_MS)
    args = parser.parse_args()

    numeric_values = (args.fps, args.duration, args.blackout_seconds, args.recovery_seconds)
    if any(not math.isfinite(value) or value <= 0 for value in numeric_values):
        parser.error("fps, duration, blackout-seconds, and recovery-seconds must be positive")
    if args.timeout_ms <= 0:
        parser.error("timeout-ms must be positive")
    return args


def main():
    args = parse_args()
    result = 0
    test_started = False
    before = None
    frame_index = 0

    try:
        device, serial = find_push(args.serial)
        print(f"Push 2 serial: {serial or '(unavailable)'}")
        print(
            f"Frame format: {WIDTH}x{HEIGHT}, {FRAME_BYTES} payload bytes, "
            f"{TRANSFER_BYTES}-byte transfers"
        )
        before = capture_metrics(args.hub_url)
        test_started = True

        with open_display(device) as endpoint:
            frame_index, measured_fps = stream(
                endpoint, args.duration, args.fps, "Initial run", frame_index, args.timeout_ms
            )
            if measured_fps < args.fps * 0.98:
                print(
                    f"WARNING: display stream fell below 98% of the {args.fps:g} fps target",
                    file=sys.stderr,
                )
                result = 1

            print(
                f"Pausing frames for {args.blackout_seconds:g}s; the screen should go black after about 2s",
                flush=True,
            )
            time.sleep(args.blackout_seconds)
            print("Resuming frames; the test pattern should return", flush=True)
            frame_index, recovery_fps = stream(
                endpoint,
                args.recovery_seconds,
                args.fps,
                "Recovery run",
                frame_index,
                args.timeout_ms,
            )
            if recovery_fps < args.fps * 0.98:
                print(
                    f"WARNING: recovery stream fell below 98% of the {args.fps:g} fps target",
                    file=sys.stderr,
                )
                result = 1
    except KeyboardInterrupt:
        print("Interrupted; releasing the Push 2 display interface", file=sys.stderr)
        result = 130
    except (RuntimeError, usb.core.USBError, OSError) as exc:
        print(f"Display test failed: {exc}", file=sys.stderr)
        result = 1

    if test_started:
        after = capture_metrics(args.hub_url)
        if not report_metrics(before, after):
            result = result or 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
