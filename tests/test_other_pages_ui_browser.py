"""Render the workspace and management pages in real Chrome and check usable layout.

The workspace runs against a real Engine and MCP session over a temporary directory,
and these tests drive it with real mouse and keyboard input (the host iframe has a
fixed height: automated clicks into a sandboxed iframe that is being resized by the
host were dropped in headless Chrome, which is a test-host artefact, not a product
fix). The management page runs its real script against simulated Tauri replies; those
replies prove the page's rendering and state handling, not the native service.
"""

import base64
import json
import os
import struct
import sys
import zipfile
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from anywhere_computer.engine import Engine
from anywhere_computer.http_service import HTTPServiceConfig, save_http_config
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import Reply, Request
from anywhere_computer.setup_connector import SetupConnector
from anywhere_computer.workspace_ui import UI_EXTENSION, UI_MIME, workspace_resource

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_TOOLS = frozenset({
    "workspace_open", "files_info", "files_read", "files_write", "files_read_binary",
    "directories_list", "documents_read", "documents_preview", "settings_get",
    "settings_update", "operations_get",
})
LONG_NAME = "quarterly-business-plan-2026-q3-final-reviewed-by-everyone-v12.final.xlsx"
HOST = """<!doctype html><meta charset="utf-8"><title>host</title>
<style>html,body{margin:0;height:100%}iframe{display:block;width:100%;height:100vh;border:0}</style>
<iframe title="workspace" sandbox="allow-scripts" src="/workspace"></iframe>
<script>
const params=new URLSearchParams(location.search);
const frame=document.querySelector('iframe');
const send=packet=>frame.contentWindow.postMessage({jsonrpc:'2.0',...packet},'*');
const call=async packet=>(await fetch('/rpc',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(packet)})).json();
window.addEventListener('message',async event=>{
  if(event.source!==frame.contentWindow||event.data?.jsonrpc!=='2.0') return;
  const packet=event.data;
  if(packet.method==='ui/initialize') send({id:packet.id,result:{protocolVersion:'2026-01-26',
    hostCapabilities:{serverTools:{}},hostContext:{theme:params.get('theme')}}});
  else if(packet.method==='ui/notifications/initialized') {
    const opened=await call({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'workspace_open',
      arguments:{path:params.get('path'),view:params.get('view')}}});
    send({method:'ui/notifications/tool-result',params:opened.result});
  } else if(packet.method==='tools/call') send(await call(packet));
});
</script>"""
# Contrast of the rendered text against the nearest opaque background.
CONTRAST = """selector=>{
  const lum=c=>{const [r,g,b]=c.match(/[\\d.]+/g).slice(0,3).map(Number).map(v=>{v/=255;
    return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});return .2126*r+.7152*g+.0722*b};
  const background=e=>{for(;e;e=e.parentElement){const c=getComputedStyle(e).backgroundColor;
    if(!/, 0\\)$/.test(c)&&c!=='transparent') return c;} return 'rgb(255,255,255)';};
  const e=document.querySelector(selector);
  const a=lum(getComputedStyle(e).color),b=lum(background(e));
  return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
}"""
NO_SIDEWAYS = "document.documentElement.scrollWidth<=document.documentElement.clientWidth"
BACKGROUND = "s=>getComputedStyle(document.querySelector(s)).backgroundColor"


def png_bytes(width=320, height=200):
    """A visibly non-blank test picture (gradient and a disc), generated for verification."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            red, green, blue = 40 + x * 200 // width, 90 + y * 120 // height, 200 - x * 120 // width
            if (x - 220) ** 2 + (y - 70) ** 2 < 900:
                red, green, blue = 250, 210, 70
            rows += bytes((red, green, blue))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def docx_bytes(*, long_text=None):
    """A small Word package with headings, a table and one long paragraph."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def paragraph(text):
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    cells = [
        ["項目", "金額", "備考"],
        ["家賃", "120,000円", "毎月末払い"],
        ["通信費", "8,800円", "年払い"],
    ]
    table = "<w:tbl>" + "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in cells) + "</w:tbl>"
    if long_text is None:
        long_text = "長い段落の本文です。" * 40
    body = paragraph("第3四半期 事業計画書") + paragraph(long_text) + table + paragraph("以上")
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
        "word/document.xml": f"<w:document {ns}><w:body>{body}</w:body></w:document>",
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, text in parts.items():
            package.writestr(name, text)
    return buffer.getvalue()


