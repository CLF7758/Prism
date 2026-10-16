# Dependencies and implementation references

Existing upstream copyright and GPL notices remain in the source tree.
New retrieval and capture integration code was written for Prism; no source
from Eagle, PureRef, TagStudio, Hydrus, PixlStash, XMind, MindNode, Obsidian
or Heptabase was copied. Those products were consulted for interaction
design only, never for code or assets.

## Dependencies

| Component | Version constraint | Use / source |
| --- | --- | --- |
| OpenImageIO | >=2.5 | Optional format decoding; BSD-3-Clause; https://github.com/AcademySoftwareFoundation/OpenImageIO |
| NumPy | >=1.26 | Pixel/vector operations; BSD-3-Clause; https://github.com/numpy/numpy |
| python-docx | >=1.1 | Word export fallback when pandoc is absent; MIT; https://github.com/python-openxml/python-docx |
| PyQt6 / PyQt6-WebEngine | ==6.7.0 | Python bindings, GPL-v3 / commercial dual license (not LGPL); this GPL project uses the GPL option; https://www.riverbankcomputing.com/software/pyqtwebengine/intro |
| Qt / Qt WebEngine | 6.7.x | Native runtime shipped by the PyQt wheels; LGPL/GPL and third-party Chromium notices apply; https://doc.qt.io/qt-6/licensing.html |
| PyAV / FFmpeg | av 19.x | PyAV BSD-3-Clause; FFmpeg licensing depends on the actual wheel build; preserve wheel license notices and source references; https://github.com/PyAV-Org/PyAV |

## Optional external tools

| Tool | Licence | How it is used |
| --- | --- | --- |
| pandoc | GPL-2.0-or-later | Word export, if the user installed it themselves. Prism shells out to the executable and never bundles or links it, so it stays at arm's length. The python-docx path takes over when pandoc is missing. |

## Implementation references

| Source | Licence | What was reused |
| --- | --- | --- |
| Quill (slab/quill) | BSD-3-Clause | The document editor's editing engine: rich text, lists, tables, code blocks and the HTML model the note is stored in. The upstream bundle ships unmodified in `prism/assets/editor/vendor` together with its BSD-3 license notice; nothing is fetched at runtime. |
| simple-mind-map (wanglin2/mind-map) | MIT | The mind map tree schema (`data: {text, children, image, hyperlink, note, tag}`) is followed so the JSON stays compatible; the upstream bundle ships in `prism/assets/mindmap/vendor` with its MIT license notice. |

The document toolbar uses the same action set the earlier `QTextEdit`-based
implementation took from Qt's `examples/widgets/richtext/textedit` example
(bold/italic/underline, headings, lists, insert image, insert table, link);
Quill now provides the editing behaviour behind those commands.

The difference-hash implementation is locally written. The Windows installer
ships collected dependency notices in its licenses directory. The dependency
inventory is in packaging/licenses/dependencies.json; native-library source
requirements are tracked in docs/RELEASE_AUDIT.md.

## Bundled isolated components (0.3.10)

- ScreenPinKit (InterwovenCode/ScreenPinKit), MIT, revision 1c0dc5fa14a9ec99660fbb3bc6eade7d7d1c3f77. Source is bundled with the capture component; the configuration-location hook is adapted for Prism.
- system_hotkey (YaoXuanZhi/system_hotkey), BSD-style upstream license, revision e3e9d308e7dd633c8ecbb9515de5e93b1975f01d.
- Capture runtime includes PyQt5 / PyQtWebEngine (GPL option), Qt and its native notices, OpenCV, NumPy, SciPy, ONNX Runtime and associated Python dependencies. Package notices are retained in site-packages.
- Local image search: OpenCLIP (MIT), FAISS (MIT), PyTorch / torchvision (BSD-style), timm (Apache-2.0), OpenCV (Apache-2.0), Pillow and NumPy. The OpenAI ViT-B-32 model is bundled with its upstream CLIP MIT notice.
- yt-dlp (Unlicense, with bundled third-party exceptions) and FFmpeg/ffprobe are included for the experimental download component. Their presence does not imply that every website is supported. Binary provenance is retained in the component manifests. FFmpeg builds may use GPL codecs; upstream licenses and corresponding source references apply.
- CPython 3.12 is bundled under the Python Software Foundation license, retained in the portable runtime.

Repository sources: https://github.com/InterwovenCode/ScreenPinKit ; https://github.com/mlfoundations/open_clip ; https://github.com/facebookresearch/faiss ; https://github.com/openai/CLIP ; https://github.com/yt-dlp/yt-dlp .

## Platform download adapters (source integration after 0.3.10)

- F2, https://github.com/Johnserf-Seed/f2, Apache-2.0, revision `f6be8c0ffba9a127075bbeafe4838716650b6325`. Its URL recognition, visitor configuration, API signing and video selector are reused. curl_cffi 0.16.3 (MIT) supplies the HTTPS transport. Anonymous visitor tokens remain in the worker's memory.
- XHS-Downloader 2.8, https://github.com/JoeanAmier/XHS-Downloader, GPL-3.0, revision `3261312721f0b37c705ba6515885bc7f34349f2f`. Upstream extraction/download/progress callbacks are reused. The staged source enables TLS verification and preserves the source video type when multiple cover frames are present; `tools/setup_platform_video_components.py` records and applies those changes. The download buffer is capped at 1 MiB by the adapter.
- bilibili-api-python 17.4.2, https://github.com/Nemo2011/bilibili-api, GPL-3.0-or-later. Metadata and stream selection use its API. aiohttp downloads the selected streams, and the existing bundled FFmpeg performs stream-copy muxing.
- Each platform runs in its own isolated Python runtime. Component manifests, complete upstream source for F2/XHS and dependency license files are included by the component staging script. No user cookies/settings/downloads are included. These changes have not replaced the published 0.3.10 installer.
- Xhshow, https://github.com/Cloxl/xhshow, MIT, revision `b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1`, supplies request signing and device identifiers for normal anonymous-session activation. PyCryptodome 3.23.0 supplies its cryptographic primitives. Both are isolated in `video-xhs`; pinned source and license are included in the component bundle. Temporary guest sessions remain in the worker's memory. Prism does not read Chrome credentials or solve verification challenges for this flow.

PDF output uses the existing Qt PDF writer and Qt WebEngine print backend. Mind map layout still comes from the bundled MIT-licensed simple-mind-map library; Prism exports its complete rendered SVG rather than implementing another layout engine.
