"""Export pyJianYingDraft's effect/filter/transition/animation/font catalogs as JSON.

The AI editing feature ports JianYingAPI's draft writer to Node; these catalogs are
the only data it still needs from pyJianYingDraft (Apache-2.0). Regenerate with the
JianYingAPI virtualenv after upgrading pyJianYingDraft:

    <JianYingAPI>/.venv/bin/python -I node/scripts/ai-edit/export-jianying-catalog.py
"""

import dataclasses
import json
import sys
from enum import Enum
from pathlib import Path

import pyJianYingDraft as draft_lib

ENUMS = [
    "VideoSceneEffectType", "VideoCharacterEffectType", "AudioSceneEffectType",
    "ToneEffectType", "SpeechToSongType", "FilterType", "MaskType", "MixModeType",
    "FontType", "TransitionType", "IntroType", "OutroType", "GroupAnimationType",
    "TextIntro", "TextOutro", "TextLoopAnim",
]


def plain(value):
    if isinstance(value, Enum):
        return plain(value.value)
    if dataclasses.is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: plain(item) for key, item in vars(value).items()}
    return value


def main() -> None:
    catalog = {
        "source": f"pyJianYingDraft {getattr(draft_lib, '__version__', '0.3.1')}",
        "enums": {
            name: [{"name": member_name, "meta": plain(member.value)}
                   for member_name, member in getattr(draft_lib, name).__members__.items()]
            for name in ENUMS
        },
    }
    target = Path(__file__).resolve().parents[2] / "aiEdit" / "catalog.json"
    target.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(target, target.stat().st_size, file=sys.stderr)


if __name__ == "__main__":
    main()