async def launch(driver, *, headless=True):
    if not headless and sys.platform == "linux" and not os.environ.get("DISPLAY"):
        pytest.skip("This real pointer check needs a Chrome window and a Linux display")
    try:
        return await driver.chromium.launch(channel="chrome", headless=headless)
    except PlaywrightError as error:
        if "is not found at" in str(error) or "Executable doesn't exist" in str(error):
            pytest.skip(f"Chrome executable is not installed: {error}")
        raise


@pytest.fixture
async def backend(tmp_path):
    files = tmp_path / "files"
    folder = files / "project"
    (folder / "docs").mkdir(parents=True)
    (folder / "notes.md").write_text("# メモ\n\n本文\n", encoding="utf-8")
    (folder / LONG_NAME).write_text("x\n", encoding="utf-8")
    # Keep the UTF-8 component below Linux's 255-byte limit while exercising wrapping.
    (folder / ("とても長い名前のファイル_" * 6 + ".txt")).write_text("x\n", encoding="utf-8")
    (folder / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (folder / "photo.png").write_bytes(png_bytes())
    (folder / "report.docx").write_bytes(docx_bytes())
    engine = Engine(tmp_path / "engine")

    async def catalog():
        return engine.catalog(FIXTURE_TOOLS)

    async def execute(request: Request) -> Reply:
        path = request.arguments.get("path")
        if request.tool not in FIXTURE_TOOLS or (
                path is not None and not Path(path).resolve().is_relative_to(files.resolve())):
            raise ValueError("Outside this disposable fixture")
        return await engine.execute(request)

    setup = SetupConnector(tmp_path / "connector", catalog, execute)
    session = MCPSession(setup.catalog, setup.execute)
    await session.handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "clientInfo": {"name": "ui-browser", "version": "1"},
        "capabilities": {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}}}}})
    await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    try:
        yield folder, session
    finally:
        setup.close()
        await engine.close()


async def open_workspace(browser, session, folder, *, width, height=860, scheme="light",
                         view="files"):
    # Reduced motion removes colour transitions so computed colours are final values.
    context = await browser.new_context(viewport={"width": width, "height": height},
                                        color_scheme=scheme, reduced_motion="reduce")
    page = await context.new_page()
    page.rpc_replies = []

    async def route(handler):
        url = handler.request.url
        try:
            if url.endswith("/workspace"):
                await handler.fulfill(content_type="text/html; charset=utf-8",
                                      body=str(workspace_resource()["text"]))
            elif url.endswith("/rpc"):
                packet = json.loads(handler.request.post_data)
                reply = await session.handle(packet)
                page.rpc_replies.append((packet, reply))
                await handler.fulfill(content_type="application/json", body=json.dumps(reply))
            else:
                await handler.fulfill(content_type="text/html; charset=utf-8", body=HOST)
        except PlaywrightError:
            pass  # the test closed the page while a late background request was in flight

    # An HTTPS origin gives the page a secure context for crypto.randomUUID.
    await page.route("https://host.test/**", route)
    await page.goto(f"https://host.test/?path={folder}&view={view}&theme={scheme}")
    frame = page.frame_locator("iframe")
    await frame.locator("#connection[data-state=ready]").wait_for(timeout=10000)
    # Interact only once the host's first open request has finished rendering.
    await frame.locator("#notice:not(:has-text('ホストとの接続を待っています'))").wait_for(
        timeout=10000)
    return context, page, frame


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 390, 768])
async def test_folder_list_fits_shows_whole_names_and_stays_readable(backend, width, scheme):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, scheme=scheme)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            inner = page.frames[-1]
            assert await inner.evaluate(NO_SIDEWAYS), "no sideways scrolling with long names"
            assert await inner.evaluate("document.documentElement.dataset.theme") == scheme
            rows = await frame.locator("#entries button").evaluate_all(
                "b=>b.map(e=>[e.getBoundingClientRect().height,e.getBoundingClientRect().right])")
            assert len(rows) == 7 and all(height >= 36 for height, _ in rows)
            assert all(right <= width for _, right in rows)
            # List rows are not permanently filled secondary buttons.
            first = frame.locator("#entries button").first
            background = "e=>getComputedStyle(e).backgroundColor"
            assert await first.evaluate(background) == "rgba(0, 0, 0, 0)"
            await first.hover()
            assert await first.evaluate(background) != "rgba(0, 0, 0, 0)"
            assert await frame.locator(".file-columns").is_visible()
            # Touch users have no hover title, so the version and extension must not be
            # cut off: names wrap instead of being truncated.
            names = await frame.locator(".file-name").evaluate_all(
                "n=>n.map(e=>[e.textContent,e.scrollWidth<=e.clientWidth+1,"
                "getComputedStyle(e).textOverflow,getComputedStyle(e).whiteSpace])")
            assert LONG_NAME in [text for text, *_ in names]
            for text, fits, overflow, white_space in names:
                assert fits and overflow == "clip" and white_space == "normal", text
            for selector in ("#folder-count", ".kind", ".file-name", "#notice"):
                assert await inner.evaluate(CONTRAST, selector) >= 4.5, selector
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [320, 1000])
async def test_file_kind_handles_extensionless_names_and_long_extensions(backend, width):
    folder, session = backend
    examples = {
        "n" * 100: "ファイル",
        "draft." + "suffix" * 20: ("suffix" * 20).upper() + " ファイル",
    }
    for name in examples:
        (folder / name).write_text("fixture\n", encoding="utf-8")
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=width)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            assert await page.frames[-1].evaluate(NO_SIDEWAYS)
            for name, kind in examples.items():
                row = frame.locator("#entries button").filter(has_text=name)
                assert await row.locator(".kind").inner_text() == kind
                assert await row.locator(".kind").evaluate(
                    "e=>e.scrollWidth<=e.clientWidth+1"), name
                assert await row.locator(".file-name").inner_text() == name
                assert await row.locator(".file-name").evaluate(
                    "e=>e.scrollWidth<=e.clientWidth+1"), name
            await context.close()
        finally:
            await browser.close()


