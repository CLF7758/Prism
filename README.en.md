<p align="center"><img src="prism/assets/logo.png" width="112" alt="Prism Logo"></p>
<h1 align="center">Prism</h1>
<p align="center">Reference images, rich-text notes, and mind maps in one local creative workspace.</p>

[简体中文](README.md) · [English](README.en.md)

Prism is a desktop workspace for illustrators, designers, 3D artists, and other creators. Collect visual references, arrange them on a freeform canvas, document your ideas, and organize their structure with mind maps. Projects are saved locally as `.prism` files.

This project extends upstream code by Rebecca Breu. Original copyright and GPL notices are retained. See [NOTICE](NOTICE) and [THIRD_PARTY.md](THIRD_PARTY.md) for attribution and dependency licensing.

## Screenshots

These are actual screenshots rendered by the current application with synthetic demo content. They contain no private user projects or third-party artwork.

### Reference canvas and inspector

![Reference canvas](docs/images/canvas.png)

Arrange references freely and inspect titles, notes, ratings, tags, and file information.

### Rich-text documents

![Document editor](docs/images/document.png)

Capture your creative direction, specifications, and checklists in a page-style document within the same project.

### Mind maps

![Mind map](docs/images/mindmap.png)

Develop branching structures for product planning, stories, and creative ideas. Edit topics, layouts, folding, and styles.

### Search and filtering

![Document search](docs/images/search-document.png)

![Filtering in a narrow window](docs/images/canvas-narrow-filter.png)

## Features

| Area | Capabilities |
| --- | --- |
| Freeform canvas | Import, move, scale, rotate, flip, and arrange references; actual-size viewing; pen strokes, lines, and arrows |
| Resource tree | Organize canvases, documents, mind maps, and folders; create, rename, move, reorder, trash, and restore |
| Material management | Titles, notes, tags, 1–5 star ratings, batch editing, and filtering by tags, rating, or color |
| Project search | Find pages, document text, mind map topics, and material information; navigate to matching content |
| Documents | Headings, rich text, lists, tasks, images, tables, links, code blocks, and find/replace |
| Mind maps | Sibling and child topics, branch editing, folding, layouts, colors, text styles, and topic metadata |
| Video frames | Inspect video metadata, extract frames, cancel tasks, and import results into a canvas |
| Image inspection | Actual-size display, exposure, channels, histogram, and tone mapping without overwriting the original |
| 3D previews | GLB/glTF thumbnails and an interactive preview with rotation, zoom, and reset |
| Capture | Clipboard image inbox and a companion browser extension for sending web images |
| Duplicate review | Duplicate/similar image candidates, grouped results, and navigation; no automatic deletion |
| Import/export | Folder, image, DOCX, and XMind imports; canvas, source-material, document, and mind map exports |
| Local persistence | Local projects, unsaved-change prompts, autosave, and recovery for some editor content |

Format support depends on file contents and the available decoder. PSD support displays the composited preview rather than editable layers. GLB/glTF support is a preview, not a 3D editor.

## Windows installation

1. Open this repository's **Releases** page.
2. Download `Prism-Setup-0.3.5-windows-x64.exe`.
3. Run the installer and choose an installation directory. A desktop shortcut is optional.
4. Launch Prism from the Start menu or open a `.prism` project.

The installer includes Python and runtime dependencies. It installs for the current user without requiring administrator privileges and supports uninstalling through Windows Settings. Application and project files share the new Prism icon.

Windows x64 is the current installer target. This build was tested on Windows 11 with Python 3.12. Other Windows versions, Linux, and macOS have not received equivalent installer validation.

The installer is currently unsigned. Obtain it from this repository's Releases and compare its SHA-256 with the published checksum.

## Getting started

1. Create or open a project and add a canvas from the left resource tree.
2. Drag reference images onto the canvas; arrange, scale, and compare them.
3. Select a material to add its title, notes, tags, and rating in the inspector.
4. Add a document for notes and a mind map for structure.
5. Use project search and filters to find content.
6. Save with `Ctrl+S` and continue from the `.prism` file later.

### Common shortcuts

