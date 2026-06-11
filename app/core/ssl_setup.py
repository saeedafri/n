# app/core/ssl_setup.py
import os
import urllib.request
from pathlib import Path
from typing import Optional

from utils.server_logger import log_structured_error

CA_URL = "https://dl.cacerts.digicert.com/DigiCertGlobalRootG2.crt.pem"
CA_FILENAME = "DigiCertGlobalRootG2.crt.pem"

def ensure_ca_cert() -> Optional[str]:
    """
    Ensure DigiCert Global Root G2 CA cert exists locally and set SSL_CA env var.

    Search order:
      1. Bundled cert next to the app directory (shipped with the repo)
      2. Azure-persistent writable home ($HOME/site/wwwroot/)
      3. Current working directory
    Only downloads from DigiCert if not found in any of those locations.
    Returns absolute path to CA file, or None if all attempts fail.
    """
    try:
        # 1. Check for bundled cert shipped with the repo (app/DigiCertGlobalRootG2.crt.pem)
        #    This file lives alongside core/, so go up one level from this script's directory.
        _app_dir = Path(__file__).resolve().parent.parent
        bundled = _app_dir / CA_FILENAME
        if bundled.exists():
            os.environ["SSL_CA"] = str(bundled)
            return str(bundled)

        # 2. Check Azure-persistent writable home
        azure_dir = Path(os.getenv("HOME", ".")) / "site" / "wwwroot"
        if azure_dir.exists():
            azure_path = (azure_dir / CA_FILENAME).resolve()
            if azure_path.exists():
                os.environ["SSL_CA"] = str(azure_path)
                return str(azure_path)

        # 3. Check current working directory
        cwd_path = (Path.cwd() / CA_FILENAME).resolve()
        if cwd_path.exists():
            os.environ["SSL_CA"] = str(cwd_path)
            return str(cwd_path)

        # 4. Last resort: download (may fail behind corporate proxies / restricted envs)
        target = (azure_dir / CA_FILENAME).resolve() if azure_dir.exists() else cwd_path
        with urllib.request.urlopen(CA_URL, timeout=15) as resp:
            target.write_bytes(resp.read())

        os.environ["SSL_CA"] = str(target)
        return str(target)
    except Exception as e:
        log_structured_error(e, page="", component="ensure_ca_cert", operation="ensure CA certificate exists")
        return None