async def test_keyboard_reaches_rows_and_focus_is_visible(backend):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=390)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            inner = page.frames[-1]
            await frame.locator("#path").focus()
            await page.keyboard.press("Tab")  # 開く
            await page.keyboard.press("Tab")  # first file row
            focus = await inner.evaluate(
                "(()=>{const s=getComputedStyle(document.activeElement);"
                "return [document.activeElement.parentElement.parentElement.id,"
                "parseFloat(s.outlineWidth),s.outlineStyle]})()")
            assert focus[0] == "entries" and focus[1] >= 2 and focus[2] == "solid"
            await context.close()
        finally:
            await browser.close()


async def test_open_edit_save_and_settings_with_real_pointer_and_keyboard(backend):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=1000)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            await frame.locator("#entries button", has_text="notes.md").click()
            await frame.locator("#editor:not([readonly])").wait_for(timeout=10000)
            assert "本文" in await frame.locator("#editor").input_value()
            inner = page.frames[-1]
            accent = await inner.evaluate(BACKGROUND, "#open")  # enabled primary
            # A disabled primary action reads as unavailable, not as a faded accent.
            assert await frame.locator("#save").is_disabled()
            assert await inner.evaluate(BACKGROUND, "#save") != accent
            await frame.locator("#editor").click()
            await page.keyboard.type("追記")
            await frame.locator("#save:enabled").wait_for(timeout=10000)
            assert await inner.evaluate(BACKGROUND, "#save") == accent
            await frame.locator("#save").click()
            await frame.locator("#notice:has-text('保存しました')").wait_for(timeout=10000)
            await frame.locator("#save:disabled").wait_for(timeout=10000)
            assert "追記" in (folder / "notes.md").read_text(encoding="utf-8")
            # Settings: edit a limit with the keyboard and apply it with the pointer.
            await frame.locator("#settings-tab").click()
            await frame.locator("#read-limit").wait_for(timeout=10000)
            await frame.locator("#read-limit").click()
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.type("123")
            await frame.locator("#set-read").click()
            await frame.locator("#notice:has-text('設定を適用しました')").wait_for(timeout=10000)
            await frame.locator("#files-tab").click()
            await frame.locator("#settings-tab").click()
            await frame.locator("#notice:has-text('共有設定を読み込みました')").wait_for(
                timeout=10000)
            assert await frame.locator("#read-limit").input_value() == "123"
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize(("width", "height", "keyboard", "headless"), [
    (390, 800, False, True), (1000, 700, True, True), (1000, 700, False, False),
])
async def test_connection_settings_review_and_save_with_real_input(
    backend, width, height, keyboard, headless,
):
    folder, session = backend
    long_host = ".".join(["a" * 50, "b" * 50, "c" * 50, "example.com"])
    async with async_playwright() as driver:
        browser = await launch(driver, headless=headless)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, height=height, view="connection")
            inner = page.frames[-1]
            await frame.locator("#notice:has-text('接続設定です')").wait_for(timeout=10000)
            await frame.locator("#setup-resource").click()
            await page.keyboard.type(f"https://{long_host}/mcp")
            # The native <select> popup cannot be driven by automation, so choose by value.
            await frame.locator("#setup-mode").select_option("files")
            # Chrome154 headless drops pointer delivery into this opaque-origin,
            # scrollable iframe at 1000x700 before the child receives any event.
            # Keep the sandbox: verify that size with a real Chrome-window pointer
            # and independently with real keyboard input in headless Chrome.
            if keyboard:
                await frame.locator("#setup-plan").focus()
                await page.keyboard.press("Enter")
            else:
                await frame.locator("#setup-plan").click()
            await frame.locator("#setup-review:visible").wait_for(timeout=10000)
            assert await inner.evaluate(NO_SIDEWAYS), "a long URL must wrap, not scroll sideways"
            # With a short window the save button is still reachable by keyboard and scrolls
            # into view.
            await frame.locator("#setup-confirm").focus()
            box = await frame.locator("#setup-confirm").bounding_box()
            assert box is not None and 0 <= box["y"] and box["y"] + box["height"] <= height
            await frame.locator("#setup-confirm").click()
            await frame.locator("#setup-status:has-text('保存済み')").wait_for(timeout=10000)
            status = await frame.locator("#setup-status").text_content()
            assert "認証・起動・接続確認は、まだこの画面では行っていません" in status
            assert not await frame.locator("#setup-form").is_visible()
            assert await inner.evaluate(NO_SIDEWAYS)
            saved = await session.handle({"jsonrpc": "2.0", "id": "status", "method": "tools/call",
                                          "params": {"name": "connection_setup_status",
                                                     "arguments": {}}})
            data = saved["result"]["structuredContent"]["data"]
            assert data["phase"] == "configured" and long_host in data["configuration"]["resource"]
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("refresh", ["status", "same-target-notification"])
async def test_connection_draft_survives_refresh_in_real_chrome(backend, refresh):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=390, view="connection")
            resource = frame.locator("#setup-resource")
            await resource.fill("https://reviewed.example/mcp")
            await frame.locator("#setup-mode").select_option("files")
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            await resource.fill("https://unsaved.example/mcp")
            async with page.expect_response(lambda response: response.url.endswith("/rpc")):
                if refresh == "status":
                    await frame.locator("#setup-reload").click()
                else:
                    opened = await session.handle({
                        "jsonrpc": "2.0", "id": "same-view", "method": "tools/call",
                        "params": {"name": "workspace_open", "arguments": {"view": "connection"}},
                    })
                    await page.evaluate(
                        "result=>send({method:'ui/notifications/tool-result',params:result})",
                        opened["result"])
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await expect(resource).to_have_value("https://unsaved.example/mcp")
            await expect(frame.locator("#setup-review")).to_be_hidden()
            await expect(frame.locator("#setup-confirm")).to_be_disabled()
            await frame.locator("#files-tab").click()
            await expect(frame.locator("#discard")).to_be_visible()
            await frame.locator("#keep").click()
            await expect(resource).to_have_value("https://unsaved.example/mcp")
            assert not any(packet["params"]["name"] == "connection_setup_confirm"
                           for packet, _ in page.rpc_replies)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1000])