| Action | Shortcut |
| --- | --- |
| Save project | `Ctrl+S` |
| Undo / redo | `Ctrl+Z` / `Ctrl+Y`; scope depends on the active editor |
| Actual image size | Select a canvas image and press `3` |
| Rename resource | `F2` |
| Mind map child / sibling | `Tab` / `Enter` |
| Edit mind map text | `F2` or double-click |
| Paste without formatting | `Ctrl+Shift+V` in documents |
| Drag to another application | Select a canvas material and drag with `Ctrl+Shift` held |

Canvas controls are described in the application's help and settings; some gestures are configurable.

## Browser capture

The companion extension is in [`browser-extension`](browser-extension). Follow its [instructions](browser-extension/README.md), load it in a Chromium browser, and start Prism before sending an image.

The bridge listens only on `127.0.0.1:47653`. Network access downloads the image selected by the user. Sites requiring authentication, anti-hotlink protection, blob URLs, and CSS background images may not work. The extension currently uses manual loading and is not presented as a Chrome Web Store release.

## Data and privacy

- Projects are local; cloud synchronization and collaboration are not included.
- Images and document attachments can be embedded. Videos and large models may reference external files. Check these source files when moving a project.
- Clipboard capture can be disabled. It primarily captures images and filters known password-manager sources; this does not guarantee detection of every sensitive source.
- Settings, logs, and recovery records live in the current user's Prism configuration directory. Logs can contain paths and filenames; redact private details before sharing them.
- Recovery primarily covers text/page content and is not a full-project backup. Keep independent backups of important work.

## Current limitations

- No AI semantic image search, cloud sync, smart folders, or folder watching.
- Perceptual-hash similarity needs human review; flat-color materials may produce false positives.
- Document import/export does not guarantee exact Word/WPS layout fidelity.
- Mind map vector SVG/PDF export is not delivered. Use the formats shown in the actual export menu.
- Historical Qt/media shutdown crashes remain relevant. Some tests run in isolated processes; passing assertions do not prove every shutdown path is fixed.
- Installation, startup, uninstall, and selected regression tests have been checked. Clean-VM testing, every media format, and all DPI combinations remain unverified.

## Run from source

Use Python 3.12. In Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[media]"
.\.venv\Scripts\python -m prism
```

Use `pip install -e .` for the base app. The optional `[media]` extra adds OpenImageIO and Pillow for extended image decoding. Model previews attempt a software fallback if an OpenGL context is unavailable.

### Tests

```powershell
.\.venv\Scripts\python -m pip install -r requirements/test.txt
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python tools/run_tests_safe.py
```

The runner uses isolated processes per test file and reports timeouts, assertion failures, and abnormal exits. Review exception markers as well as pass counts.

### Build the installer

```powershell
.\.venv\Scripts\python -m pip install -r requirements/build.txt
.\.venv\Scripts\python tools/collect_licenses.py
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean Prism.spec
# After installing Inno Setup 6:
iscc packaging/Prism.iss
```

The application payload is in `dist/Prism`; the user-facing installer is in `release`. Publish matching source, build scripts, and dependency notices alongside the binary. See [the release guide](packaging/README.md).

## Repository layout

```text
prism/                 Application code and bundled resources
browser-extension/     Companion browser capture extension
tests/                 Automated tests
tools/                 Reusable development/build tools
packaging/             Installer scripts, licenses, release configuration
docs/images/           Screenshots from the current app
.github/workflows/     Automated checks and release workflow
```

Virtual environments, build caches, raw logs, private data, and installers are excluded from the source repository. Distribute installers through GitHub Releases.

## License and credits

Prism is released under **GPL-3.0-or-later**. Preserve upstream attribution and license notices when redistributing modified source or installers, and meet the corresponding-source requirements.

- Upstream author: Rebecca Breu; see [NOTICE](NOTICE).
- Desktop framework: PyQt6 / Qt.
- Document engine: Quill, BSD-3-Clause.
- Mind map engine: simple-mind-map, MIT.
- Video decoding: PyAV and its bundled FFmpeg build.

See [LICENSE](LICENSE), [THIRD_PARTY.md](THIRD_PARTY.md), and `packaging/licenses` for notices. The Prism logo is a generated design for this project; its SVG file embeds a PNG and is not a hand-authored vector master.

Use this repository's Issues for bug reports. Include the application version, OS version, reproduction steps, and redacted screenshots. Do not upload private projects or sensitive logs.
