"""Memory optimisation for cached page frames — lossless categorical encoding."""
import os

from utils.server_logger import log_warning as slog_warning


def _categorical_on() -> bool:
    return os.getenv("MDP_CATEGORICAL", "1").strip().lower() in ("1", "true", "yes", "on")


def audit_frame_numbers(name: str, df) -> None:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame):
            return
        _n = len(df)
        parts = [f"rows={_n}"]
        if "ticker" in df.columns:
            parts.append(f"tickers={df['ticker'].nunique()}")
        if "sector" in df.columns:
            _g = df.groupby("sector", observed=True).size()
            parts.append("by_sector=" + repr({str(k): int(v) for k, v in _g.sort_index().items()}))
        slog_warning(f"[AUDIT][{name}] " + " | ".join(parts))
    except Exception as _e:
        slog_warning(f"[AUDIT][{name}] failed: {type(_e).__name__}: {str(_e)[:120]}")


def optimize_frame_memory(df, name: str = "", ratio: float = 0.5, min_rows: int = 1000):
    try:
        import pandas as pd
        if df is None or not _categorical_on() or not isinstance(df, pd.DataFrame) or len(df) < min_rows:
            return df
        _before = int(df.memory_usage(deep=True).sum())
        conv = {}
        _n = len(df)
        for c in df.columns:
            if df[c].dtype == object:
                try:
                    if pd.api.types.infer_dtype(df[c], skipna=True) != "string":
                        continue
                    if df[c].nunique(dropna=False) <= _n * ratio:
                        conv[c] = "category"
                except Exception:
                    continue
        if conv:
            df = df.astype(conv)
            df.attrs["_memopt_cols"] = list(conv)
            _after = int(df.memory_usage(deep=True).sum())
            slog_warning(
                f"[MEM_OPT] {name or 'frame'}: {len(conv)} cols→category "
                f"{_before/1e6:.0f}MB→{_after/1e6:.0f}MB (saved {(_before-_after)/1e6:.0f}MB) "
                f"cols={list(conv)}"
            )
        return df
    except Exception as _e:
        slog_warning(f"[MEM_OPT] {name or 'frame'}: skipped ({type(_e).__name__}: {str(_e)[:100]})")
        return df


def decategorize(df):
    try:
        if df is None:
            return df
        import pandas as pd
        if not isinstance(df, pd.DataFrame):
            return df
        _tagged = list(getattr(df, "attrs", {}).get("_memopt_cols", []) or [])
        _cat = [c for c in _tagged if c in df.columns and isinstance(df[c].dtype, pd.CategoricalDtype)]
        if not _cat:
            return df
        return df.astype({c: object for c in _cat})
    except Exception:
        return df


def release_memory() -> None:
    """Hand freed heap back to OS after heavy builds. Safe no-op off Linux."""
    try:
        import ctypes as _ct
        _ct.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
