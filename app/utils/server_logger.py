"""
Server Logger - Custom logging to file for debugging
Logs are stored in project_root/server-logs/server-log.log

PERFORMANCE PROFILING ADDED:
- Function execution timing
- DB query timing
- Component render timing
- Memory usage tracking

ENV FLAGS (read once at module import):
  APP_LOG_LEVEL=WARNING   Suppress INFO/DEBUG, keep WARNING+ERROR+TIMING (default: WARNING)
  APP_TIMING=1            When set, only [TIMING] and WARNING+ messages pass through
  APP_VERBOSE_LOGGING=0   When 0, suppress verbose non-timing INFO/DEBUG messages

LOG FORMAT:
  %(asctime)s | %(levelname)-7s | %(rerun_id)-8s | page=%(page)-18s | %(filename)s:%(funcName)s | %(message)s

RERUN ID:
  Uses contextvars.ContextVar (Streamlit-safe) with threading.local as fallback.
  Call new_rerun_id('page_name') at the top of each page/main before any logging.
  The ContextEnrichFilter injects rerun_id + page into every LogRecord automatically.
"""
import os
import copy
import logging
import logging.handlers
import time
import traceback as _tb
import functools
import threading
import contextvars
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import contextmanager
from queue import Queue

# IST timezone offset (UTC+5:30) — no external dependency
_IST = timezone(timedelta(hours=5, minutes=30))

# Server logs directory (at project root)
SERVER_LOGS_DIR = Path(__file__).parent.parent.parent / "server-logs"
SERVER_LOG_FILE = SERVER_LOGS_DIR / "server-log.log"

# Thread-local storage — fallback for contexts where ContextVar isn't inherited
_thread_local = threading.local()

# ============================================================================
# CORRELATION ID — one per Streamlit rerun; stored in contextvars + thread-local
# ============================================================================
_rerun_id_counter = 0
_rerun_id_lock = threading.Lock()

# ContextVar: the preferred carrier — survives async boundaries within a rerun
_rerun_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    'server_logger_rerun_id', default='R?????'
)
_page_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    'server_logger_page', default='-'
)

# Module-level fallback for st.cache_data background threads.
# These threads are spawned by Streamlit's cache machinery and do NOT inherit
# contextvars from the calling thread. _last_known_page is set in the main
# request thread via new_rerun_id() and is readable by all threads.
_last_known_page: str = '-'


def new_rerun_id(page: str = '') -> str:
    """Generate a new rerun correlation ID.

    Call at the very top of each page entry-point (before require_auth),
    and at the top of main.py before any log_timing calls.

    Args:
        page: short page/context name e.g. 'earnings_calls', 'main', 'login'
    """
    global _rerun_id_counter, _last_known_page
    with _rerun_id_lock:
        _rerun_id_counter += 1
        rid = f"R{_rerun_id_counter:05d}"
    # Set in both ContextVar (primary) and thread-local (fallback)
    _rerun_id_var.set(rid)
    _thread_local.rerun_id = rid
    if page:
        _last_known_page = page  # global fallback for st.cache_data background threads
        _page_var.set(page)
        _thread_local.page = page
    return rid


def get_rerun_id() -> str:
    """Get current rerun correlation ID."""
    rid = _rerun_id_var.get()
    if rid == 'R?????':
        rid = getattr(_thread_local, 'rerun_id', 'R?????')
    return rid


def set_page_context(page: str) -> None:
    """Set the current page name for log enrichment without changing rerun id."""
    _page_var.set(page)
    _thread_local.page = page


def _get_page_context() -> str:
    page = _page_var.get()
    if not page or page == '-':
        page = getattr(_thread_local, 'page', '-')
    return page or '-'


# ============================================================================
# IST FORMATTER — timestamps in IST (UTC+5:30) with 12-hour AM/PM clock
# ============================================================================

class ISTFormatter(logging.Formatter):
    """Formats log timestamps in Indian Standard Time with AM/PM."""

    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=_IST)
        return ct.strftime('%d-%b-%Y %I:%M:%S %p IST')


# ============================================================================
# CONTEXT ENRICH FILTER — injects page name into every LogRecord
# ============================================================================