async def test_connection_confirm_conflict_keeps_reviewed_form_in_real_chrome(backend, width):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, height=860, view="connection")
            resource = frame.locator("#setup-resource")
            await resource.fill("https://reviewed.example/mcp")
            await frame.locator("#setup-mode").select_option("files")
            await frame.locator("#setup-plan").focus()
            await page.keyboard.press("Enter")
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            status = await session.handle({"jsonrpc": "2.0", "id": "review",
                                           "method": "tools/call", "params": {
                                               "name": "connection_setup_status", "arguments": {}}})
            reviewed = status["result"]["structuredContent"]["data"]["configuration"]
            saved = HTTPServiceConfig.model_validate_json(json.dumps({
                **reviewed, "resource": "https://other.example/mcp",
            }))
            # Another trusted local writer publishes first; the controller must refuse overwrite.
            await save_http_config(folder.parents[1] / "connector", saved)
            await frame.locator("#setup-confirm").focus()
            await page.keyboard.press("Enter")
            await expect(frame.locator("#setup-status")).to_contain_text("別の設定が保存")
            await expect(resource).to_have_value("https://reviewed.example/mcp")
            await expect(resource).to_be_visible()
            await expect(resource).to_be_enabled()
            assert await resource.get_attribute("readonly") is not None
            await expect(frame.locator("#setup-summary")).to_contain_text("https://other.example/mcp")
            await expect(frame.locator("#setup-status")).to_contain_text(
                "確認していた内容を入力欄に残しています")
            await expect(frame.locator("#setup-confirm")).to_be_disabled()
            await frame.locator("#setup-reload").click()
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await expect(resource).to_have_value("https://reviewed.example/mcp")
            assert await page.frames[-1].evaluate(NO_SIDEWAYS)
            await frame.locator("#files-tab").click()
            await expect(frame.locator("#discard")).to_be_visible()
            await frame.locator("#discard-go").click()
            assert sum(packet["params"]["name"] == "connection_setup_confirm"
                       for packet, _ in page.rpc_replies) == 1
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("preview_mode", ["installed", "unavailable"])
async def test_image_and_document_are_really_read_and_displayed(backend, monkeypatch, preview_mode):
    if preview_mode == "unavailable":
        from anywhere_computer import engine as engine_module
        from anywhere_computer.document_preview import DocumentPreviewUnavailable

        def unavailable_renderer(_request):
            raise DocumentPreviewUnavailable("Fixture adapter is unavailable")

        # Exercise the unsupported-platform outcome even on a Mac with an adapter.
        # Source reads, extraction, MCP transport and browser rendering remain real.
        monkeypatch.setattr(engine_module, "preview_document", unavailable_renderer)
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=768)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            await frame.locator("#entries button", has_text="photo.png").click()
            await frame.locator("#image:visible").wait_for(timeout=10000)
            image = await frame.locator("#image").evaluate(
                "e=>e.decode().then(()=>[e.complete,e.naturalWidth,e.naturalHeight,e.src])")
            assert image[0] and image[1] == 320 and image[2] == 200 and image[3].startswith("blob:")
            await frame.locator("#back").click()
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            await frame.locator("#entries button", has_text="report.docx").click()
            await frame.locator("#document:not(:empty)").wait_for(timeout=10000)
            text = await frame.locator("#document").text_content()
            assert "第3四半期 事業計画書" in text and "長い段落の本文です。" * 40 in text
            assert "[表1 2行 2列] 120,000円" in text
            inner = page.frames[-1]
            assert await inner.evaluate(NO_SIDEWAYS)
            await frame.locator("#files-tab:enabled").wait_for(timeout=10000)
            assert await frame.locator("#document-title:visible").text_content() == "抽出した本文"
            # The real renderer's availability varies by platform. Require the UI to
            # match the actual MCP outcome, rather than assuming macOS's failure text.
            previews = [reply["result"]["structuredContent"]
                        for packet, reply in page.rpc_replies
                        if packet.get("method") == "tools/call"
                        and packet.get("params", {}).get("name") == "documents_preview"]
            assert len(previews) == 1
            preview = previews[0]
            if preview_mode == "unavailable":
                assert preview["state"] == "failed"
                assert preview["data"]["error_code"] == "preview_unavailable"
            notice = await frame.locator("#notice").text_content()
            if preview["state"] == "failed":
                if preview["data"].get("error_code") == "preview_unavailable":
                    assert notice == ("書式プレビューはこの端末で利用できません。"
                                      "抽出した文字情報を表示しています。")
                    assert await frame.locator("#notice").get_attribute("data-error") == "false"
                else:
                    assert notice == ("書式プレビューを生成できませんでした。"
                                      "抽出した本文は表示しています。")
                    assert await frame.locator("#notice").get_attribute("data-error") == "true"
                assert not await frame.locator("#image").is_visible()
            else:
                assert preview["state"] == "completed"
                assert await frame.locator("#formatted-preview").is_visible()
                assert "抽出した本文を表示しています" in notice
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [320, 390, 1000])
async def test_document_format_can_open_without_hiding_mobile_body(backend, monkeypatch, width):
    from anywhere_computer import engine as engine_module

    folder, session = backend
    image_data = base64.b64encode(png_bytes(600, 840)).decode("ascii")

    def rendered_page(request):
        # The external renderer is simulated; extraction, MCP image delivery,
        # integrity checks and actual Chrome disclosure interaction remain real.
        return {"path": request.path, "sha256": request.expected_sha256,
                "format": "docx", "rendered": True, "page": request.page, "pages": 1,
                "mime_type": "image/png", "content": [
                    {"type": "image", "mimeType": "image/png", "data": image_data}]}

    monkeypatch.setattr(engine_module, "preview_document", rendered_page)
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=width)
            await frame.locator("#entries button", has_text="report.docx").click()
            await frame.locator("#formatted-preview:visible").wait_for(timeout=10000)
            await frame.locator("#files-tab:enabled").wait_for(timeout=10000)
            assert "第3四半期 事業計画書" in await frame.locator("#document").text_content()
            assert await frame.locator("#document-title").is_visible()
            assert await frame.locator("#formatted-preview").evaluate("e=>e.open") == (width > 580)
            if width <= 580:
                assert not await frame.locator("#image").is_visible()
                body = await frame.locator("#document").bounding_box()
                assert body is not None and 0 <= body["y"] < 860
                await frame.locator("#formatted-preview summary").click()
            dimensions = await frame.locator("#image").evaluate(
                "e=>e.decode().then(()=>[e.naturalWidth,e.naturalHeight])")
            assert dimensions == [600, 840]
            assert await frame.locator("#image").is_visible()
            await frame.locator("#back").click()
            await frame.locator("#entries button", has_text="photo.png").click()
            await frame.locator("#image:visible").wait_for(timeout=10000)
            assert await frame.locator("#image-preview").is_visible()
            assert not await frame.locator("#formatted-preview").is_visible()
            assert not await frame.locator("#document-title").is_visible()
            assert await frame.locator("#image").get_attribute("alt") == "選択した画像のプレビュー"
            assert await page.frames[-1].evaluate(NO_SIDEWAYS)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [320, 390])
