"""Generate the edge-case media oracle for node/aiEdit/media.mjs from the original JianYingAPI.

node/aiEdit/media.mjs replaces ffprobe with a pure JS container parser that reproduces the
timing rules of ffprobe (FFmpeg 9). The 17 shared files in node/fixtures/ai-edit/media only
cover the plain cases, so this script renders small samples with ffmpeg for the branches that are
easy to get wrong (edit lists, B-frame and negative composition offsets without an edit list,
fragmented / sidx / CMAF mp4, timecode, chapter and subtitle tracks, the WAV codecs and RF64,
MP3 Xing / VBRI / MPEG-2.5 / Layer II, Ogg and Matroska variants, AVI, truncated files), builds a
few fragmented mp4 files with hostile trun boxes by hand, then runs the original
MediaResolver.probe (which calls ffprobe) on every file and stores the results.

Run from anywhere with the JianYingAPI virtualenv; ffmpeg and ffprobe must be on PATH. gif2webp
and cwebp (libwebp) are optional and only add the two WebP samples:

    <venv>/bin/python -I node/scripts/ai-edit/generate-media-fixtures.py --jianying-api <JianYingAPI checkout>

Output (the directory is emptied and rewritten on every run):
- node/fixtures/ai-edit/media-extra/<name>: the samples, a few KB to a few dozen KB each.
- node/fixtures/ai-edit/media-extra/oracle.json: {name: {kind, duration_us, width, height,
  audio, video}} or {name: {"error": CODE}}, the same shape as ../media-info.json.
- node/fixtures/ai-edit/media-extra/frames.json: {name: microseconds} for the raw .aac / .mp3
  samples, the sum of ffprobe's packet durations (what counting every frame gives).

ffmpeg output differs between builds, so the samples are committed together with the oracle and
rerunning the script regenerates both. media.test.mjs compares every entry exactly, except the
intentional differences it lists: audio with a cover picture (held to the oracle of the same audio
without the picture, nocover.*) and raw AAC / MPEG audio without a Xing header (held to frames.json).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHARED = REPO / "fixtures" / "ai-edit" / "media"
OUT = REPO / "fixtures" / "ai-edit" / "media-extra"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--jianying-api", required=True, type=Path, help="JianYingAPI checkout (read only)")
parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: ffmpeg on PATH)")
args = parser.parse_args()
sys.path.insert(0, str(args.jianying_api.resolve() / "src"))

from jianying_service.errors import ValidationFailure  # noqa: E402
from jianying_service.media import MediaResolver  # noqa: E402


def video(size: str = "128x72", rate: str = "25", extra: str = "") -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}{extra}"]


def tone(rate: int = 44100, extra: str = "") -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}{extra}"]


H264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "40"]
HEVC = ["-c:v", "libx265", "-tag:v", "hvc1"]
AAC = ["-c:a", "aac", "-b:a", "32k"]
MPEG4 = ["-c:v", "mpeg4", "-q:v", "31"]
LAME = ["-c:a", "libmp3lame"]
V, A, A8 = video(), tone(), tone(8000)
COVER = ["-i", "{dir}/cover.jpg", "-map", "0:a", "-map", "1:v", "-c", "copy", "-disposition:v", "attached_pic"]

# name -> ffmpeg arguments (output path appended), rendered in this order. "{dir}" is the scratch
# directory, "{out}" the output directory (for samples derived from an earlier one).
SAMPLES: dict[str, list[str]] = {
    # ISO BMFF: edit lists, composition offsets, extra tracks.
    "noedit.mp4": [*V, *A, "-t", "2.3", *H264, "-bf", "2", *AAC, "-use_editlist", "0"],
    "negcts.mp4": [*V, *A, "-t", "2.3", *H264, "-bf", "3", *AAC, "-movflags", "negative_cts_offsets", "-use_editlist", "0"],
    "hevc-noedit.mp4": [*V, *A, "-t", "2.3", *HEVC, "-x265-params", "bframes=4:crf=40:log-level=none", *AAC, "-use_editlist", "0"],
    "hevc.mov": [*V, *A, "-t", "2", *HEVC, "-x265-params", "crf=40:log-level=none", *AAC],
    "delayed-audio.mp4": [*V, "-itsoffset", "0.5", *A, "-t", "2", *H264, *AAC, "-shortest"],
    "delayed-all.mov": ["-itsoffset", "0.4", *V, "-itsoffset", "0.4", *A, "-t", "2", *H264, *AAC],
    "negoffset.mov": [*V, "-itsoffset", "-0.5", *A, "-t", "2", *H264, *AAC],
    "tsoffset.mp4": [*V, *A, "-t", "2", *H264, *AAC, "-output_ts_offset", "3"],
    "trimcopy.mp4": ["-ss", "0.7", "-i", "{dir}/trim-src.mp4", "-t", "1.5", "-c", "copy"],
    "trimcopy-hevc.mov": ["-ss", "0.7", "-i", "{dir}/trim-src-hevc.mp4", "-t", "1.1", "-c", "copy"],
    "seek.mov": ["-ss", "0.3", *V, *A, "-t", "2.5", *H264, *AAC],
    "longaudio.mp4": [*video(extra=":duration=1"), *tone(extra=":duration=3"), *H264, *AAC],
    "twoaudio-long.mp4": [
        *video(extra=":duration=1"), *tone(extra=":duration=1.5"), *tone(extra=":duration=3.7"),
        "-map", "0", "-map", "1", "-map", "2", *H264, *AAC,
    ],
    "chapters.mp4": [*V, *A, "-i", "{dir}/chapters.txt", "-map", "0", "-map", "1", "-map_chapters", "2", "-t", "2", *H264, *AAC],
    "chapters.m4a": [*A, "-i", "{dir}/chapters.txt", "-map", "0", "-map_chapters", "1", "-t", "2", *AAC],
    "longsub.mp4": [*V, "-i", "{dir}/sub-4.5.srt", "-t", "2", *H264, "-c:s", "mov_text", "-map", "0", "-map", "1"],
    "ntsc.mov": [*video(rate="30000/1001"), *A, "-t", "2.2", *H264, *AAC, "-video_track_timescale", "30000"],
    "vfr.mp4": [*V, *A, "-t", "2", "-vf", "setpts='if(lt(N,20),PTS*2,PTS)'", "-fps_mode", "vfr", *H264, *AAC],
    "mp3inmp4.mp4": [*V, *A, "-t", "2", *H264, *LAME, "-b:a", "32k"],
    "alac.m4a": [*A8, "-t", "1.7", "-c:a", "alac"],
    "pcm.mov": [*V, *A8, "-t", "1.2", *H264, "-c:a", "pcm_s16le"],
    "prores.mov": [*video(size="64x36"), *A8, "-t", "0.4", "-c:v", "prores", "-c:a", "pcm_s16le"],
    "plain.mov": [*V, *A, "-t", "2.3", *H264, *AAC, "-f", "mov", "-write_tmcd", "0"],
    "tmcd.mov": [*V, *A, "-t", "2", *H264, *AAC, "-timecode", "00:00:10:00"],
    "brand.mp4": [*V, *A, "-t", "2.3", *H264, *AAC, "-f", "mp4", "-brand", "3gp4"],
    "rotated.mp4": [*V, *A, "-t", "2", *H264, *AAC, "-metadata:s:v", "rotate=90"],
    "minute.mp4": [*video(size="64x36", rate="5"), "-t", "61", *H264, "-g", "250"],
    "video-only.m4a": [*V, "-t", "1.5", *H264],
    # The same audio once more with a cover picture (stream copy), so the port can be held to the
    # oracle of the plain file.
    "nocover.m4a": [*A, "-t", "2", *AAC],
    "cover.m4a": ["-i", "{out}/nocover.m4a", *COVER],
    # Fragmented mp4.
    "frag-audio.m4a": [*A, "-t", "2.3", *AAC, "-movflags", "frag_keyframe+empty_moov"],
    "multi-frag.mp4": [*V, *A, "-t", "3.2", *H264, "-g", "10", *AAC, "-movflags", "frag_keyframe+empty_moov"],
    "sidx-frag.mp4": [
        *V, *A, "-t", "3.2", *H264, "-g", "10", *AAC, "-movflags", "frag_keyframe+empty_moov+default_base_moof+global_sidx",
    ],
    "sepmoof.mp4": [*V, *A, "-t", "2.3", *H264, "-g", "10", *AAC, "-movflags", "frag_keyframe+empty_moov+separate_moof"],
    "cmaf.mp4": [*V, *A, "-t", "2.4", *H264, "-bf", "2", "-g", "12", *AAC, "-movflags", "cmaf+frag_keyframe"],
    "delaymoov.mp4": [*V, *A, "-t", "2", *H264, "-g", "10", *AAC, "-movflags", "frag_keyframe+empty_moov+delay_moov"],
    # WAV (wavdec.c).
    "s24.wav": [*A8, "-t", "0.65", "-c:a", "pcm_s24le"],
    "f32.wav": [*A8, "-t", "0.65", "-c:a", "pcm_f32le"],
    "u8.wav": [*A8, "-t", "1.25", "-c:a", "pcm_u8"],
    "ext.wav": [*A8, "-t", "0.3", "-af", "pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0", "-c:a", "pcm_s16le"],
    "mulaw.wav": [*A8, "-t", "1.3", "-c:a", "pcm_mulaw"],
    "alaw.wav": [*A8, "-t", "1.3", "-c:a", "pcm_alaw"],
    "ima.wav": [*A8, "-t", "1.3", "-c:a", "adpcm_ima_wav"],
    "msadpcm.wav": [*A8, "-t", "1.3", "-c:a", "adpcm_ms"],
    "bext.wav": [*A8, "-t", "1.3", "-c:a", "pcm_s16le", "-write_bext", "1"],
    "rf64.wav": [*A8, "-t", "1.3", "-c:a", "pcm_s16le", "-rf64", "always"],
    "listinfo.wav": [*A8, "-t", "1.3", "-c:a", "pcm_s16le", "-metadata", "title=x", "-metadata", "artist=y"],
    # MP3 (mp3dec.c).
    "tagged.mp3": [*A, "-t", "3.3", *LAME, "-b:a", "32k", "-metadata", "title=hello", "-metadata", "artist=me"],
    "vbr.mp3": [*A, "-t", "5.3", *LAME, "-q:a", "9"],
    "cbr-noxing.mp3": [*A, "-t", "5.3", *LAME, "-b:a", "32k", "-write_xing", "0"],
    "vbr-noxing.mp3": [*A, "-t", "4.4", *LAME, "-q:a", "2", "-write_xing", "0"],
    "mono22k.mp3": [*tone(22050), "-t", "4.1", *LAME, "-ac", "1", "-b:a", "32k"],
    "mpeg25.mp3": [*A8, "-t", "3.3", "-ac", "1", *LAME],
    "layer2.mp3": [*A, "-t", "2.6", "-c:a", "mp2", "-b:a", "64k", "-f", "mp2"],
    "mp3-48k.mp3": [*tone(48000), "-t", "2.6", *LAME, "-b:a", "64k"],
    "mp3-8k-noxing.mp3": [*A8, "-t", "2.6", "-ac", "1", *LAME, "-write_xing", "0"],
    "v24.mp3": [*A, "-t", "2.6", *LAME, "-b:a", "32k", "-id3v2_version", "4", "-metadata", "title=x"],
    "nocover.mp3": [*A, "-t", "2.7", *LAME, "-b:a", "32k", "-id3v2_version", "3"],
    "cover.mp3": ["-i", "{out}/nocover.mp3", *COVER, "-id3v2_version", "3"],
    # Raw AAC, FLAC, Ogg.
    "long.aac": [*A, "-t", "7.7", "-c:a", "aac", "-b:a", "24k"],
    "hi.flac": [*tone(48000), "-t", "0.3", "-ac", "2", "-c:a", "flac", "-sample_fmt", "s32"],
    "oggflac.ogg": [*A8, "-t", "1.3", "-c:a", "flac", "-f", "ogg"],
    "vorbis.ogg": [*A, "-t", "2.1", "-c:a", "vorbis", "-strict", "-2", "-ac", "2"],
    "longvorbis.ogg": [*A8, "-t", "9.5", "-c:a", "vorbis", "-strict", "-2", "-ac", "2"],
    "mono.ogg": [*tone(48000), "-t", "1.3", "-c:a", "libopus", "-ac", "1", "-b:a", "24k"],
    "longopus.ogg": [*A, "-t", "9.5", "-c:a", "libopus", "-b:a", "16k"],
    # Matroska / WebM.
    "h264aac.mkv": [*V, *A, "-t", "2.2", *H264, *AAC],
    "audioonly.mkv": [*A, "-t", "2.2", *AAC],
    "vp8vorbis.webm": [
        *video(size="176x144", rate="30"), *A, "-t", "1.9", "-c:v", "libvpx", "-b:v", "100k",
        "-c:a", "vorbis", "-strict", "-2", "-ac", "2",
    ],
    "vp9.webm": [*V, *A, "-t", "2.3", "-c:v", "libvpx-vp9", "-b:v", "100k", "-c:a", "libopus", "-b:a", "24k"],
    "clusters.mkv": [*V, *A, "-t", "2", *H264, *AAC, "-f", "matroska", "-cluster_time_limit", "200"],
    "live.webm": [*V, *A, "-t", "2.2", "-c:v", "libvpx", "-b:v", "100k", "-c:a", "libopus", "-b:a", "24k", "-f", "webm", "-live", "1"],
    "livemkv.mkv": [*V, *A, "-t", "2.3", *H264, *AAC, "-f", "matroska", "-live", "1"],
    "sub.mkv": [*V, *A, "-i", "{dir}/sub-9.5.srt", "-t", "2", "-map", "0", "-map", "1", "-map", "2", *H264, *AAC, "-c:s", "srt"],
    # AVI.
    "mpeg4mp3.avi": [*V, *A, "-t", "2.4", *MPEG4, *LAME, "-b:a", "32k"],
    "mpeg4pcm.avi": [*V, *A8, "-t", "1.4", *MPEG4, "-c:a", "pcm_s16le"],
    "audio-only.avi": [*A8, "-t", "1.5", "-c:a", "pcm_s16le"],
    "longaudio.avi": [*video(extra=":duration=0.5"), *tone(8000, ":duration=1.5"), *MPEG4, "-c:a", "pcm_s16le"],
    "vbrmp3.avi": [*V, *A, "-t", "2.4", *MPEG4, *LAME, "-q:a", "2"],
    # Images (content and extension may disagree on purpose).
    "prog.jpg": [*video(size="333x222"), "-frames:v", "1", "-c:v", "mjpeg"],
    "gif-as.jpg": [*video(size="333x222"), "-frames:v", "1", "-f", "gif"],
    "jpg-as.png": [*video(size="333x222"), "-frames:v", "1", "-c:v", "mjpeg", "-f", "mjpeg"],
    "p16.png": [*video(size="96x64"), "-frames:v", "1", "-pix_fmt", "rgb48be"],
    "pal.png": [*video(size="96x64"), "-frames:v", "1", "-pix_fmt", "pal8"],
}

# Rendered into the scratch directory first, then cut, prefixed or converted into the sample.
INTERMEDIATE: dict[str, list[str]] = {
    "trim-src.mp4": [*V, *A, "-t", "3", *H264, *AAC],
    "trim-src-hevc.mp4": [*V, *A, "-t", "3", *HEVC, "-x265-params", "crf=40:log-level=none", *AAC],
    "faststart.mp4": [*V, *A, "-t", "3", *H264, *AAC, "-movflags", "+faststart"],
    "moovend.mp4": [*V, *A, "-t", "3", *H264, *AAC],
    "full.wav": [*A8, "-t", "2", "-c:a", "pcm_s16le"],
    "opus.ogg": [*A, "-t", "3", "-c:a", "libopus", "-b:a", "24k"],
    "plain.aac": [*A, "-t", "2.6", "-c:a", "aac", "-b:a", "48k", "-f", "adts"],
    "frame.png": [*video(size="210x160"), "-frames:v", "1"],
    "cover.jpg": [*video(size="64x64"), "-frames:v", "1", "-c:v", "mjpeg"],
    "anim.gif": [*video(size="64x48", rate="5"), "-t", "0.6"],
}

CHAPTERS = """;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=1000
title=a
[CHAPTER]
TIMEBASE=1/1000
START=1000
END=5000
title=b
"""


def ffmpeg(arguments: list[str], output: Path, scratch: Path, stdout=None) -> None:
    command = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    command += [item.replace("{dir}", str(scratch)).replace("{out}", str(output.parent)) for item in arguments]
    command += [str(output)] if stdout is None else []
    subprocess.run(command, check=True, stdout=stdout, stdin=subprocess.DEVNULL)


def keep_head(source: Path, target: Path, fraction: float) -> None:
    data = source.read_bytes()
    target.write_bytes(data[: int(len(data) * fraction)])


def box(kind: str, *bodies: bytes) -> bytes:
    body = b"".join(bodies)
    return struct.pack(">I", 8 + len(body)) + kind.encode("latin1") + body


def tfhd(flags: int, track_id: int, *fields: int) -> bytes:
    return box("tfhd", struct.pack(">II", flags, track_id), *(struct.pack(">I", value) for value in fields))


def trun(flags: int, entries: int, *fields: int) -> bytes:
    return box("trun", struct.pack(">II", flags, entries), *(struct.pack(">I", value) for value in fields))


def write_trun_cases(out: Path) -> list[str]:
    """ftyp + moov of the shared fragmented.mp4 followed by one hand-built moof (mov_read_trun limits)."""
    source = (SHARED / "fragmented.mp4").read_bytes()
    parts: dict[str, bytes] = {}
    position = 0
    while position < len(source):
        size = struct.unpack(">I", source[position : position + 4])[0]
        parts.setdefault(source[position + 4 : position + 8].decode("latin1"), source[position : position + size])
        position += size
    head = parts["ftyp"] + parts["moov"]
    mdat = box("mdat", bytes(100))
    cases = {
        # trex default size is 0 and the trun gives no sizes: rejected before any loop.
        "trun-zero-size-huge.mp4": box("moof", box("traf", tfhd(0x08, 1, 1), trun(0, 0xFFFFFFFF))),
        # Default size 1, no per-sample fields: more samples than the file has bytes.
        "trun-default-size-huge.mp4": box("moof", box("traf", tfhd(0x18, 1, 1, 1), trun(0, 10_000_000))),
        # Same layout within the limit: durations come from count x default duration.
        "trun-default-size-ok.mp4": box("moof", box("traf", tfhd(0x18, 1, 512, 1), trun(0, 1000))) + box("mdat", bytes(1000)),
        # Per-sample sizes, but the box only holds 10 of the 50 announced entries.
        "trun-per-entry-short.mp4": box("moof", box("traf", tfhd(0x08, 1, 512), trun(0x200, 50, *[1] * 10))) + mdat,
        # Per-sample duration + size, exactly as many as announced.
        "trun-per-entry-ok.mp4": box(
            "moof", box("traf", tfhd(0x00, 1), trun(0x300, 10, *[1000 + i if i % 2 == 0 else 1 for i in range(20)]))
        )
        + mdat,
        # Per-sample durations only while the default size is 0.
        "trun-per-entry-nosize.mp4": box("moof", box("traf", tfhd(0x08, 1, 512), trun(0x100, 10, *[700] * 10))) + mdat,
        # tfhd with track id 0.
        "trun-tfhd-zero.mp4": box("moof", box("traf", tfhd(0x18, 0, 512, 1), trun(0, 10))) + mdat,
        # trun without a tfhd in the moof.
        "trun-no-tfhd.mp4": box("moof", box("traf", trun(0, 10))) + mdat,
        # An empty trun must not move the next trun's start.
        "trun-empty-then.mp4": box("moof", box("traf", tfhd(0x18, 1, 512, 1), trun(0, 0), trun(0, 20))) + mdat,
    }
    for name, moof in cases.items():
        (out / name).write_bytes(head + moof)
    return list(cases)


def packet_sum_us(path: Path) -> int:
    """Length of the first audio stream by adding up ffprobe's packet durations (exact, floored to us)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=time_base:packet=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
    )
    data = json.loads(result.stdout)
    numerator, denominator = (int(part) for part in data["streams"][0]["time_base"].split("/"))
    total = sum(int(packet["duration"]) for packet in data["packets"])
    return total * numerator * 1_000_000 // denominator