class ContextEnrichFilter(logging.Filter):
    """Inject page name from contextvars into every log record.

    Attached to the QueueHandler (runs on the caller thread, before the record
    is queued) so the background listener thread sees the enriched field.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Tier 1: contextvar (set on the Streamlit request thread)
        page = _page_var.get()
        # Tier 2: thread-local (same thread, different call path)
        if not page or page == '-':
            page = getattr(_thread_local, 'page', None)
        # Tier 3: module-level global (for st.cache_data background threads
        #          that don't inherit contextvars from the calling thread)
        if not page or page == '-':
            page = _last_known_page
        record.page = page or '-'
        return True  # always pass


# ============================================================================
# ENV-FLAG LOG LEVEL CONTROL (read once at module import, not per-call)
# ============================================================================
_RAW_LEVEL = os.getenv("APP_LOG_LEVEL", "WARNING").upper()
_EFFECTIVE_LEVEL: int = getattr(logging, _RAW_LEVEL, logging.WARNING)
_TIMING_ONLY: bool = os.getenv("APP_TIMING", "1").strip() == "1"
_VERBOSE: bool = os.getenv("APP_VERBOSE_LOGGING", "0").strip() != "0"

# Background queue listener (singleton, started once)
_queue_listener: Optional[logging.handlers.QueueListener] = None
_queue_listener_lock = threading.Lock()


def ensure_log_dir():
    """Create server-logs directory if it doesn't exist."""
    SERVER_LOGS_DIR.mkdir(parents=True, exist_ok=True)


def get_server_logger():
    """Get or create the server logger with synchronous FileHandler.
    Synchronous so every log line is flushed to disk immediately — no messages
    lost on crash or early exit.
    """
    ensure_log_dir()

    logger = logging.getLogger("server_logger")

    if not logger.handlers:
        with _queue_listener_lock:
            if not logger.handlers:
                logger.setLevel(logging.WARNING)
                logger.propagate = False

                formatter = ISTFormatter(
                    '%(asctime)s | %(levelname)-8s | %(page)-15s'
                    ' | %(filename)s:%(lineno)d:%(funcName)s'
                    ' | %(message)s'
                )

                # Synchronous file handler — writes immediately on every call
                fh = logging.FileHandler(SERVER_LOG_FILE, mode='a', encoding='utf-8')
                fh.setLevel(logging.WARNING)
                fh.setFormatter(formatter)
                fh.addFilter(ContextEnrichFilter())
                logger.addHandler(fh)

    return logger


# ============================================================================
# BASIC LOGGING FUNCTIONS
# stacklevel is propagated so %(filename)s/%(funcName)s show the CALLER,
# not server_logger.py itself.
#   log_info(msg)           → stacklevel=2 → frame calling log_info     ✓
#   log_timing → log_info   → stacklevel=3 → frame calling log_timing   ✓
# ============================================================================

def _is_timing_msg(message: str) -> bool:
    """Check if message is a timing/performance message that always passes through."""
    return (
        message.startswith("[TIMING]")
        or message.startswith("[PAGE_")
        or message.startswith("[DB_")
        or message.startswith("[AUTH_")
    )


def log_info(message: str, _stacklevel: int = 2):
    """Log an INFO level message. Timing-tagged messages always pass through.

    FIX (2026-03-31): Timing messages must be emitted at WARNING level to bypass
    the logger.setLevel(WARNING) gate. Previously, logger.info() was silently
    dropped by Python's logging framework before reaching any handler.
    """
    if _is_timing_msg(message):
        # Emit at WARNING level so the message passes logger.setLevel(WARNING).
        # The [TIMING]/[PAGE_]/[DB_]/[AUTH_] prefix in the message body
        # distinguishes these from real warnings during log analysis.
        get_server_logger().warning(message, stacklevel=_stacklevel)
        return
    if _EFFECTIVE_LEVEL > logging.INFO:
        return
    if _TIMING_ONLY:
        return
    get_server_logger().info(message, stacklevel=_stacklevel)


def log_debug(message: str, _stacklevel: int = 2):
    """Log a DEBUG level message. Suppressed if APP_LOG_LEVEL > DEBUG."""
    if _EFFECTIVE_LEVEL > logging.DEBUG:
        return
    if _TIMING_ONLY:
        return
    get_server_logger().debug(message, stacklevel=_stacklevel)


