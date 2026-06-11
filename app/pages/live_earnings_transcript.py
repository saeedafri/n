"""
Live Earnings Transcript Page - Coresight Research
==================================================
Chunked transcription workspace for earnings calls.

**Browser mic** here is for ad-hoc or **short** segments (quick capture, dress‑rehearsal).
**Full calls (often 1–2 hours)** are not realistically operated as dozens of manual
minute‑long browser recordings — use **Upload audio file** for a full recording from
another tool, or a future **server-side** (ffmpeg/HLS) chunking pipeline.
Transcription uses OpenAI or local faster-whisper.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st

from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import require_auth

require_auth(page="live_earnings_transcript")
hide_sidebar()

from components.navigation import render_coresight_footer, render_header
from data.live_earnings_transcript_store import (
    build_earnings_transcript_payload,
    export_period_key,
    normalize_ticker,
    upsert_live_transcript_row,
)

try:
    from utils.server_logger import log_structured_error
except ImportError:
    def log_structured_error(*a, **kw):  # type: ignore
        pass


BACKEND_OPTIONS = ["Local faster-whisper", "OpenAI API"]
OPENAI_MODEL_OPTIONS = [
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",
]
FASTER_WHISPER_MODEL_OPTIONS = ["small.en", "medium.en", "large-v3"]
DEVICE_OPTIONS = ["auto", "cpu", "cuda"]
COMPUTE_TYPE_OPTIONS = ["int8", "float16", "float32"]
# Ingestion mode (stored value must match checks below).
INGESTION_MIC = "Microphone (browser recorder)"
INGESTION_UPLOAD = "Upload audio file from computer"
INGESTION_SOURCE_OPTIONS = [INGESTION_MIC, INGESTION_UPLOAD]

QUARTER_OPTIONS = ["Q1", "Q2", "Q3", "Q4"]


def _safe_ticker(raw: str) -> str:
    """Normalize a user-entered ticker for metadata and filenames."""
    return normalize_ticker(raw)


def _init_state() -> None:
    defaults = {
        "live_call_active": False,
        "live_call_started_at": None,
        "live_transcript_segments": [],
        "live_processed_audio_hashes": set(),
        "live_audio_widget_version": 0,
        "live_activity_log": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _audio_digest(audio_bytes: bytes) -> str:
    return hashlib.sha256(audio_bytes).hexdigest()[:20]


def _now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_LIVE_ACTIVITY_MAX = 100


def _append_live_activity(message: str) -> None:
    log: List[str] = st.session_state.setdefault("live_activity_log", [])
    log.append(f"[{_now_label()}] {message}")
    if len(log) > _LIVE_ACTIVITY_MAX:
        st.session_state.live_activity_log = log[-_LIVE_ACTIVITY_MAX:]


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _elapsed_label(started_at: Optional[float]) -> str:
    if not started_at:
        return "00:00"
    elapsed = max(0, int(time.time() - started_at))
    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _build_transcription_prompt(
    company: str,
    quarter: str,
    fiscal_year: int,
    context_notes: str,
) -> str:
    details = [
        "This is an earnings call transcript.",
        "Preserve financial terms, ticker symbols, guidance language, KPIs, brand names, and speaker names.",
        "Prefer clean transcript prose over filler words when speech is unclear.",
    ]
    if company:
        details.append(f"Company/ticker: {company}.")
    if quarter and fiscal_year:
        details.append(f"Reporting period: {quarter} fiscal {fiscal_year}.")
    if context_notes.strip():
        details.append(f"Known context: {context_notes.strip()}")
    return " ".join(details)


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text.strip()
    if isinstance(response, dict):
        return str(response.get("text", "")).strip()
    return str(response).strip()


def _transcribe_audio_openai(
    audio_bytes: bytes,
    filename: str,
    model: str,
    language: str,
    prompt: str,
) -> str:
    """Transcribe one completed audio chunk through the OpenAI SDK."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed.") from exc

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename or "earnings-call-segment.wav"

    kwargs: Dict[str, Any] = {
        "model": model,
        "file": audio_file,
    }
    if language.strip():
        kwargs["language"] = language.strip()
    if prompt.strip():
        kwargs["prompt"] = prompt.strip()

    client = OpenAI(api_key=api_key)
    response = client.audio.transcriptions.create(**kwargs)
    return _extract_response_text(response)