async def test_mobile_folder_uses_page_scroll_and_can_open_its_last_entry(backend, width):
    folder, session = backend
    for index in range(20):
        (folder / f"z-{index:02}.txt").write_text(f"本文 {index}\n", encoding="utf-8")
    (folder / "zz-last.txt").write_text("一覧の最後の本文\n", encoding="utf-8")
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=width)
            await frame.locator("#entries li").first.wait_for(timeout=10000)
            assert await frame.locator("#entries li").count() == 28
            # A long mobile list should grow with the page, rather than cutting off
            # after an unmarked, separately scrollable 640px region.
            assert await frame.locator("#entries").evaluate(
                "e=>e.scrollHeight<=e.clientHeight+1")
            await frame.locator("#entries button", has_text="zz-last.txt").click()
            await frame.locator("#files-tab:enabled").wait_for(timeout=10000)
            assert await frame.locator("#editor").input_value() == "一覧の最後の本文\n"
            assert await page.frames[-1].evaluate(NO_SIDEWAYS)
            await context.close()
        finally:
            await browser.close()


async def test_document_truncation_and_render_failure_keep_their_distinct_outcomes(
    backend, monkeypatch,
):
    from anywhere_computer import engine as engine_module

    folder, session = backend
    # The real documents_read path truncates this entry at its existing 32768-character cap.
    (folder / "report.docx").write_bytes(docx_bytes(long_text="長" * 32778))

    def failed_renderer(_request):
        raise ValueError("Fixture renderer failure")

    # Only the external renderer failure is simulated; extraction and MCP replies are real.
    monkeypatch.setattr(engine_module, "preview_document", failed_renderer)
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(browser, session, folder, width=390)
            await frame.locator("#entries button", has_text="report.docx").click()
            await frame.locator("#files-tab:enabled").wait_for(timeout=10000)
            text = await frame.locator("#document").text_content()
            assert "長" * 32768 in text and "長" * 32769 not in text
            assert "この項目の本文は途中までです" in text
            notice = await frame.locator("#notice").text_content()
            assert "書式プレビューを生成できませんでした" in notice
            assert "抽出した本文は表示しています" in notice
            assert "Operation failed" not in notice
            assert "操作結果を照会" not in notice, "No recovery control exists for this read"
            assert await frame.locator("#notice").get_attribute("data-error") == "true"
            assert not await frame.locator("#image").is_visible()
            assert await page.frames[-1].evaluate(NO_SIDEWAYS)
            await context.close()
        finally:
            await browser.close()


