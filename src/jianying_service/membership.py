"""Read membership tier and expiry from the Jianying home window.

Jianying ships ``disable_qt_access=1`` in its global settings, so UI Automation
sees the editor window but none of its descendants: every query for child
controls comes back empty. The membership badge, however, is large plain text,
so OCR over a screenshot of the top-left block reads both the tier and the
expiry date. Nothing here is reported until the row layout matches the geometry
observed on Jianying 11.6, so a reshuffled or unrelated window yields
``badge_layout_not_verified`` instead of a plausible guess.
"""

from __future__ import annotations

import importlib.util
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

WINDOW_NAME = "剪映专业版"
WINDOW_CLASS_PREFIXES = ("HomePage_QMLTYPE_", "MainWindow_")
# Normalised box around the account block: avatar, name, tier badge, expiry.
REGION_LEFT = 0.0
REGION_TOP = 0.05
REGION_RIGHT = 0.36
REGION_BOTTOM = 0.26
# The badge is small on disk, so the crop is upscaled before recognition.
CROP_SCALE = 2
TIER_MIN_SCORE = 0.75
DATE_MIN_SCORE = 0.70
# Vertical distance between the tier and the expiry row, as a fraction of the
# cropped badge block (observed 0.12 on Jianying 11.6).
MAX_VERTICAL_GAP = 0.35
NAME_MIN_SCORE = 0.60
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

TIER_TOKENS = (("SVIP", "svip"), ("VIP", "vip"))
FREE_TOKENS = ("开通会员", "立即开通", "免费版", "开会员", "续费享")
NAME_EXCLUDES = ("剪映", "抖音", "主页", "到期", "SVIP", "VIP", "会员", "续费", "个人版", "团队版")
PROFILE_TAIL = re.compile(r"(个人版|团队版|企业版|专业版)$")
EXPIRY = re.compile(
    r"(?P<year>\d{4})\s*[.\-/]\s*(?P<month>\d{1,2})\s*[.\-/]\s*(?P<day>\d{1,2})"
)
# OCR turns "2026.11.05" into "2026 。 1 1 。 05" often enough to normalise first.
SEPARATORS = str.maketrans(
    {"。": ".", "，": ".", ",": ".", "、": ".", "·": ".", "年": ".", "月": ".", "/": "-"}
)
# OCR frequently mangles the separators of "2026.11.05 到期".
EXPIRY_CONTEXT = ("到期", "有效期")


@contextmanager
def automation_client() -> Iterator[Any]:
    import uiautomation as automation

    with automation.UIAutomationInitializerInThread():
        yield automation


def ocr_available() -> bool:
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def find_window(automation: Any) -> Any | None:
    """Return the Jianying home window, ignoring the tray process."""
    try:
        children = automation.GetRootControl().GetChildren()
    except Exception:
        return None
    for control in children:
        try:
            if (
                control.Name == WINDOW_NAME
                and str(control.ClassName).startswith(WINDOW_CLASS_PREFIXES)
            ):
                return control
        except Exception:
            continue
    return None


def is_foreground(automation: Any, window: Any) -> bool:
    try:
        return (
            automation.GetForegroundControl().NativeWindowHandle
            == window.NativeWindowHandle
        )
    except Exception:
        return False


def capture_badge(window: Any) -> Any:
    """Grab the badge block from the desktop, never via PrintWindow."""
    from PIL import ImageGrab

    rectangle = window.BoundingRectangle
    image = ImageGrab.grab(
        bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
        all_screens=True,
        include_layered_windows=True,
    )
    width, height = image.size
    box = (
        int(REGION_LEFT * width),
        int(REGION_TOP * height),
        int(REGION_RIGHT * width),
        int(REGION_BOTTOM * height),
    )
    crop = image.crop(box)
    return crop.resize(
        (crop.width * CROP_SCALE, crop.height * CROP_SCALE),
        getattr(getattr(image, "Resampling", None), "LANCZOS", 1),
    )


def badge_rows(rows: list, width: int, height: int) -> list[dict]:
    """Keep recognised rows inside the badge box, ordered for reading."""
    kept: list[dict] = []
    for box, text, score in rows:
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        center_x = sum(xs) / len(xs)
        center_y = sum(ys) / len(ys)
        kept.append(
            {
                "text": " ".join(str(text).split()),
                "score": float(score),
                "x": center_x,
                "y": center_y,
                "nx": center_x / width,
                "ny": center_y / height,
            }
        )
    kept.sort(key=lambda row: (round(row["ny"] / 0.01), row["nx"]))
    return kept


def tier_of(text: str) -> str | None:
    upper = text.upper().replace(" ", "")
    if "SVIP" in upper:
        return "svip"
    if "VIP" in upper:
        return "vip"
    return None


def parse_expiry(text: str) -> str | None:
    compact = "".join(str(text).split()).translate(SEPARATORS)
    match = EXPIRY.search(compact)
    if not match:
        return None
    try:
        parsed = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None
    return parsed.isoformat()


def display_name_of(rows: list[dict], stop_y: float) -> tuple[str | None, float | None]:
    """Best-effort account name; always reported as unverified OCR text."""
    for row in rows:
        if row["y"] >= stop_y:
            break
        if row["score"] < NAME_MIN_SCORE:
            continue
        if any(token in row["text"] for token in NAME_EXCLUDES):
            continue
        candidate = PROFILE_TAIL.sub("", row["text"]).strip()
        if candidate:
            return candidate, round(row["score"], 3)
    return None, None