@st.cache_resource
def _load_faster_whisper_model(model_size: str, device: str, compute_type: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "The faster-whisper package is not installed. "
            "Run: pip install faster-whisper av"
        ) from exc

    resolved = device
    if resolved == "auto":
        try:
            import torch

            resolved = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            resolved = "cpu"

    return WhisperModel(model_size, device=resolved, compute_type=compute_type)


def _guess_audio_suffix(audio_bytes: bytes, filename: Optional[str]) -> str:
    """Pick a file suffix so ffmpeg can decode browser/mic blobs reliably."""
    name = (filename or "").lower()
    for ext in (".wav", ".webm", ".mp3", ".m4a", ".ogg", ".flac", ".opus", ".mp4"):
        if name.endswith(ext):
            return ext
    if len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return ".wav"
    if len(audio_bytes) >= 4 and audio_bytes[:4] == b"OggS":
        return ".ogg"
    if len(audio_bytes) >= 12 and audio_bytes[4:8] == b"ftyp":
        return ".webm"
    return ".webm"


def _transcribe_audio_faster_whisper(
    audio_bytes: bytes,
    filename: str,
    model_size: str,
    device: str,
    compute_type: str,
    language: str,
    prompt: str,
) -> str:
    """Decode via a temp file — BytesIO often breaks format detection for mic/webm blobs."""
    model = _load_faster_whisper_model(model_size, device, compute_type)
    lang = (language or "").strip() or None
    ip = (prompt or "").strip() or None
    suffix = _guess_audio_suffix(audio_bytes, filename)
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(audio_bytes)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        segments, _info = model.transcribe(
            tmp_path,
            language=lang,
            initial_prompt=ip,
            # Short browser chunks are often classified as non-speech by the WebRTC VAD.
            vad_filter=False,
            beam_size=5,
        )
        parts: List[str] = []
        for seg in segments:
            t = (getattr(seg, "text", None) or "").strip()
            if t:
                parts.append(t)
        return " ".join(parts)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _transcribe_audio(
    audio_bytes: bytes,
    filename: str,
    backend: str,
    language: str,
    prompt: str,
    *,
    openai_model: str,
    faster_whisper_model: str,
    device: str,
    compute_type: str,
) -> str:
    if backend == "OpenAI API":
        return _transcribe_audio_openai(
            audio_bytes, filename, openai_model, language, prompt
        )
    if backend == "Local faster-whisper":
        return _transcribe_audio_faster_whisper(
            audio_bytes,
            filename,
            faster_whisper_model,
            device,
            compute_type,
            language,
            prompt,
        )
    raise RuntimeError(f"Unknown transcription backend: {backend}")


def _transcript_text(segments: List[Dict[str, Any]]) -> str:
    if not segments:
        return ""

    lines = []
    for idx, segment in enumerate(segments, start=1):
        time_label = segment.get("recorded_at", "")
        text = (segment.get("text") or "").strip()
        lines.append(f"[{idx:02d}] {time_label}\n{text}")
    return "\n\n".join(lines)


def _transcript_markdown(
    segments: List[Dict[str, Any]],
    company: str,
    quarter: str,
    fiscal_year: int,
) -> str:
    title = f"# Live Earnings Transcript - {_safe_ticker(company)} {quarter} FY{fiscal_year}"
    body = _transcript_text(segments)
    return f"{title}\n\nGenerated: {_now_label()}\n\n{body}".strip() + "\n"