def management_fixture(authorization, *, registration=None, engine_state="ready", **extra):
    snapshot = {
        "schema_version": 1, "engine_state": engine_state, "observed_at": "2026-10-01T09:00:00Z",
        "setup": {"phase": "configured", "configuration": {"client": "anywhere-chatgpt"}},
        "capabilities": {"files": True}, "active_resources": {"terminal_sessions": 0},
        "devices": [{"device_id": "a" * 32, "name": "仕事用Windows（経理部の共有端末・3階）",
                     "last_observed_state": "unreachable", "checked_at": None}],
        "device_registry_state": "read", "automatic_stable_updates": False,
        "version": "1", "runtime_id": "r", "instance_id": "i",
        "source_build": {"version": "1", "runtime_id": "r"},
        "runtime_comparison": {"version_matches": True, "runtime_id_matches": True},
    }
    return {
        "snapshot": snapshot,
        "startup": {"schema_version": 1, "state": "not_installed", "mode": "local",
                    "observed_at": snapshot["observed_at"]},
        "enrollment": {"schema_version": 1, "authorization": authorization,
                       "registration": registration, "connection_state": "not_checked", **extra},
    }


INIT = """window.nativeCalls=[];window.__TAURI__={core:{invoke:async(command,args)=>{
  window.nativeCalls.push({command,args});
  const f=%s;
  if(command==='management_snapshot') return JSON.stringify(f.snapshot);
  if(command==='management_startup_status') return JSON.stringify(f.startup);
  if(command==='management_enrollment') return JSON.stringify(f.enrollment);
  throw new Error('unexpected '+command);
}}};"""


