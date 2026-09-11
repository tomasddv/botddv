from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import time
import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / ".runtime_modules"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Asistente-Comercial-DDV/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_RAW_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download_via_api(repo: str, path: str, branch: str) -> str:
    """
    Descarga un archivo público usando GitHub Contents API.
    Evita depender de raw.githubusercontent.com, que puede devolver 503.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    last_error = None

    for attempt in range(4):
        try:
            response = requests.get(
                url,
                params={"ref": branch},
                headers=_headers(),
                timeout=45,
            )
            if response.status_code == 200:
                payload = response.json()
                content = payload.get("content", "")
                encoding = payload.get("encoding", "")
                if encoding == "base64" and content:
                    return base64.b64decode(content).decode("utf-8")
                download_url = payload.get("download_url")
                if download_url:
                    raw = requests.get(
                        download_url,
                        headers={"User-Agent": "Asistente-Comercial-DDV/1.0"},
                        timeout=45,
                    )
                    raw.raise_for_status()
                    return raw.text
                raise RuntimeError("GitHub no devolvió contenido del archivo.")

            # 403 puede ser rate limit; 5xx puede ser temporal.
            last_error = RuntimeError(
                f"GitHub API respondió HTTP {response.status_code}: {response.text[:180]}"
            )
        except Exception as exc:
            last_error = exc

        if attempt < 3:
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"No se pudo descargar {repo}/{path}: {last_error}")


def _download_via_raw(repo: str, path: str, branch: str) -> str:
    """Fallback secundario si la API de GitHub falla."""
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Asistente-Comercial-DDV/1.0"},
                timeout=45,
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Fallback raw falló: {last_error}")


def load_remote_module(repo: str, path: str, branch: str = "main", force: bool = False):
    """
    Carga la lógica de otro Dash.

    Orden:
    1) cache local si ya existe y no se fuerza actualización;
    2) GitHub Contents API;
    3) raw.githubusercontent como fallback;
    4) si la actualización falla pero existe cache, usa la cache.
    """
    digest = hashlib.sha1(f"{repo}:{branch}:{path}".encode()).hexdigest()[:12]
    local = CACHE_DIR / f"{Path(path).stem}_{digest}.py"

    if force or not local.exists():
        old_text = local.read_text(encoding="utf-8") if local.exists() else None
        errors = []
        new_text = None

        try:
            new_text = _download_via_api(repo, path, branch)
        except Exception as exc:
            errors.append(f"API: {exc}")

        if new_text is None:
            try:
                new_text = _download_via_raw(repo, path, branch)
            except Exception as exc:
                errors.append(f"RAW: {exc}")

        if new_text is not None:
            # Validación mínima antes de reemplazar cache.
            compile(new_text, f"{repo}/{path}", "exec")
            local.write_text(new_text, encoding="utf-8")
        elif old_text is None:
            raise RuntimeError(" | ".join(errors))
        # Si había cache, seguimos con ella aunque GitHub esté temporalmente caído.

    module_name = f"remote_{Path(path).stem}_{digest}"
    if module_name in sys.modules:
        del sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, local)
    if not spec or not spec.loader:
        raise RuntimeError(f"No se pudo cargar {repo}/{path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