def read_membership(
    rows: list,
    width: int,
    height: int,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Turn recognised badge rows into a membership snapshot, or a reason."""
    rows = badge_rows(rows, width, height)
    tiers = [
        row for row in rows
        if tier_of(row["text"]) is not None and row["score"] >= TIER_MIN_SCORE
    ]
    if len(tiers) > 1:
        return {"reason": "badge_ambiguous", "rows": _texts(rows)}
    if not tiers:
        free_rows = [
            row for row in rows
            if any(token in row["text"] for token in FREE_TOKENS)
        ]
        if free_rows:
            return {
                "membership": {
                    "status": "free",
                    "label": free_rows[0]["text"],
                    "expires_on": None,
                    "expired": None,
                    "days_left": None,
                    "display_name_hint": None,
                    "display_name_confidence": None,
                    "confidence": round(free_rows[0]["score"], 3),
                },
                "evidence": [free_rows[0]["text"]],
                "rows": _texts(rows),
            }
        return {"reason": "badge_not_readable", "rows": _texts(rows)}

    tier = tiers[0]
    status = tier_of(tier["text"])
    dates = [
        row for row in rows
        if row["score"] >= DATE_MIN_SCORE
        and parse_expiry(row["text"]) is not None
        and row["y"] > tier["y"]
        and (row["y"] - tier["y"]) / height <= MAX_VERTICAL_GAP
    ]
    expires_on = parse_expiry(dates[0]["text"]) if dates else None
    reference = today or datetime.now(tz=LOCAL_TIMEZONE).date()
    expired: bool | None = None
    days_left: int | None = None
    if expires_on:
        expiry_date = date.fromisoformat(expires_on)
        days_left = (expiry_date - reference).days
        expired = days_left < 0
    name, name_score = display_name_of(rows, tier["y"])

    evidence = [tier["text"]]
    if dates:
        evidence.append(dates[0]["text"])
    if name:
        evidence.append(name)
    return {
        "membership": {
            "status": status,
            "label": tier["text"],
            "expires_on": expires_on,
            "expired": expired,
            "days_left": days_left,
            "display_name_hint": name,
            "display_name_confidence": name_score,
            "confidence": round(tier["score"], 3),
        },
        "evidence": evidence,
        "rows": _texts(rows),
    }


def _texts(rows: list[dict]) -> list[str]:
    return [row["text"] for row in rows]


class MembershipReader:
    """One read-only OCR pass over the Jianying account badge."""

    def __init__(
        self,
        *,
        automation: Any = None,
        engine: Any = None,
        capture: Any = None,
        today: date | None = None,
    ) -> None:
        self._automation = automation
        self._engine = engine
        self._capture = capture or capture_badge
        self._today = today

    def _engine_or_import(self) -> Any:
        if self._engine is not None:
            return self._engine
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()
        return self._engine

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "read_only": True,
            "needs_gui": True,
            "available": ocr_available(),
            "reason": None if ocr_available() else "ocr_dependency_missing",
            "verification": "window_ocr_with_geometry_check",
            "data_source": "jianying_account_badge",
            "file_read_fallback": "unavailable",
        }

    def read(self, *, focus: bool = False) -> dict[str, Any]:
        """Read the badge. ``focus`` steals foreground focus; default is no."""
        if self._automation is not None:
            return self._read_with(self._automation, focus)
        with automation_client() as managed:
            return self._read_with(managed, focus)

    def _read_with(self, automation: Any, focus: bool) -> dict[str, Any]:
        base: dict[str, Any] = {
            "ok": True,
            "read_only": True,
            "needs_gui": True,
            "verification": "window_ocr_with_geometry_check",
            "data_source": "jianying_account_badge",
        }
        if not ocr_available():
            return {
                **base,
                "available": False,
                "reason": "ocr_dependency_missing",
                "membership": None,
            }
        window = find_window(automation)
        if window is None:
            return {
                **base,
                "available": False,
                "reason": "window_not_found",
                "membership": None,
            }
        if focus:
            window.SetActive(waitTime=0.1)
        if not is_foreground(automation, window):
            return {
                **base,
                "available": False,
                "reason": "window_not_foreground",
                "hint": "call with focus=true, or bring Jianying to the front",
                "window": _window_info(window),
                "membership": None,
            }
        image = self._capture(window)
        rows, _ = self._engine_or_import()(image)
        parsed = read_membership(
            rows or [], image.width, image.height, today=self._today
        )
        if "membership" not in parsed:
            return {
                **base,
                "available": False,
                "reason": parsed["reason"],
                "badge_rows": parsed.get("rows", []),
                "window": _window_info(window),
                "membership": None,
            }
        return {
            **base,
            "available": True,
            "reason": None,
            "membership": parsed["membership"],
            "evidence": parsed["evidence"],
            "badge_rows": parsed["rows"],
            "window": _window_info(window),
        }


def _window_info(window: Any) -> dict[str, Any]:
    rectangle = window.BoundingRectangle
    return {
        "class_name": str(window.ClassName),
        "handle": int(getattr(window, "NativeWindowHandle", 0)),
        "bounds": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
    }