async def open_management(browser, fixture, *, width, height, scheme):
    context = await browser.new_context(viewport={"width": width, "height": height},
                                        color_scheme=scheme, reduced_motion="reduce")
    page = await context.new_page()
    await page.add_init_script(INIT % json.dumps(fixture))
    types = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript"}

    async def route(handler):
        name = handler.request.url.rsplit("/", 1)[-1] or "index.html"
        source = ROOT / "desktop/ui" / name
        await handler.fulfill(content_type=types[source.suffix], body=source.read_bytes())

    await page.route("https://app.test/**", route)
    await page.goto("https://app.test/index.html")
    await page.locator("#status:has-text('状態を更新しました')").wait_for(timeout=10000)
    await page.click("#enrollment-progress")
    await page.locator("#enrollment-state:not(:has-text('未確認'))").wait_for(timeout=10000)
    return context, page


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", [(800, 600), (1280, 700)])
async def test_management_page_fits_and_emphasises_only_the_available_recovery_steps(
        size, scheme):
    fixture = management_fixture(
        {"phase": "credential_error", "can_retry_save": True},
        registration={"name": "自宅のMac mini", "device": None}, can_reauthorize=True,
        can_cleanup=False)
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page = await open_management(
                browser, fixture, width=size[0], height=size[1], scheme=scheme)
            assert await page.evaluate(NO_SIDEWAYS)
            for name in ("start", "poll", "register"):
                assert not await page.locator(f"#enrollment-{name}").is_visible()
                assert await page.locator(f"#enrollment-{name}").is_disabled()
            # Save retry is primary; reauthorization is a distinct alternative.
            accent = await page.evaluate(BACKGROUND, "#enrollment-retry_save")
            assert accent != await page.evaluate(BACKGROUND, "#enrollment-reauthorize")
            assert await page.locator(".enrollment-path").is_visible()
            assert await page.locator("#enrollment-recovery-help").is_visible()
            assert await page.evaluate(BACKGROUND, "#start") != accent
            assert await page.locator("#enrollment-reauthorize").is_visible()
            assert await page.locator("#enrollment-retry_save").is_visible()
            assert not await page.locator("#enrollment-cleanup").is_visible()
            assert await page.locator("#enrollment-cancel").is_visible()
            assert await page.evaluate(BACKGROUND, "#enrollment-cancel") != accent
            assert await page.evaluate(BACKGROUND, "#startup-enable") == await page.evaluate(
                BACKGROUND, ".column:first-child")
            assert not await page.locator("#startup-disable").is_visible()
            if size[0] < 900:
                gap = await page.evaluate("""() => {
                  const columns=document.querySelectorAll('.column');
                  return columns[1].querySelector('h2').getBoundingClientRect().top
                    -columns[0].getBoundingClientRect().bottom;
                }""")
                assert gap >= 24
            assert await page.locator("#enrollment-name").is_disabled()
            assert await page.locator("#enrollment-name").input_value() == "自宅のMac mini"
            state = await page.locator("#enrollment-state").text_content()
            assert "登録要求は保持されています" in state and "認証情報を利用できません" in state
            assert "AIから操作できません" in await page.locator("main").text_content()
            for selector in ("#status", ".muted", "summary", "#engine"):
                ratio = await page.evaluate(CONTRAST, selector)
                assert ratio >= 4.5, (selector, ratio)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1280])
