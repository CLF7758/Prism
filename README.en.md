<p align="center"><img src="prism/assets/logo.png" width="96" alt="Prism"></p>
<h1 align="center">Prism</h1>
<p align="center">A local creative workspace for references, notes and mind maps.</p>

**[Download Windows x64 installer · v0.3.9](https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-Setup-0.3.9-windows-x64.exe)** · [Release notes](https://github.com/CLF7758/Prism/releases/tag/v0.3.9) · [中文](README.md)

Arrange references on an infinite canvas, organize them with tags and folders, write project briefs, and plan your work with mind maps. Save the workspace locally as a `.prism` project.

![Infinite canvas](docs/images/release-canvas.png)

Screenshots are actual v0.3.9 UI renders with original procedural demonstration images. They contain no private projects and are not UI mockups.

## Install

1. Download [Prism-Setup-0.3.9-windows-x64.exe](https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-Setup-0.3.9-windows-x64.exe).
2. Run the installer and choose an installation directory. Python is included.
3. Launch Prism from the Start menu, drag in references, and save with `Ctrl+S`.

The installer runs per user without administrator privileges. Windows x64 is the current release target; primary development validation uses Windows 11. The installer is not code-signed. Matching [source](https://github.com/CLF7758/Prism/releases/download/v0.3.9/Prism-0.3.9-source.zip) and [SHA-256 checksums](https://github.com/CLF7758/Prism/releases/download/v0.3.9/SHA256.txt) are included in the release.

## Features

- Infinite reference canvas with move, zoom, rotate, crop and compact arrangement. Strokes, arrows and text remain in place during image arrangement.
- Canvas-based All Assets, Unclassified, Untagged, Recent, folder and tag views.
- Titles, notes, ratings, direct tag entry, color filters and project-wide content search.
- Duplicate/similar-image groups with locate-on-canvas, trash and undo synchronization. No automatic deletion.
- Rich-text documents and mind maps in the same project.
- Video frame extraction with automatic compact arrangement and full-resolution lossless PNG preservation.
- GLB/glTF previews, clipboard collection and a companion [browser extension](browser-extension/README.md).
- Original-image, adjusted-image and whole-canvas exports with distinct behavior.

![Asset library](docs/images/release-library.png)

![Duplicate results](docs/images/release-duplicates.png)

![Project document](docs/images/release-document.png)

![Mind map](docs/images/release-mindmap.png)

## What changed in 0.3.9

Large canvases use logical asset records, viewport-driven editing items, bounded multiresolution previews and tiled display caches. Zooming no longer forces a 192-pixel preview during wheel interaction. Decode queues and operation logs are bounded.

Duplicate scans reuse read-only archive connections and compute original-image fingerprints in a bounded process pool. In one 2,712-image project on the development machine, first scanning with an empty fingerprint cache improved from about 55 seconds to 21.4 seconds; an in-process cached scan took about 0.23 seconds. All fingerprints matched the previous implementation and both returned 44 groups. OS disk caches were not cleared; this is not a cold-disk or universal performance claim.

This version also fixes erasing strokes, arrows, F2 renaming, sidebar selection, canvas location, temporary annotations, frame arrangement, and original-export false warnings. The canvas AI-tagging entry was removed; this installer does not bundle the tagging model. Architecture cleanup continues and some central files remain large.

## Data behavior

Imported image bytes are preserved where available and embedded when the project is saved. Display previews do not replace originals. Video/model embedding versus referencing depends on import options. Generated captures and frames cannot preserve source-file EXIF that they never had. Missing originals are still reported rather than disguised as lossless exports.

Annotations in public asset and tag views are temporary and cleared on navigation. Use an actual canvas for persistent annotations. Keep backups of important projects.

## Develop

Python 3.12 is required:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements/windows-lock.txt
.\.venv\Scripts\python -m pip install --no-deps -e .
.\.venv\Scripts\python -m prism
```

See [build instructions](packaging/README.md) and [developer tools](tools/README.md). Restart after changing source; installed executables do not update themselves. When reporting issues, include the version and reproduction steps and remove private file paths from shared logs.

## License

Based on Rebecca Breu's open-source reference-image application, distributed under [GPL-3.0-or-later](LICENSE). See [NOTICE](NOTICE), [third-party notices](THIRD_PARTY.md), and the scoped [release audit](docs/RELEASE_AUDIT.md). Binary releases include matching project source and dependency notices.
