"""Real browser end-to-end tests (Playwright): system Chrome and WebKit (Safari's engine).

Skipped if Playwright or a browser isn't installed:
    uv pip install playwright && .venv/bin/playwright install webkit
"""

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"
SHOTS = Path(os.environ.get("STELE_SCREENSHOTS", Path(tempfile.gettempdir()) / "stele-screenshots"))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    storage = tmp_path_factory.mktemp("browser-data")
    env = os.environ | {"STELE_STORAGE": str(storage), "STELE_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            httpx.get(f"{base}/api/jobs", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    yield base
    proc.kill()
    proc.wait()


@pytest.fixture(scope="module", params=["chrome", "webkit"])
def browser(request):
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome") if request.param == "chrome" else p.webkit.launch()
        except Exception as exc:  # browser not installed on this machine
            pytest.skip(f"{request.param} not available: {exc}")
        yield request.param, b
        b.close()


@pytest.fixture
def page(browser, server):
    name, b = browser
    ctx = b.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status} {r.url}"))
    pg.goto(server)
    pg.wait_for_selector("#mem-text:not(:text('–'))")
    yield name, pg
    ctx.close()
    assert not errors, f"JavaScript errors in {name}: {errors}"


def convert(pg, files, mode="auto", folder=False):
    pg.check(f"input[name=mode][value={mode}]")
    pg.set_input_files("#pick-folder" if folder else "#pick-files", files)
    pg.click("#start")
    pg.wait_for_selector("#job-title:has-text('completata'), #job-title:has-text('non riuscita')", timeout=300_000)


def download(pg, selector="#job-zip") -> tuple[str, zipfile.ZipFile]:
    with pg.expect_download() as dl:
        pg.click(selector)
    path = dl.value.path()
    return dl.value.suggested_filename, zipfile.ZipFile(io.BytesIO(Path(path).read_bytes()))


def test_single_file_fast_mode_and_zip(page, tmp_path):
    name, pg = page
    src = tmp_path / "relazione.pdf"
    shutil.copy(DATA / "digital_chart_it.pdf", src)
    convert(pg, str(src), mode="fast")
    summary = pg.inner_text("#job-summary")
    assert "Modalità Veloce" in summary and "1 documento" in summary and "1 pagina" in summary
    assert pg.get_attribute("#job-zip", "aria-disabled") == "false"
    fname, z = download(pg)
    assert fname == "relazione.zip"
    assert sorted(z.namelist()) == ["relazione/assets/immagine_001.png", "relazione/metadata.json", "relazione/relazione.md"]


def test_multiple_files_with_failure_auto_mode(page, tmp_path):
    name, pg = page
    files = []
    for target, src in [("scansione.pdf", "scan_noisy150_it.pdf"), ("slide.pptx", "office_slides_en.pptx"),
                        ("rotto.pdf", "broken_truncated.pdf")]:
        shutil.copy(DATA / src, tmp_path / target)
        files.append(str(tmp_path / target))
    convert(pg, files, mode="auto")
    assert "2 convertiti" in pg.inner_text("#job-summary") and "1 non convertito" in pg.inner_text("#job-summary")
    assert "README" in pg.inner_text("#job-message")
    assert "PDF illeggibile" in pg.inner_text("#files")
    assert "motore" not in pg.inner_text("#job").lower() and "paddle" not in pg.inner_text("#job").lower()
    fname, z = download(pg)
    assert fname == "conversione.zip" and "README.txt" in z.namelist()
    assert {"scansione/scansione.md", "slide/slide.md"} <= set(z.namelist())


def test_folder_upload_keeps_structure(page, tmp_path):
    name, pg = page
    folder = tmp_path / "Documenti"
    (folder / "sub").mkdir(parents=True)
    shutil.copy(DATA / "office_report_it.docx", folder / "appunti.docx")
    shutil.copy(DATA / "digital_en_twocol.pdf", folder / "sub" / "note.pdf")
    convert(pg, str(folder), mode="advanced", folder=True)
    table = pg.inner_text("#files")
    assert "Documenti/appunti.docx" in table and "Documenti/sub/note.pdf" in table
    assert "Modalità Avanzata" in pg.inner_text("#job-summary")
    _, z = download(pg)
    assert {"Documenti/appunti/appunti.md", "Documenti/sub/note/note.md"} <= set(z.namelist())


def test_all_failed_offers_no_download(page, tmp_path):
    name, pg = page
    src = tmp_path / "rotto.pdf"
    shutil.copy(DATA / "broken_truncated.pdf", src)
    convert(pg, str(src))
    assert pg.inner_text("#job-title") == "Conversione non riuscita"
    assert pg.get_attribute("#job-zip", "aria-disabled") == "true"
    assert "Nessun documento" in pg.inner_text("#job-message")


def test_preview_renders_tables_and_images(page, tmp_path):
    name, pg = page
    src = tmp_path / "grafico.pdf"
    shutil.copy(DATA / "digital_chart_it.pdf", src)
    convert(pg, str(src))
    pg.click("[data-prev]")
    frame = pg.frame_locator("#preview-frame")
    frame.locator("table").first.wait_for()
    assert frame.locator("td", has_text="Biblioteca").count() == 1
    assert pg.frame_locator("#preview-frame").locator("img").evaluate("img => img.complete && img.naturalWidth > 0")
    pg.click("#preview-close")


def test_resource_panel_never_invents_values(page):
    name, pg = page
    gpu = pg.inner_text("#gpu-text")
    assert gpu.endswith("%") or gpu == "Non disponibile"
    detail = pg.inner_text("#mem-detail")
    for label in ("Processo principale", "Processi ausiliari", "Modelli MLX", "Picco", "Budget"):
        assert label in detail


@pytest.mark.parametrize("width,height", [(1440, 1000), (1024, 900), (390, 844)])
def test_layout_has_no_horizontal_overflow(page, width, height):
    name, pg = page
    pg.set_viewport_size({"width": width, "height": height})
    pg.goto(pg.url)
    pg.wait_for_selector("#mem-text:not(:text('–'))")
    pg.click("#history [data-job]") if pg.locator("#history [data-job]").count() else None
    time.sleep(1)
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(SHOTS / f"{name}_{width}.png"), full_page=True)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{name} overflows at {width}px"
