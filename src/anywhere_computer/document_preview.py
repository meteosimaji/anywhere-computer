"""Bounded, hash-bound OOXML page rendering through isolated local tools."""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import JsonValue

from .documents import WORD, OfficePackage
from .files import absolute_path, read_bytes, sha256
from .models import PreviewDocument
from .renderer_location import find_renderer

MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20
OFFICE_FORMATS = {
    ".docx": ("word/document.xml", "writer_pdf_Export"),
    ".xlsx": ("xl/workbook.xml", "calc_pdf_Export"),
    ".pptx": ("ppt/presentation.xml", "impress_pdf_Export"),
}


class DocumentPreviewUnavailable(ValueError):
    """The platform or required renderer is unavailable before rendering begins."""


def _sandbox_policy(root: Path) -> str:
    return ('(version 1)\n(allow default)\n(deny network*)\n'
            f'(deny file-write* (require-not (subpath {json.dumps(str(root.resolve()))})))\n')


def _renderer(name: str) -> str:
    path = find_renderer(name)
    if path is None:
        raise DocumentPreviewUnavailable(
            f"Document preview unavailable: {name} is unavailable")
    return path


def _safe_ooxml(content: bytes, extension: str) -> bytes:
    package = OfficePackage(content)
    try:
        if package.main_part() != OFFICE_FORMATS[extension][0]:
            raise ValueError("Document preview requires a standard OOXML main part")
        replacement: dict[str, bytes] = {}
        for item in package.archive.infolist():
            name = item.filename.lower()
            if (item.filename.startswith("/") or "\\" in item.filename
                    or ".." in item.filename.split("/")):
                raise ValueError("Document preview rejects unsafe package paths")
            if (name.endswith((".bin", ".svg")) or "/embeddings/" in name
                    or "/activex/" in name or name.startswith("xl/externallinks/")
                    or name.startswith("xl/querytables/") or name == "xl/connections.xml"):
                raise ValueError("Document preview rejects active or embedded content")
            if item.filename.endswith(".rels"):
                root = package.xml(item.filename)
                for relation in root:
                    if relation.get("TargetMode", "").lower() == "external":
                        raise ValueError("Document preview rejects external relationships")
            if name.startswith("word/") and name.endswith(".xml"):
                root = package.xml(item.filename)
                if any(node.tag in (WORD + "instrText", WORD + "fldSimple")
                       for node in root.iter()):
                    raise ValueError("Document preview rejects fields that may load external data")
            if extension == ".xlsx" and name.startswith("xl/") and name.endswith(".xml"):
                root = package.xml(item.filename)
                changed = False
                for parent in root.iter():
                    for child in list(parent):
                        local_name = child.tag.rsplit("}", 1)[-1].lower()
                        if (local_name == "f" or "formula" in local_name
                                or local_name == "definedname"):
                            parent.remove(child)
                            changed = True
                if changed:
                    replacement[item.filename] = ET.tostring(root, encoding="utf-8")
        if not replacement:
            return content
        rendered = io.BytesIO()
        with zipfile.ZipFile(rendered, "w") as archive:
            for item in package.archive.infolist():
                archive.writestr(item, replacement.get(item.filename,
                                                       package.archive.read(item)))
        return rendered.getvalue()
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise ValueError("Document preview requires a valid OOXML package") from error
    finally:
        package.archive.close()


_LIMIT_EXEC = (
    "import os,resource,sys; "
    "n=int(sys.argv[1]); resource.setrlimit(resource.RLIMIT_FSIZE,(n,n)); "
    "os.execv(sys.argv[2],sys.argv[2:])"
)


def _run(command: list[str], *, timeout: int, env: dict[str, str],
         max_file_bytes: int | None = None, capture: bool = False) -> bytes:
    launched = ([sys.executable, "-I", "-c", _LIMIT_EXEC, str(max_file_bytes), *command]
                if max_file_bytes is not None else command)
    process = subprocess.Popen(
        launched, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            # On POSIX, a negative PID targets the dedicated process group.
            # Windows never renders, but keep this module type-checkable there.
            if sys.platform == "darwin":
                os.kill(-process.pid, 9)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()
        raise ValueError("Document preview renderer timed out") from error
    if process.returncode:
        raise ValueError("Document preview renderer failed")
    return output or b""


def preview_document(args: PreviewDocument) -> dict[str, JsonValue]:
    path = absolute_path(args.path)
    extension = path.suffix.lower()
    if extension not in OFFICE_FORMATS:
        raise ValueError("Rendered preview supports DOCX, XLSX and PPTX")
    content = read_bytes(path)
    digest = sha256(content)
    if digest != args.expected_sha256:
        raise ValueError("Document changed; read it again")
    render_content = _safe_ooxml(content, extension)
    if sys.platform != "darwin":
        raise DocumentPreviewUnavailable(
            "Document preview currently requires the macOS network sandbox")
    office, pdfinfo, raster = (_renderer(name) for name in ("soffice", "pdfinfo", "pdftoppm"))
    sandbox = _renderer("sandbox-exec")
    with tempfile.TemporaryDirectory(prefix="anywhere-doc-preview-") as root_name:
        root = Path(root_name)
        source = root / ("source" + extension)
        source.write_bytes(render_content)
        output = root / "output"
        output.mkdir(mode=0o700)
        profile = root / "profile"
        profile.mkdir(mode=0o700)
        policy = root / "network.sb"
        policy.write_text(_sandbox_policy(root))
        env = {**os.environ, "HOME": str(root), "TMPDIR": str(root),
               "SAL_DISABLE_OPENCL": "1"}
        _run([sandbox, "-f", str(policy), office, f"-env:UserInstallation={profile.as_uri()}",
              "--headless", "--convert-to", "pdf:" + OFFICE_FORMATS[extension][1], "--outdir",
              str(output), str(source)], timeout=35, env=env,
             max_file_bytes=MAX_PDF_BYTES)
        pdf = output / "source.pdf"
        if not pdf.is_file() or not 0 < pdf.stat().st_size <= MAX_PDF_BYTES:
            raise ValueError("Document preview PDF is missing or exceeds its size limit")
        try:
            info = _run([sandbox, "-f", str(policy), pdfinfo, str(pdf)],
                        timeout=5, env=env, capture=True).decode("utf-8", errors="replace")
        except ValueError as error:
            raise ValueError("Document preview PDF could not be inspected") from error
        match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
        if match is None:
            raise ValueError("Document preview PDF has no page count")
        pages = int(match[1])
        if not 1 <= pages <= MAX_PAGES:
            raise ValueError("Document preview page limit exceeded")
        if args.page > pages:
            raise ValueError("Requested document preview page does not exist")
        image = root / "page"
        _run([sandbox, "-f", str(policy), raster,
              "-f", str(args.page), "-l", str(args.page), "-singlefile",
              "-scale-to", "1400", "-png", str(pdf), str(image)], timeout=15, env=env,
             max_file_bytes=MAX_PNG_BYTES)
        png = image.with_suffix(".png")
        if not png.is_file() or not 0 < png.stat().st_size <= MAX_PNG_BYTES:
            raise ValueError("Document preview image is missing or exceeds its size limit")
        data = png.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Document preview renderer returned an invalid image")
    if sha256(read_bytes(path)) != digest:
        raise ValueError("Document changed during preview; read it again")
    return {"path": str(path), "sha256": digest, "format": extension[1:], "rendered": True,
            "page": args.page, "pages": pages, "mime_type": "image/png",
            "data_base64": base64.b64encode(data).decode("ascii")}