def log_warning(message: str, _stacklevel: int = 2):
    """Log a WARNING level message. Always passes through."""
    get_server_logger().warning(message, stacklevel=_stacklevel)


def log_error(message: str, _stacklevel: int = 2):
    """Log an ERROR level message. Always passes through."""
    get_server_logger().error(message, stacklevel=_stacklevel)


def log_exception(message: str, _stacklevel: int = 2):
    """Log an ERROR level message with exception info. Always passes through."""
    get_server_logger().exception(message, stacklevel=_stacklevel)


def log_critical(message: str, _stacklevel: int = 2):
    """Log a CRITICAL level message. Always passes through."""
    get_server_logger().critical(message, stacklevel=_stacklevel)


def log_structured_error(
    exc: Exception,
    *,
    page: str = "",
    component: str = "",
    operation: str = "",
    context: str = "",
    _stacklevel: int = 2,
) -> str:
    """Log a structured error with exception type, origin frame, and compact traceback.

    Returns:
        A compact origin string ``"file.py:func_name:42"`` for traceability.

    Log line format::

        [STRUCTURED_ERROR] page=newsroom | component=render_cards
        | op=DB_FETCH | type=ConnectionError | msg=Connection refused
        | file=repository.py:get_articles:882 | tb=...
    """
    tb = exc.__traceback__
    origin_file, origin_func, origin_line = "?", "?", "?"
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        origin_file = os.path.basename(tb.tb_frame.f_code.co_filename)
        origin_func = tb.tb_frame.f_code.co_name
        origin_line = str(tb.tb_lineno)

    parts = ["[STRUCTURED_ERROR]"]
    if page:
        parts.append(f"page={page}")
    if component:
        parts.append(f"component={component}")
    if operation:
        parts.append(f"op={operation}")
    parts.append(f"type={type(exc).__name__}")
    parts.append(f"msg={str(exc)[:200]}")
    parts.append(f"file={origin_file}:{origin_func}:{origin_line}")
    if context:
        parts.append(f"ctx={context}")

    tb_lines = _tb.format_exception(type(exc), exc, exc.__traceback__)
    tb_compact = "".join(tb_lines[-5:]).replace("\n", " | ").strip()
    if len(tb_compact) > 500:
        tb_compact = tb_compact[:500] + "..."

    msg = " | ".join(parts) + f" | tb={tb_compact}"
    log_error(msg, _stacklevel=_stacklevel + 1)
    return f"{origin_file}:{origin_func}:{origin_line}"


@contextmanager
def error_boundary(page: str = "", component: str = "", operation: str = "", reraise: bool = False):
    """Context manager that catches and logs any exception inside the block.

    Usage::

        with error_boundary(page="login", component="render_form"):
            render_form()
    """
    try:
        yield
    except Exception as exc:
        log_structured_error(exc, page=page, component=component, operation=operation, _stacklevel=3)
        if reraise:
            raise


