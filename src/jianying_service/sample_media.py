from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


def write_wav(path: Path, seconds: float = 3.0, sample_rate: int = 16_000) -> None:
    if seconds <= 0 or sample_rate <= 0:
        raise ValueError("seconds and sample_rate must be positive")
    frames = int(seconds * sample_rate)
    data = bytearray()
    for index in range(frames):
        # A quiet 440 Hz tone is generated locally and contains no user media.
        value = int(1000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        data.extend(struct.pack("<h", value))
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + data)


def write_png(path: Path, width: int = 1280, height: int = 720) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if 120 <= x < 1160 and 220 <= y < 500:
                rgb = (20, 92, 121) if (x // 48 + y // 48) % 2 else (239, 139, 62)
            else:
                rgb = (245, 241, 230)
            rows.extend(rgb)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        header
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


def create_samples(output: Path) -> tuple[Path, Path]:
    if output.exists() and not output.is_dir():
        raise ValueError(f"output must be a directory: {output}")
    output = output.resolve()
    image = output / "sample.png"
    audio = output / "sample.wav"
    write_png(image)
    write_wav(audio)
    return image, audio


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Generate privacy-free demo media")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("samples/generated"),
        help="Output directory (default: samples/generated)",
    )
    args = parser.parse_args()
    try:
        image, audio = create_samples(args.output)
    except (OSError, ValueError) as exc:
        print(f"sample-media: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"image={image}")
    print(f"audio={audio}")


if __name__ == "__main__":
    main()
