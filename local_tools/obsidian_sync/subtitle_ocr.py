from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CJK_RE = re.compile(r"[\u3400-\u9fff]")


@dataclass
class OcrSegment:
    start: float
    end: float
    text: str


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def subtitle_ocr_config(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(config.get("subtitle_ocr", {}) or {})
    return {
        "interval_seconds": float(cfg.get("interval_seconds", 1.0)),
        "crop_top_ratio": float(cfg.get("crop_top_ratio", 0.48)),
        "crop_bottom_ratio": float(cfg.get("crop_bottom_ratio", 0.94)),
        "crop_left_ratio": float(cfg.get("crop_left_ratio", 0.03)),
        "crop_right_ratio": float(cfg.get("crop_right_ratio", 0.97)),
        "scale": float(cfg.get("scale", 2.0)),
        "min_confidence": float(cfg.get("min_confidence", 0.45)),
        "similarity_threshold": float(cfg.get("similarity_threshold", 0.86)),
        "min_transcript_chars": int(cfg.get("min_transcript_chars", 20)),
        "min_text_chars": int(cfg.get("min_text_chars", 2)),
        "min_cjk_chars": int(cfg.get("min_cjk_chars", 2)),
        "prefer_cjk": bool(cfg.get("prefer_cjk", True)),
        "max_frames": int(cfg.get("max_frames", 900)),
    }


def find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("ffmpeg not found. Install it first, e.g. `brew install ffmpeg`.")


def extract_frames(video_path: Path, frame_dir: Path, cfg: Dict[str, Any]) -> List[Path]:
    interval = max(0.25, float(cfg["interval_seconds"]))
    max_frames = max(1, int(cfg["max_frames"]))
    frame_pattern = frame_dir / "frame_%06d.jpg"
    cmd = [
        find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={1.0 / interval}",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(frame_pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg subtitle frame extraction failed: {result.stderr.strip()}")
    return sorted(frame_dir.glob("frame_*.jpg"))


def crop_and_enhance_image(image_path: Path, cfg: Dict[str, Any]) -> Any:
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is not installed. Run `.venv/bin/python -m pip install -r requirements-obsidian.txt`.") from exc

    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    left = int(width * min(max(cfg["crop_left_ratio"], 0.0), 1.0))
    right = int(width * min(max(cfg["crop_right_ratio"], 0.0), 1.0))
    top = int(height * min(max(cfg["crop_top_ratio"], 0.0), 1.0))
    bottom = int(height * min(max(cfg["crop_bottom_ratio"], 0.0), 1.0))
    if right <= left or bottom <= top:
        raise RuntimeError("字幕 OCR 裁剪区域配置无效")
    cropped = image.crop((left, top, right, bottom))
    scale = max(1.0, float(cfg["scale"]))
    if scale != 1.0:
        cropped = cropped.resize((int(cropped.width * scale), int(cropped.height * scale)), Image.Resampling.LANCZOS)
    cropped = ImageOps.grayscale(cropped)
    cropped = ImageOps.autocontrast(cropped)
    cropped = cropped.filter(ImageFilter.SHARPEN)
    return cropped


def clean_ocr_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    text = text.replace("|", "").replace("丨", "")
    return text.strip()


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def should_keep_ocr_text(text: str, cfg: Dict[str, Any]) -> bool:
    compact = clean_ocr_text(text)
    if len(compact) < int(cfg["min_text_chars"]):
        return False
    if not bool(cfg["prefer_cjk"]):
        return True
    return cjk_count(compact) >= int(cfg["min_cjk_chars"])


def comparable_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def similar_enough(left: str, right: str, threshold: float) -> bool:
    left_cmp = comparable_text(left)
    right_cmp = comparable_text(right)
    if not left_cmp or not right_cmp:
        return False
    if left_cmp in right_cmp or right_cmp in left_cmp:
        return True
    return difflib.SequenceMatcher(None, left_cmp, right_cmp).ratio() >= threshold


def parse_rapidocr_result(raw_result: Optional[List[List[Any]]], cfg: Dict[str, Any]) -> str:
    if not raw_result:
        return ""
    min_confidence = float(cfg["min_confidence"])
    lines: List[Tuple[float, float, str]] = []
    for item in raw_result:
        if not isinstance(item, list) or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        text = clean_ocr_text(str(text or ""))
        if not text or confidence < min_confidence or not should_keep_ocr_text(text, cfg):
            continue
        y = 0.0
        x = 0.0
        if isinstance(box, list) and box:
            points = [point for point in box if isinstance(point, (list, tuple)) and len(point) >= 2]
            if points:
                y = sum(float(point[1]) for point in points) / len(points)
                x = sum(float(point[0]) for point in points) / len(points)
        lines.append((y, x, text))
    lines.sort(key=lambda item: (item[0], item[1]))
    return clean_ocr_text("".join(item[2] for item in lines))


def merge_segments(frame_texts: Iterable[Tuple[float, str]], interval: float, similarity_threshold: float) -> List[OcrSegment]:
    segments: List[OcrSegment] = []
    for timestamp, text in frame_texts:
        text = clean_ocr_text(text)
        if not text:
            continue
        if segments and similar_enough(segments[-1].text, text, similarity_threshold):
            segments[-1].end = max(segments[-1].end, timestamp + interval)
            if len(text) > len(segments[-1].text):
                segments[-1].text = text
            continue
        segments.append(OcrSegment(start=timestamp, end=timestamp + interval, text=text))
    return segments


def transcribe_subtitles_from_video(video_path: Path, config: Dict[str, Any]) -> str:
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError("RapidOCR 依赖未安装。请运行 `.venv/bin/python -m pip install -r requirements-obsidian.txt`.") from exc

    cfg = subtitle_ocr_config(config)
    interval = max(0.25, float(cfg["interval_seconds"]))
    with tempfile.TemporaryDirectory(prefix="subtitle_ocr_") as temp_dir:
        frame_dir = Path(temp_dir)
        frames = extract_frames(video_path, frame_dir, cfg)
        if not frames:
            raise RuntimeError("字幕 OCR 没有抽取到视频帧")

        ocr = RapidOCR()
        frame_texts: List[Tuple[float, str]] = []
        for index, frame_path in enumerate(frames):
            enhanced = crop_and_enhance_image(frame_path, cfg)
            raw_result, _ = ocr(np.array(enhanced))
            text = parse_rapidocr_result(raw_result, cfg)
            if text:
                frame_texts.append((index * interval, text))

    segments = merge_segments(frame_texts, interval, float(cfg["similarity_threshold"]))
    transcript = "\n".join(f"[{format_seconds(item.start)} - {format_seconds(item.end)}] {item.text}" for item in segments).strip()
    plain_chars = len(re.sub(r"\s+", "", "\n".join(item.text for item in segments)))
    if plain_chars < int(cfg["min_transcript_chars"]):
        raise RuntimeError(f"字幕 OCR 识别文字太少（{plain_chars} 字），可能字幕区域不准或视频没有清晰硬字幕")
    return transcript