def safe_execute(func, *args, page: str = "", component: str = "", fallback=None, **kwargs):
    """Execute *func* safely; on failure log a structured error and return *fallback*.

    Never raises.

    Usage::

        data = safe_execute(fetch_articles, date_from, date_to,
                            page="newsroom", component="DATA_LOAD", fallback=[])
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        log_structured_error(
            exc,
            page=page,
            component=component,
            operation=getattr(func, "__name__", str(func)),
            _stacklevel=3,
        )
        return fallback


# ============================================================================
# PERFORMANCE TIMING FUNCTIONS
# ============================================================================

def log_timing(operation: str, elapsed_ms: float, details: str = "", level: str = "INFO"):
    """Log a timing event.

    The rerun_id is injected by ContextEnrichFilter — do NOT embed it manually.
    File/function shown in log will be the caller of log_timing (stacklevel=3).

    Format produced:
        [TIMING] AUTH_RETRY_SCHEDULED | 500.00ms | attempt=2/5 delay=1.0s
    """
    msg = f"[TIMING] {operation} | {elapsed_ms:.2f}ms"
    if details:
        msg += f" | {details}"

    # stacklevel=3: skip log_timing frame + log_XXX frame → shows original caller
    if level == "DEBUG":
        log_debug(msg, _stacklevel=3)
    elif level == "WARNING":
        log_warning(msg, _stacklevel=3)
    elif level == "ERROR":
        log_error(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)

def log_db_timing(query_type: str, table: str, elapsed_ms: float, rows: int = -1, ticker: str = ""):
    """Log database query timing."""
    details = f"table={table}"
    if rows >= 0:
        details += f" rows={rows}"
    if ticker:
        details += f" ticker={ticker}"
    level = "WARNING" if elapsed_ms > 500 else "INFO"
    # stacklevel=4: log_db_timing → log_timing → log_XXX → logger.info → caller shown
    msg = f"[TIMING] DB_{query_type} | {elapsed_ms:.2f}ms | {details}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)

def log_render_timing(component: str, elapsed_ms: float, ticker: str = "", details: str = ""):
    """Log component rendering timing."""
    extra = ""
    if ticker:
        extra += f" ticker={ticker}"
    if details:
        extra += f" {details}"
    level = "WARNING" if elapsed_ms > 1000 else "INFO"
    msg = f"[TIMING] RENDER_{component} | {elapsed_ms:.2f}ms{extra.strip() and ' | ' + extra.strip()}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)


def log_function_timing(func_name: str, elapsed_ms: float, args_summary: str = ""):
    """Log function execution timing."""
    details = f" | {args_summary}" if args_summary else ""
    level = "WARNING" if elapsed_ms > 200 else "DEBUG"
    msg = f"[TIMING] FUNC_{func_name} | {elapsed_ms:.2f}ms{details}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_debug(msg, _stacklevel=3)

# ============================================================================
# CONTEXT MANAGERS FOR AUTOMATIC TIMING
# ============================================================================

@contextmanager
def timed_operation(operation: str, details: str = "", log_level: str = "INFO"):
    """
    Context manager for timing a block of code.

    Usage:
        with timed_operation("DATA_LOAD", "ticker=AAPL"):
            data = load_data()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_timing(operation, elapsed_ms, details, log_level)

@contextmanager
def timed_db_query(query_type: str, table: str, ticker: str = ""):
    """
    Context manager for timing database queries.

    Usage:
        with timed_db_query("SELECT", "coreiq_av_financials", "AAPL"):
            results = db.execute_query(query, params)
    """
    start = time.perf_counter()
    rows = -1
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing(query_type, table, elapsed_ms, rows, ticker)