def _render_css() -> None:
    st.markdown(
        """
        <style>
        .live-wrap {
            padding: 22px 0 36px;
            color: #2d2a29;
            font-family: Roboto, Arial, sans-serif;
        }
        .live-title-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 18px;
        }
        .live-title {
            margin: 0;
            font-family: Montserrat, Roboto, sans-serif;
            font-size: 28px;
            font-weight: 700;
            color: #323232;
        }
        .live-subtitle {
            margin: 6px 0 0;
            font-size: 14px;
            color: #6b6b6b;
            line-height: 1.45;
        }
        .live-pill {
            display: inline-flex;
            align-items: center;
            min-height: 32px;
            padding: 6px 12px;
            border-radius: 999px;
            border: 1px solid #e5e5e5;
            background: #fff;
            font-size: 13px;
            font-weight: 600;
            color: #4d4d4d;
            white-space: nowrap;
        }
        .live-pill.active {
            border-color: rgba(40, 167, 69, 0.35);
            background: #eefaf1;
            color: #1f7a3b;
        }
        .live-panel {
            border: 1px solid #e5e5e5;
            border-radius: 8px;
            background: #fff;
            padding: 18px;
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
        }
        .live-section-title {
            margin: 0 0 10px;
            font-family: Montserrat, Roboto, sans-serif;
            font-size: 16px;
            font-weight: 700;
            color: #323232;
        }
        .live-metrics {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }
        .live-metric {
            border: 1px solid #e9ecef;
            border-radius: 8px;
            padding: 12px 14px;
            background: #fafafa;
        }
        .live-metric-label {
            margin: 0 0 4px;
            font-size: 12px;
            color: #6b6b6b;
        }
        .live-metric-value {
            margin: 0;
            font-size: 22px;
            font-weight: 700;
            color: #2d2a29;
        }
        .transcript-stream {
            height: 520px;
            overflow-y: auto;
            padding: 4px 4px 4px 0;
        }
        .segment {
            border-left: 3px solid #d62e2f;
            padding: 12px 14px;
            margin-bottom: 12px;
            background: #fff;
            border-radius: 0 8px 8px 0;
            box-shadow: 0 1px 6px rgba(0, 0, 0, 0.06);
        }
        .segment-meta {
            margin: 0 0 8px;
            font-size: 12px;
            font-weight: 600;
            color: #6b6b6b;
        }
        .segment-text {
            margin: 0;
            white-space: pre-wrap;
            font-size: 15px;
            line-height: 1.62;
            color: #2d2a29;
        }
        .empty-transcript {
            height: 520px;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 1px dashed #d8d8d8;
            border-radius: 8px;
            color: #777;
            background: #fafafa;
            text-align: center;
            padding: 24px;
        }
        .idea-list {
            margin: 0;
            padding-left: 18px;
            color: #4d4d4d;
            font-size: 14px;
            line-height: 1.55;
        }
        div[data-testid="stButton"] > button {
            border-radius: 6px;
            min-height: 38px;
        }
        div[data-testid="stDownloadButton"] > button {
            border-radius: 6px;
            min-height: 38px;
        }
        @media (max-width: 900px) {
            .live-title-row {
                align-items: flex-start;
                flex-direction: column;
            }
            .live-metrics {
                grid-template-columns: 1fr;
            }
            .transcript-stream,
            .empty-transcript {
                height: 420px;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_metrics(segments: List[Dict[str, Any]]) -> None:
    words = sum(_word_count(segment.get("text", "")) for segment in segments)
    elapsed = _elapsed_label(st.session_state.get("live_call_started_at"))

    st.markdown(
        f"""
        <div class="live-metrics">
          <div class="live-metric">
            <p class="live-metric-label">Segments</p>
            <p class="live-metric-value">{len(segments)}</p>
          </div>
          <div class="live-metric">
            <p class="live-metric-label">Words</p>
            <p class="live-metric-value">{words:,}</p>
          </div>
          <div class="live-metric">
            <p class="live-metric-label">Elapsed</p>
            <p class="live-metric-value">{elapsed}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_transcript(segments: List[Dict[str, Any]]) -> None:
    if not segments:
        st.markdown(
            """
            <div class="empty-transcript">
                <div><strong>No segments yet.</strong> After each mic upload or file upload finishes transcribing, text shows here. Nothing streams live while you talk — only completed chunks.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    html = ['<div class="transcript-stream">']
    for idx, segment in enumerate(segments, start=1):
        text = (
            (segment.get("text") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        meta = (
            f"Segment {idx:02d} | {segment.get('recorded_at', '')} | "
            f"{segment.get('backend', '')} | {segment.get('model', '')}"
        )
        html.append(
            f"""
            <div class="segment">
                <p class="segment-meta">{meta}</p>
                <p class="segment-text">{text}</p>
            </div>
            """
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def _start_call() -> None:
    st.session_state.live_call_active = True
    if not st.session_state.get("live_call_started_at"):
        st.session_state.live_call_started_at = time.time()


def _end_call() -> None:
    st.session_state.live_call_active = False


def _clear_call() -> None:
    st.session_state.live_call_active = False
    st.session_state.live_call_started_at = None
    st.session_state.live_transcript_segments = []
    st.session_state.live_processed_audio_hashes = set()
    st.session_state.live_audio_widget_version = st.session_state.get("live_audio_widget_version", 0) + 1
    st.session_state.live_activity_log = []


def _process_uploaded_audio(
    uploaded_audio: Any,
    company: str,
    quarter: str,
    fiscal_year: int,
    backend: str,
    openai_model: str,
    faster_whisper_model: str,
    device: str,
    compute_type: str,
    language: str,
    context_notes: str,
) -> None:
    if uploaded_audio is None:
        return

    audio_bytes = uploaded_audio.getvalue()
    if not audio_bytes:
        _append_live_activity(
            "ABORT: audio widget returned zero bytes (stop recorder in other tab / wait for upload)."
        )
        st.warning(
            "No audio bytes were received. Finish recording in the recorder tab so the file can upload to this page."
        )
        return

    digest = _audio_digest(audio_bytes)
    processed = st.session_state.live_processed_audio_hashes
    if digest in processed:
        _append_live_activity(
            f"SKIP duplicate clip digest={digest[:12]}… ({len(audio_bytes):,} bytes)"
        )
        st.info(
            "This exact audio clip was already transcribed in this session. "
            "Use **Clear** if you want to start over, or record a new clip."
        )
        return

    prompt = _build_transcription_prompt(company, quarter, fiscal_year, context_notes)
    filename = getattr(uploaded_audio, "name", None) or f"{_safe_ticker(company)}-{digest}.wav"
    selected_model = (
        openai_model if backend == "OpenAI API" else faster_whisper_model
    )
    fmt_guess = _guess_audio_suffix(audio_bytes, filename)
    _append_live_activity(
        f"CLIP bytes={len(audio_bytes):,} fmt={fmt_guess} file={filename!r} "
        f"backend={backend} model={selected_model} digest={digest[:12]}…"
    )

    clip_mb = len(audio_bytes) / (1024 * 1024)
    if clip_mb > 40:
        _append_live_activity(f"WARN: large clip ({clip_mb:.1f} MB) — slow or may hit proxy limits.")

    text = ""
    elapsed_ms = 0.0
    if hasattr(st, "status"):
        with st.status("Transcribing audio…", expanded=True) as status:
            status.markdown(
                "**Long audio:** each run transcribes **one** blob end‑to‑end. Bigger files take longer "
                "(local Whisper on CPU can run for **many minutes** on long recordings). "
                "That is expected — keep this tab open. "
                "**Full 1–2 hour calls** belong in **Upload** or a backend chunking pipeline, not repeated "
                "browser mic captures."
            )
            status.write("Running the model… **Stay on this tab** until this finishes.")
            t0 = time.perf_counter()
            text = _transcribe_audio(
                audio_bytes=audio_bytes,
                filename=filename,
                backend=backend,
                language=language,
                prompt=prompt,
                openai_model=openai_model,
                faster_whisper_model=faster_whisper_model,
                device=device,
                compute_type=compute_type,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            status.write(f"Model finished in **{elapsed_ms / 1000:.1f} s**.")
            try:
                if (text or "").strip():
                    status.update(label="Transcription complete", state="complete")
                else:
                    status.update(label="No text from model", state="error")
            except Exception:
                pass
    else:
        with st.spinner(
            "Transcribing (long clips can take several minutes — do not close this tab)..."
        ):
            t0 = time.perf_counter()
            text = _transcribe_audio(
                audio_bytes=audio_bytes,
                filename=filename,
                backend=backend,
                language=language,
                prompt=prompt,
                openai_model=openai_model,
                faster_whisper_model=faster_whisper_model,
                device=device,
                compute_type=compute_type,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

    _append_live_activity(
        f"TRANSCRIBE done in {elapsed_ms:.0f} ms — "
        f"chars_out={len((text or '').strip())}"
    )

    if text:
        st.session_state.live_transcript_segments.append(
            {
                "id": digest,
                "recorded_at": _now_label(),
                "company": _safe_ticker(company),
                "quarter": quarter,
                "fiscal_year": int(fiscal_year),
                "backend": backend,
                "model": selected_model,
                "text": text,
            }
        )
        processed.add(digest)
        st.session_state.live_processed_audio_hashes = processed
        st.session_state.live_audio_widget_version = st.session_state.get("live_audio_widget_version", 0) + 1
        preview = (text.strip()[:120] + "…") if len(text.strip()) > 120 else text.strip()
        _append_live_activity(f"SUCCESS segment #{len(st.session_state.live_transcript_segments)} preview: {preview!r}")
        if not upsert_live_transcript_row(
            company,
            int(fiscal_year),
            quarter,
            st.session_state.live_transcript_segments,
        ):
            _append_live_activity("WARN: MySQL upsert failed (segment kept in session).")
            st.warning(
                "Transcript segment was added in this session, but saving to "
                "**coreiq_earnings_call_live_transcripts** failed. Check **Pipeline log** and DB connectivity."
            )
        else:
            _append_live_activity("MySQL upsert OK.")
        st.toast("Segment transcribed.")
        st.rerun()
    else:
        _append_live_activity(
            "FAIL: empty transcript (model returned no text — check mic level, format, or backend errors above)."
        )
        st.warning(
            "No transcript text was returned for this clip. "
            f"Received **{len(audio_bytes):,}** bytes (format hint: {_guess_audio_suffix(audio_bytes, filename)}). "
            "Try: speak closer to the mic, use **Upload** with a WAV/MP3, switch backend to **OpenAI API**, "
            "or set **Device** to **cpu** if GPU decode fails. "
            "Check the server log if faster-whisper raised an error."
        )


def _run_live_environment_check(backend: str) -> None:
    """Append import/DB checks to the pipeline log (no transcription)."""
    _append_live_activity("--- Environment check ---")
    _append_live_activity(
        f"Python executable (this Streamlit process): {sys.executable}"
    )
    fw_ok = False
    try:
        import faster_whisper  # noqa: F401

        fw_ok = True
        _append_live_activity("Import faster_whisper: OK")
    except Exception as e:
        _append_live_activity(f"Import faster_whisper: FAIL ({e!r})")
    av_ok = False
    try:
        import av  # noqa: F401

        av_ok = True
        _append_live_activity("Import av (PyAV): OK")
    except Exception as e:
        _append_live_activity(f"Import av: FAIL ({e!r})")
    if backend == "OpenAI API":
        has = bool(os.getenv("OPENAI_API_KEY", "").strip())
        _append_live_activity(f"OPENAI_API_KEY: {'present' if has else 'MISSING'}")
    try:
        from core.database import db_manager

        ok = db_manager.health_check()
        _append_live_activity(f"MySQL health_check: {'OK' if ok else 'FAIL'}")
    except Exception as e:
        _append_live_activity(f"MySQL health_check: ERROR ({e!r})")
    if not fw_ok or not av_ok:
        _append_live_activity(
            "FIX: Install into the SAME Python as above, e.g.: "
            f"{sys.executable} -m pip install faster-whisper av"
            "   — or: pip install -r requirements.txt (from project root, with that Python active)."
        )
    _append_live_activity("--- Environment check done ---")


def render_page() -> None:
    _init_state()
    _render_css()

    segments: List[Dict[str, Any]] = st.session_state.live_transcript_segments
    status_class = "active" if st.session_state.live_call_active else ""
    status_text = "Timer on (not live captions)" if st.session_state.live_call_active else "Timer off"

    st.markdown('<div class="live-wrap">', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="live-title-row">
          <div>
            <h1 class="live-title">Live Earnings Transcript</h1>
            <p class="live-subtitle">Transcribe audio in segments. <strong>Browser mic</strong> suits short capture; <strong>full 1–2h calls</strong>: upload a file from your recorder, or plan server-side chunking — not dozens of manual browser clips.</p>
          </div>
          <span class="live-pill {status_class}">{status_text}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _render_metrics(segments)

    left, right = st.columns([0.38, 0.62], gap="large")

    with left:
        st.markdown('<div class="live-panel">', unsafe_allow_html=True)
        st.markdown('<p class="live-section-title">Call Setup</p>', unsafe_allow_html=True)

        default_ticker = st.query_params.get("ticker", st.session_state.get("active_ticker", ""))
        company = st.text_input("Company / ticker", value=_safe_ticker(default_ticker), max_chars=20)

        c1, c2 = st.columns(2)
        with c1:
            quarter = st.selectbox("Quarter", QUARTER_OPTIONS, index=0)
        with c2:
            fiscal_year = st.number_input(
                "Fiscal year",
                min_value=2000,
                max_value=2100,
                value=datetime.now().year,
                step=1,
            )

        transcription_backend = st.selectbox(
            "Transcription backend", BACKEND_OPTIONS, index=0
        )
        openai_model = OPENAI_MODEL_OPTIONS[0]
        faster_whisper_model = FASTER_WHISPER_MODEL_OPTIONS[0]
        device = DEVICE_OPTIONS[0]
        compute_type = COMPUTE_TYPE_OPTIONS[0]

        if transcription_backend == "OpenAI API":
            openai_model = st.selectbox("OpenAI model", OPENAI_MODEL_OPTIONS, index=0)
        else:
            faster_whisper_model = st.selectbox(
                "Faster-whisper model", FASTER_WHISPER_MODEL_OPTIONS, index=0
            )
            device = st.selectbox("Device", DEVICE_OPTIONS, index=0)
            compute_type = st.selectbox(
                "Compute type", COMPUTE_TYPE_OPTIONS, index=0
            )

        language = st.text_input("Language", value="en", max_chars=12)
        context_notes = st.text_area(
            "Call context",
            value="",
            height=92,
            placeholder="Known speakers, product names, segment names, common metrics...",
        )

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("Start", type="primary", use_container_width=True):
                _start_call()
                st.rerun()
        with b2:
            if st.button("End", use_container_width=True):
                _end_call()
                st.rerun()
        with b3:
            if st.button("Clear", use_container_width=True):
                _clear_call()
                st.rerun()

        st.divider()
        st.markdown('<p class="live-section-title">Audio Segment</p>', unsafe_allow_html=True)
        st.caption(
            "Audio is not stored as files on the server. Each **finished** clip is transcribed once; "
            "only text is kept in this session."
        )

        ingestion_source = st.radio(
            "Add audio",
            INGESTION_SOURCE_OPTIONS,
            index=0,
            horizontal=True,
            help="Use **Upload** for a WAV/MP3 (or similar) from your machine — e.g. a full earnings call export.",
        )

        uploaded_audio = None
        if ingestion_source == INGESTION_MIC:
            st.info(
                "**Browser mic vs full call:** earnings calls are often **1–2 hours**. This widget is "
                "a **Streamlit browser recorder** — fine for **short segments** or demos, not a substitute "
                "for recording the whole call one uninterrupted take (upload limits, tabs, transcription "
                "runtime stack up).\n\n"
                "For **full audio**, record with your usual tool, export **WAV/MP3**, switch **Add audio** above to "
                "**Upload audio file from computer** (one file per pass; very large files may be slow). "
                "Server-side ffmpeg/HLS chunking is the right long-term shape for hour-long automation.\n\n"
                "- **Start** only affects the timer / status pill — you can record anytime.\n"
                "- After **Stop** in the recorder tab, return here for **Audio ready** and **Transcribing**.\n"
                "- If the upload stalls, use **Reload run** or switch **Add audio** to **Upload**."
            )
            audio_key = f"live_audio_input_{st.session_state.live_audio_widget_version}"
            uploaded_audio = st.audio_input(
                "Record next segment",
                key=audio_key,
                disabled=False,
                help=(
                    "Short browser segments; for full earnings calls use Upload with a file from your recorder."
                ),
            )
            st.caption(
                "No **Audio ready** after Stop? Try **Reload run** or switch **Add audio** to **Upload audio file from computer**."
            )
            if st.button(
                "Reload run (pick up upload / refresh widgets)",
                key="live_pickup_rerun",
                use_container_width=True,
            ):
                st.rerun()
        else:
            uploaded_audio = st.file_uploader(
                "Upload audio segment",
                type=["wav", "mp3", "m4a", "webm", "ogg", "flac"],
                key=f"live_audio_upload_{st.session_state.live_audio_widget_version}",
                disabled=False,
                help="Full or partial call from your recorder (MP3/WAV, etc.). Large files can take a long time to transcribe.",
            )

        # Transcribe whenever the widget returns bytes — including if the user clicked End before
        # this rerun: requiring live_call_active dropped in-flight clips that finished after End.
        if uploaded_audio is not None:
            _sz = len(uploaded_audio.getvalue())
            _nm = getattr(uploaded_audio, "name", None) or "(microphone chunk)"
            st.success(
                f"**Audio ready:** {_sz:,} bytes from `{_nm}`. Transcription runs on this run; "
                "open **Pipeline log** under Transcript Stream to verify each step."
            )
            try:
                _process_uploaded_audio(
                    uploaded_audio=uploaded_audio,
                    company=company,
                    quarter=quarter,
                    fiscal_year=int(fiscal_year),
                    backend=transcription_backend,
                    openai_model=openai_model,
                    faster_whisper_model=faster_whisper_model,
                    device=device,
                    compute_type=compute_type,
                    language=language,
                    context_notes=context_notes,
                )
            except Exception as exc:
                tb = traceback.format_exc()
                _append_live_activity(f"EXCEPTION: {exc!r}")
                for line in tb.splitlines():
                    _append_live_activity(line)
                log_structured_error(
                    exc,
                    page="live_earnings_transcript",
                    component="_process_uploaded_audio",
                    operation="TRANSCRIBE_AUDIO",
                )
                st.error(str(exc))
                with st.expander("Full traceback"):
                    st.code(tb)

        if transcription_backend == "OpenAI API" and not os.getenv(
            "OPENAI_API_KEY", ""
        ).strip():
            st.warning(
                "OPENAI_API_KEY is missing. Recording works, but transcription will fail until it is configured."
            )

        with st.expander("Local faster-whisper setup"):
            st.code("pip install faster-whisper av", language="bash")
            st.caption(
                " CUDA users should install a matching ctranslate2 build; CPU works out of the box."
            )

        with st.expander("Implementation paths"):
            st.markdown(
                """
                <ul class="idea-list">
                  <li><strong>MVP:</strong> Short browser-mic segments or file upload; OpenAI or local faster-whisper; MySQL + export — not a substitute for automated hour-long capture.</li>
                  <li><strong>Full earnings calls (1–2h):</strong> ingest a single recording via <strong>Upload</strong> (or ffmpeg/HLS server chunks), not repeated manual browser clips.</li>
                  <li><strong>Local faster-whisper:</strong> runs fully offline after models download; tune device and compute type for latency vs. accuracy.</li>
                  <li><strong>Virtual mic / system audio:</strong> route earnings audio through a virtual cable or OS loopback into the browser mic capture for Browser mic mode.</li>
                  <li><strong>Future ingestion:</strong> ffmpeg-driven HLS segment fetch for chunked transcription without the browser.</li>
                  <li><strong>Better live mode:</strong> browser MediaRecorder or WebRTC sends 5-15 second chunks to a backend queue.</li>
                  <li><strong>Lowest latency:</strong> a WebSocket bridge to a realtime transcription session with partial results.</li>
                  <li><strong>Production layer:</strong> store audio chunks, transcript segments, tickers, speaker tags, and review status in the database.</li>
                </ul>
                """,
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown('<div class="live-panel">', unsafe_allow_html=True)
        st.markdown('<p class="live-section-title">Transcript Stream</p>', unsafe_allow_html=True)

        if not segments and st.session_state.live_call_active:
            st.info(
                "**Timer is on** — that only tracks elapsed time. **This area does not fill while you speak.** "
                "There is **no live rolling transcript** in this version. Text appears **after** each completed "
                "step: **Stop** the browser recorder (or use **Upload**), then you must see **Audio ready** and "
                "**Transcribing…** above the left column. If you did that and it is still empty, open "
                "**Pipeline log** below for byte counts and errors."
            )
        elif not segments:
            st.caption(
                "Transcript text appears here **after** each file or finished mic clip is transcribed — not during recording."
            )

        log_lines: List[str] = st.session_state.get("live_activity_log") or []
        _expand_log = any(
            any(
                tag in (line.upper())
                for tag in ("FAIL", "EXCEPTION", "ABORT", "ERROR")
            )
            for line in log_lines[-8:]
        )
        with st.expander("Pipeline log (verify transcription)", expanded=_expand_log):
            if not log_lines:
                st.caption(
                    "No events yet. After you record/upload, lines appear here. "
                    "Use **Run environment check** to confirm faster-whisper, PyAV, and MySQL."
                )
            else:
                st.code("\n".join(log_lines), language=None)
            bv1, bv2 = st.columns(2)
            with bv1:
                if st.button("Run environment check", key="live_verify_env", width="stretch"):
                    _run_live_environment_check(transcription_backend)
                    st.rerun()
            with bv2:
                if st.button("Clear log", key="live_clear_activity", width="stretch"):
                    st.session_state.live_activity_log = []
                    st.rerun()

        _render_transcript(segments)

        if segments:
            transcript_md = _transcript_markdown(segments, company, quarter, int(fiscal_year))
            transcript_txt = _transcript_text(segments)
            export_payload = build_earnings_transcript_payload(
                company, int(fiscal_year), quarter, segments
            )
            transcript_json = json.dumps(export_payload, indent=2, ensure_ascii=False)
            period_key = export_period_key(int(fiscal_year), quarter)
            filename_base = f"{_safe_ticker(company)}_{period_key}_live_transcript"
            st.caption(
                "Structured JSON (**symbol**, **quarter**, **transcript**[]) is stored in MySQL "
                "table **coreiq_earnings_call_live_transcripts** "
                f"(source `live_streamlit`, ticker **{_safe_ticker(company)}**, quarter **{period_key}**) "
                "after each new segment. Download mirrors **raw_json**."
            )

            d1, d2, d3 = st.columns(3)
            with d1:
                st.download_button(
                    "Download MD",
                    data=transcript_md,
                    file_name=f"{filename_base}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )
            with d2:
                st.download_button(
                    "Download TXT",
                    data=transcript_txt,
                    file_name=f"{filename_base}.txt",
                    mime="text/plain",
                    use_container_width=True,
                )
            with d3:
                st.download_button(
                    "Download JSON",
                    data=transcript_json,
                    file_name=f"{filename_base}.json",
                    mime="application/json",
                    use_container_width=True,
                )
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    try:
        st.set_page_config(page_title="Live Earnings Transcript", layout="wide")
    except Exception:
        pass

    render_styles()
    set_page_layout(
        header_full_width=True,
        footer_full_width=True,
        body_padding="0 20px",
        max_content_width="1440px",
        remove_top_padding=True,
        footer_at_bottom=True,
    )

    header_ticker = st.query_params.get("ticker", st.session_state.get("active_ticker", "M")) or "M"
    render_header(full_width=True, current_page="live_earnings_transcript", ticker=header_ticker)
    render_page()
    render_coresight_footer(full_width=True, stick_to_bottom=True)


try:
    main()
except Exception as exc:
    log_structured_error(
        exc,
        page="live_earnings_transcript",
        component="main",
        operation="PAGE_RENDER",
    )
    st.error("Live transcription is temporarily unavailable. Please refresh the page.")


if __name__ == "__main__":
    pass
