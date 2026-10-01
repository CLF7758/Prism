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