@contextmanager
def timed_render(component: str, ticker: str = ""):
    """
    Context manager for timing component rendering.

    Usage:
        with timed_render("INCOME_STATEMENT", "AAPL"):
            render_income_statement(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_render_timing(component, elapsed_ms, ticker)

# ============================================================================
# DECORATORS FOR AUTOMATIC TIMING
# ============================================================================

def timed_function(log_args: bool = False, max_arg_length: int = 50):
    """
    Decorator to automatically time function execution.

    Usage:
        @timed_function(log_args=True)
        def get_company_data(ticker: str):
            return db.query(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            func_name = func.__name__

            # Build args summary if enabled
            args_summary = ""
            if log_args:
                args_str = ", ".join([
                    str(a)[:max_arg_length] for a in args if a is not None
                ])
                kwargs_str = ", ".join([
                    f"{k}={str(v)[:max_arg_length]}"
                    for k, v in kwargs.items() if v is not None
                ])
                all_args = ", ".join(filter(None, [args_str, kwargs_str]))
                args_summary = all_args[:100]  # Limit total length

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                log_function_timing(func_name, elapsed_ms, args_summary)

        return wrapper
    return decorator

def timed_db_operation(query_type: str, table: str):
    """
    Decorator to time database operations.

    Usage:
        @timed_db_operation("SELECT", "coreiq_av_financials_income_statement")
        def get_income_data(ticker: str):
            return db.query(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Try to extract ticker from first arg if it's a string
            ticker = ""
            if args and isinstance(args[0], str):
                ticker = args[0]

            with timed_db_query(query_type, table, ticker):
                return func(*args, **kwargs)

        return wrapper
    return decorator

# ============================================================================
# PAGE-LEVEL TIMING HELPERS
# ============================================================================

class PageTimer:
    """
    Helper class for timing page load and render phases.

    Usage:
        timer = PageTimer("market_data")
        timer.start_phase("db_fetch")
        data = fetch_data()
        timer.end_phase("db_fetch")

        timer.start_phase("render")
        render_page()
        timer.end_phase("render")

        timer.summary()  # Logs total and breakdown
    """

    def __init__(self, page_name: str, ticker: str = ""):
        self.page_name = page_name
        self.ticker = ticker
        self.phases: Dict[str, Dict[str, Any]] = {}
        self.current_phase: Optional[str] = None
        self.start_time = time.perf_counter()

    def start_phase(self, phase_name: str):
        """Start timing a phase."""
        self.current_phase = phase_name
        self.phases[phase_name] = {
            'start': time.perf_counter(),
            'end': None,
            'elapsed_ms': None
        }

    def end_phase(self, phase_name: Optional[str] = None):
        """End timing a phase."""
        phase = phase_name or self.current_phase
        if phase and phase in self.phases:
            self.phases[phase]['end'] = time.perf_counter()
            elapsed = self.phases[phase]['end'] - self.phases[phase]['start']
            self.phases[phase]['elapsed_ms'] = elapsed * 1000

    def summary(self):
        """Log summary of all phases."""
        total_elapsed = (time.perf_counter() - self.start_time) * 1000

        breakdown = " | ".join([
            f"{name}={data['elapsed_ms']:.1f}ms"
            for name, data in self.phases.items()
            if data['elapsed_ms'] is not None
        ])

        ticker_str = f" ticker={self.ticker}" if self.ticker else ""
        msg = (
            f"[TIMING] PAGE_SUMMARY | {total_elapsed:.1f}ms"
            f" | page={self.page_name}{ticker_str}"
            f"{' | ' + breakdown if breakdown else ''}"
        )

        if total_elapsed > 2000:
            log_warning(msg, _stacklevel=3)
        else:
            log_info(msg, _stacklevel=3)

# ============================================================================
# FILE OPERATIONS
# ============================================================================

def get_log_content(max_lines: int = 1000, max_bytes: int = 500000) -> str:
    """Get the current log file content."""
    ensure_log_dir()

    if not SERVER_LOG_FILE.exists():
        return "No logs yet. Start using the app to generate logs."

    try:
        # Read file
        with open(SERVER_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(max_bytes)

        # Get last N lines
        lines = content.split('\n')
        if len(lines) > max_lines:
            lines = lines[-max_lines:]

        return '\n'.join(lines)
    except Exception as e:
        return f"Error reading logs: {e}"

def clear_logs() -> bool:
    """Clear all logs from the log file."""
    ensure_log_dir()

    try:
        logger = logging.getLogger("server_logger")
        # Stop the queue listener (flushes the queue) then close handlers
        global _queue_listener
        if _queue_listener is not None:
            _queue_listener.stop()
            _queue_listener = None

        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

        # Truncate and write cleared marker
        ist_now = datetime.now(tz=_IST).strftime('%d-%b-%Y %I:%M:%S %p IST')
        with open(SERVER_LOG_FILE, 'w', encoding='utf-8') as f:
            f.write(
                f"{ist_now} | WARNING  | -              "
                f"| server_logger.py:0:clear_logs "
                f"| Logs cleared by user\n"
            )
        return True
    except Exception as e:
        print(f"Error clearing logs: {e}")
        return False

def download_logs() -> bytes:
    """Get log file content as bytes for download."""
    ensure_log_dir()

    if not SERVER_LOG_FILE.exists():
        return b"No logs available."

    try:
        with open(SERVER_LOG_FILE, 'rb') as f:
            return f.read()
    except Exception as e:
        return f"Error reading logs: {e}".encode('utf-8')

def get_log_stats() -> dict:
    """Get log file statistics."""
    ensure_log_dir()

    if not SERVER_LOG_FILE.exists():
        return {
            'exists': False,
            'size': 0,
            'lines': 0,
            'modified': None
        }

    try:
        stat = SERVER_LOG_FILE.stat()

        # Count lines
        with open(SERVER_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = sum(1 for _ in f)

        return {
            'exists': True,
            'size': stat.st_size,
            'lines': lines,
            'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'path': str(SERVER_LOG_FILE)
        }
    except Exception as e:
        return {
            'exists': False,
            'error': str(e)
        }

# ============================================================================
# PERFORMANCE ANALYSIS HELPERS
# ============================================================================

def analyze_slow_operations(log_lines: list = None) -> Dict[str, Any]:
    """
    Analyze logs to find slow operations.

    Returns:
        Dict with slowest DB queries, renders, and functions
    """
    if log_lines is None:
        content = get_log_content(max_lines=5000)
        log_lines = content.split('\n')

    slow_db_queries = []
    slow_renders = []
    slow_functions = []

    for line in log_lines:
        if '[TIMING]' in line:
            # Parse timing log
            try:
                # Extract operation and time
                parts = line.split('|')
                if len(parts) >= 3:
                    message = parts[2].strip()
                    if '[TIMING]' in message:
                        # Parse: [TIMING] OPERATION | TIMEms | details
                        timing_parts = message.split('|')
                        if len(timing_parts) >= 2:
                            op_parts = timing_parts[0].strip().split()
                            if len(op_parts) >= 2:
                                operation = op_parts[1]
                                time_str = timing_parts[1].strip()
                                if 'ms' in time_str:
                                    time_ms = float(time_str.replace('ms', '').strip())

                                    entry = {
                                        'operation': operation,
                                        'time_ms': time_ms,
                                        'details': timing_parts[2].strip() if len(timing_parts) > 2 else "",
                                        'line': line
                                    }

                                    if operation.startswith('DB_'):
                                        slow_db_queries.append(entry)
                                    elif operation.startswith('RENDER_'):
                                        slow_renders.append(entry)
                                    elif operation.startswith('FUNC_'):
                                        slow_functions.append(entry)
            except Exception:
                pass

    # Sort by time (descending)
    slow_db_queries.sort(key=lambda x: x['time_ms'], reverse=True)
    slow_renders.sort(key=lambda x: x['time_ms'], reverse=True)
    slow_functions.sort(key=lambda x: x['time_ms'], reverse=True)

    return {
        'slowest_db_queries': slow_db_queries[:10],
        'slowest_renders': slow_renders[:10],
        'slowest_functions': slow_functions[:10],
        'total_slow_operations': len(slow_db_queries) + len(slow_renders) + len(slow_functions)
    }


# ============================================================================
# COMPREHENSIVE PAGE LOAD SEQUENCE TRACKER (PRODUCT-GRADE)
# ============================================================================

class PageLoadTracker:
    """
    Product-grade page load sequence tracker.

    Tracks EVERY step of page load from the moment user clicks navigation
    to the final render completion. No step is missed.

    Usage in pages:
        from utils.server_logger import PageLoadTracker

        tracker = PageLoadTracker("market_data")
        tracker.step_start("HEADER_RENDER")
        render_header(...)
        tracker.step_end("HEADER_RENDER")

        tracker.step_start("DATA_FETCH")
        data = fetch_data()
        tracker.step_end("DATA_FETCH")

        tracker.finish()  # Logs complete breakdown

    Log Output Format:
        [PAGE_LOAD] market_data | STEP=HEADER_RENDER | elapsed=45.2ms
        [PAGE_LOAD] market_data | STEP=DATA_FETCH | elapsed=234.1ms
        [PAGE_LOAD] market_data | STEP=CONTENT_RENDER | elapsed=12.5ms
        [PAGE_LOAD] market_data | TOTAL=291.8ms | BREAKDOWN=HEADER:45.2,DATA:234.1,CONTENT:12.5
    """

    def __init__(self, page_name: str, ticker: str = "", user: str = ""):
        self.page_name = page_name
        self.ticker = ticker
        self.user = user
        self.steps = []
        self.current_step = None
        self.start_time = time.perf_counter()

        log_info(f"[PAGE_LOAD] === {page_name} | PAGE LOAD SEQUENCE START ===")

    def step_start(self, step_name: str, details: str = ""):
        """Start tracking a step."""
        self.current_step = {
            'name': step_name,
            'start': time.perf_counter(),
            'end': None,
            'elapsed_ms': None,
            'details': details
        }
        log_debug(f"[PAGE_LOAD] {self.page_name} | START {step_name}" + (f" | {details}" if details else ""))

    def step_end(self, step_name: str = None, details: str = ""):
        """End tracking a step."""
        step = self.current_step
        if step and (step_name is None or step['name'] == step_name):
            step['end'] = time.perf_counter()
            step['elapsed_ms'] = (step['end'] - step['start']) * 1000
            if details:
                step['details'] = details
            self.steps.append(step)

            log_info(f"[PAGE_LOAD] {self.page_name} | STEP={step['name']} | "
                    f"elapsed={step['elapsed_ms']:.2f}ms" +
                    (f" | {step['details']}" if step['details'] else ""))
            self.current_step = None

    def step(self, step_name: str, details: str = ""):
        """Context manager for a step."""
        class StepContext:
            def __init__(ctx_self, tracker, name, details):
                ctx_self.tracker = tracker
                ctx_self.name = name
                ctx_self.details = details

            def __enter__(ctx_self):
                ctx_self.tracker.step_start(ctx_self.name, ctx_self.details)
                return ctx_self

            def __exit__(ctx_self, exc_type, exc_val, exc_tb):
                ctx_self.tracker.step_end(ctx_self.name)
                return False

        return StepContext(self, step_name, details)

    def finish(self, status: str = "OK"):
        """Finish tracking and log complete summary."""
        total_elapsed = (time.perf_counter() - self.start_time) * 1000

        # Build breakdown string
        breakdown = ", ".join([
            f"{step['name']}:{step['elapsed_ms']:.1f}ms"
            for step in self.steps
        ])

        ticker_str = f" ticker={self.ticker}" if self.ticker else ""
        user_str = f" user={self.user}" if self.user else ""

        msg = (f"[PAGE_LOAD] {self.page_name}{ticker_str}{user_str} | "
               f"TOTAL={total_elapsed:.1f}ms | status={status} | BREAKDOWN={breakdown}")

        # Slow-page thresholds — always WARNING (never ERROR; slow != broken).
        # _stacklevel=3 skips log_warning() + finish() frames → shows the
        # actual page file (e.g. newsroom.py:render_newsroom) in the log.
        if total_elapsed > 3000:
            log_warning(f"[PAGE_LOAD_SLOW] {msg}", _stacklevel=3)
        elif total_elapsed > 1000:
            log_warning(f"[PAGE_LOAD_MEDIUM] {msg}", _stacklevel=3)
        else:
            log_info(msg, _stacklevel=3)

        log_info(f"[PAGE_LOAD] === {self.page_name} | PAGE LOAD SEQUENCE END ===")

        return {
            'page': self.page_name,
            'total_ms': total_elapsed,
            'steps': self.steps,
            'status': status
        }


# ============================================================================
# STREAMLIT-SPECIFIC PAGE LOAD WRAPPER
# ============================================================================

def track_page_load(page_name: str):
    """
    Decorator to automatically track page load sequence.

    Usage:
        @track_page_load("market_data")
        def render_page():
            render_header()
            render_content()
            render_footer()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tracker = PageLoadTracker(page_name)

            try:
                # Track the main function execution
                tracker.step_start("PAGE_FUNCTION")
                result = func(*args, **kwargs)
                tracker.step_end("PAGE_FUNCTION")

                tracker.finish("OK")
                return result
            except Exception as e:
                tracker.finish(f"ERROR: {str(e)[:50]}")
                raise

        return wrapper
    return decorator


# ============================================================================
# NAVIGATION CLICK TRACKER
# ============================================================================

class NavigationTracker:
    """
    Tracks navigation clicks and page transitions.

    This helps identify:
    - Which navigation links are clicked
    - Time from click to page start
    - Time from click to page fully loaded
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self.nav_click_time = None
        self.source_page = None
        self.target_page = None

    def record_nav_click(self, source_page: str, target_page: str):
        """Record when user clicks navigation."""
        self.nav_click_time = time.perf_counter()
        self.source_page = source_page
        self.target_page = target_page

        log_info(f"[NAV_CLICK] {source_page} -> {target_page} | click_recorded")

    def record_page_start(self, page_name: str):
        """Record when target page starts loading."""
        if self.target_page == page_name and self.nav_click_time:
            elapsed_ms = (time.perf_counter() - self.nav_click_time) * 1000
            log_info(f"[NAV_TRANSITION] {self.source_page} -> {page_name} | "
                    f"time_to_start={elapsed_ms:.1f}ms")
            return elapsed_ms
        return None


# Global navigation tracker instance
nav_tracker = NavigationTracker()