def main() -> None:
    out = Path(os.path.realpath(OUT))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    names: list[str] = []
    with tempfile.TemporaryDirectory() as raw_scratch:
        scratch = Path(raw_scratch)
        (scratch / "chapters.txt").write_text(CHAPTERS, encoding="utf-8")
        (scratch / "sub-4.5.srt").write_text("1\n00:00:00,000 --> 00:00:04,500\nhello\n", encoding="utf-8")
        (scratch / "sub-9.5.srt").write_text("1\n00:00:00,000 --> 00:00:09,500\nhello\n", encoding="utf-8")
        for name, arguments in INTERMEDIATE.items():
            ffmpeg(arguments, scratch / name, scratch)
        for name, arguments in SAMPLES.items():
            ffmpeg(arguments, out / name, scratch)
            names.append(name)
        with open(out / "piped.flac", "wb") as piped:
            ffmpeg([*A, "-t", "1.3", "-c:a", "flac", "-f", "flac", "pipe:1"], out / "piped.flac", scratch, stdout=piped)
        names.append("piped.flac")
        # moov in front: the cut only loses media data; moov at the end: the cut loses the moov.
        keep_head(scratch / "faststart.mp4", out / "truncated-faststart.mp4", 0.6)
        keep_head(scratch / "moovend.mp4", out / "truncated-moovend.mp4", 0.6)
        keep_head(scratch / "full.wav", out / "truncated.wav", 0.6)
        keep_head(scratch / "opus.ogg", out / "truncated.ogg", 0.6)
        # ID3v2.4 header with 10 bytes of padding in front of raw ADTS.
        (out / "id3.aac").write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x0a" + bytes(10) + (scratch / "plain.aac").read_bytes())
        names += ["truncated-faststart.mp4", "truncated-moovend.mp4", "truncated.wav", "truncated.ogg", "id3.aac"]
        if shutil.which("cwebp"):
            subprocess.run(["cwebp", "-quiet", "-lossless", str(scratch / "frame.png"), "-o", str(out / "lossless.webp")], check=True)
            names.append("lossless.webp")
        else:
            print("cwebp not found: skipping lossless.webp", file=sys.stderr)
        if shutil.which("gif2webp"):
            subprocess.run(["gif2webp", "-quiet", str(scratch / "anim.gif"), "-o", str(out / "anim.webp")], check=True)
            names.append("anim.webp")
        else:
            print("gif2webp not found: skipping anim.webp", file=sys.stderr)
    names += write_trun_cases(out)

    resolver = MediaResolver((out,), "ffprobe", 8 * 1024**3)
    oracle: dict[str, dict] = {}
    for name in sorted(names):
        try:
            info = resolver.probe(str(out / name))
        except ValidationFailure as error:
            oracle[name] = {"error": error.code}
            continue
        oracle[name] = {
            "kind": info.kind,
            "duration_us": info.duration_us,
            "width": info.width,
            "height": info.height,
            "audio": info.audio,
            "video": info.video,
        }
    (out / "oracle.json").write_text(json.dumps(oracle, indent=1) + "\n", encoding="utf-8")
    # Raw ADTS / MPEG audio carry no total length; ffprobe estimates it from the bit rate (except for a
    # Xing/VBRI header), the port counts frames. This is the frame count to hold the port to there.
    frames = {name: packet_sum_us(out / name) for name in sorted(names) if Path(name).suffix in (".aac", ".mp3")}
    (out / "frames.json").write_text(json.dumps(frames, indent=1) + "\n", encoding="utf-8")
    total = sum(path.stat().st_size for path in out.iterdir())
    print(f"{len(oracle)} samples, {total / 1024:.0f} KB in {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