async def test_waiting_enrollment_opens_native_page_and_copies_code_in_order(width):
    fixture = management_fixture({"phase": "waiting", "user_code": "TEST-CODE",
                                  "verification_uri": "https://auth.example/verify"})
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page = await open_management(
                browser, fixture, width=width, height=800, scheme="light")
            assert await page.evaluate(NO_SIDEWAYS)
            accent = await page.evaluate(BACKGROUND, "#enrollment-open_page")
            assert accent != await page.evaluate(BACKGROUND, "#enrollment-poll")
            assert await page.locator("#enrollment-open_page").is_enabled()
            await page.locator("#enrollment-open_page").click()
            await page.locator("#enrollment-result").get_by_text(
                "ブラウザーへ認証ページを開く要求を送りました。本人認証を終えたら、結果を確認してください。",
                exact=True).wait_for()
            calls = await page.evaluate("window.nativeCalls")
            assert calls[-1] == {"command": "management_enrollment",
                                 "args": {"method": "open_page", "name": None}}
            # Simulate Clipboard permission outcomes; never modify the owner's OS clipboard.
            await page.evaluate("""() => {
              window.copied=[];
              Object.defineProperty(navigator, 'clipboard', {configurable:true,
                value:{writeText:async text=>window.copied.push(text)}});
            }""")
            await page.locator("#enrollment-copy-code").click()
            assert await page.evaluate("window.copied") == ["TEST-CODE"]
            assert await page.evaluate("window.nativeCalls") == calls
            assert await page.locator("#enrollment-result").inner_text() == (
                "コードをコピーしました。")
            await page.evaluate("""() => {
              navigator.clipboard.writeText=async()=>{throw new Error('permission denied');};
            }""")
            await page.locator("#enrollment-copy-code").click()
            assert "手動でコピー" in await page.locator("#enrollment-result").inner_text()
            assert await page.locator("#enrollment-code").evaluate(
                "e=>document.activeElement===e && e.selectionStart===0 && e.selectionEnd===9")
            assert await page.evaluate("window.nativeCalls") == calls
            assert await page.locator("#enrollment-poll").is_enabled()
            await context.close()
        finally:
            await browser.close()


async def test_stopped_engine_makes_the_start_button_the_accent():
    fixture = management_fixture({"phase": "new"}, engine_state="stopped")
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page = await open_management(
                browser, fixture, width=800, height=600, scheme="light")
            await page.locator("#start:enabled").wait_for(timeout=10000)
            await page.locator("h1").hover()
            accent = await page.evaluate(BACKGROUND, "#start")
            assert accent == await page.evaluate(BACKGROUND, "#enrollment-start")  # also enabled
            await context.close()
        finally:
            await browser.close()
