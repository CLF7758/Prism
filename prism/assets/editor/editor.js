/* Document editor page for Prism.
 *
 * Quill (BSD-3, vendored under vendor/) is the editing surface. Python owns
 * everything else: it sends the document in, receives every change back as
 * HTML, and hosts the dialogs that Quill has no equivalent for (file pickers,
 * colour chooser, table size, image size).
 *
 * Two conventions matter when reading this file:
 *
 * 1. Images. The board keeps every picture in its attachments table and refers
 *    to it as prism://attach/<id>. Chromium cannot load that scheme (a custom
 *    QWebEngineUrlScheme would have to be registered before QApplication
 *    exists), so Python writes the attachments into a scratch directory and
 *    rewrites the URLs to file:// before sending the document here; it rewrites
 *    them back when the HTML comes home. This page therefore only ever sees
 *    file:// image URLs, and never has to know about prism:// at all.
 *
 * 2. Text. Every user-visible string lives in the label table Python pushes in
 *    (document_panel.py holds the _() calls, i18n/zh_CN.py the translations).
 *    Nothing in this file may hard-code UI text.
 */
(function () {
  'use strict';

  var parchment = Quill.import('parchment');
  var Scope = parchment.Scope;
  var StyleAttributor = parchment.StyleAttributor;
  var Image = Quill.import('formats/image');

  /* Quill knows nothing about arbitrary font sizes, colours or line heights,
   * and quietly drops inline styles it has no attributor for. Registering them
   * keeps documents written by other tools (Qt's rich text writer, Word,
   * earlier Prism builds) readable instead of flattening them on open. */
  [
    new StyleAttributor('fontSize', 'font-size', { scope: Scope.INLINE }),
    new StyleAttributor('font', 'font-family', { scope: Scope.INLINE }),
    new StyleAttributor('color', 'color', { scope: Scope.INLINE }),
    new StyleAttributor('align', 'text-align', { scope: Scope.BLOCK }),
    new StyleAttributor('lineHeight', 'line-height', { scope: Scope.BLOCK }),
    // Image size travels as an inline style so it survives a round trip
    // through the board file and the Word export.
    new StyleAttributor('width', 'width', { scope: Scope.INLINE }),
    new StyleAttributor('height', 'height', { scope: Scope.INLINE }),
  ].forEach(function (attributor) {
    Quill.register(attributor, true);
  });

  /* Quill's stock image blot passes every src through a protocol whitelist,
   * which turns the host's file:// URLs into about:blank - every picture would
   * be a broken placeholder. Taking the value verbatim is safe here because the
   * page only ever receives URLs this process produced. Width and height ride
   * along so the "image size" command and the Word export keep working. */
  class PrismImage extends Image {
    static create(value) {
      var node = super.create(value);
      if (typeof value === 'string') {
        node.setAttribute('src', value);
      }
      return node;
    }

    static formats(domNode) {
      var formats = super.formats(domNode) || {};
      var width = domNode.style.width || attributeSize(domNode, 'width');
      var height = domNode.style.height || attributeSize(domNode, 'height');
      if (width) { formats.width = width; }
      if (height) { formats.height = height; }
      return formats;
    }

    format(name, value) {
      if (name === 'width' || name === 'height') {
        if (value) {
          this.domNode.style[name] = value;
        } else {
          this.domNode.style.removeProperty(name);
        }
      } else {
        super.format(name, value);
      }
    }
  }
  PrismImage.blotName = 'image';
  PrismImage.tagName = 'IMG';
  PrismImage.className = 'ql-image';
  Quill.register(PrismImage, true);

  function attributeSize(domNode, name) {
    var value = domNode.getAttribute(name);
    return value ? value + 'px' : '';
  }

  // ── Host communication ───────────────────────────────────────

  var bridge = null;
  var labels = {};

  function t(key, fallback) {
    if (labels && labels[key]) { return labels[key]; }
    return fallback !== undefined ? fallback : key;
  }

  function setStatus(text, timeout) {
    var status = document.getElementById('status');
    status.textContent = text || '';
    if (statusTimer) { window.clearTimeout(statusTimer); }
    if (text) {
      statusTimer = window.setTimeout(function () {
        status.textContent = '';
      }, timeout || 6000);
    }
  }
  var statusTimer = null;

  // ── Editor ───────────────────────────────────────────────────

  var quill = new Quill('#editor', {
    theme: 'snow',
    placeholder: '',
    modules: {
      toolbar: false,
      table: true,
      // One undo step per typing burst, and API pushes never enter the stack:
      // opening a document must not be undoable.
      history: { userOnly: true, delay: 400, maxStack: 200 },
      clipboard: { matchVisual: false },
      keyboard: {
        bindings: {
          findReplace: {
            key: 'h',
            shortKey: true,
            handler: function () { toggleFindBar(true); },
          },
          saveDocument: {
            key: 's',
            shortKey: true,
            handler: function () { if (bridge) { bridge.requestSave(); } },
          },
          insertLink: {
            key: 'k',
            shortKey: true,
            handler: function () { requestCommand('insert-link'); },
          },
        },
      },
    },
  });

  var workspace = document.getElementById('workspace');
  var findbar = document.getElementById('findbar');
  var findText = document.getElementById('find-text');
  var replaceText = document.getElementById('replace-text');

  quill.on('editor-change', function (eventName) {
    if (eventName === 'selection-change' || eventName === 'text-change') {
      updateRibbonState();
    }
  });

  var changeTimer = null;
  quill.on('text-change', function (delta, oldDelta, source) {
    if (source !== 'user') { return; }
    if (changeTimer) { window.clearTimeout(changeTimer); }
    changeTimer = window.setTimeout(function () {
      changeTimer = null;
      if (bridge) { bridge.pushContent(getHtml()); }
    }, 300);
  });

  function getHtml() {
    var html = quill.getSemanticHTML();
    return isBlank(html) ? '' : html;
  }

  function isBlank(html) {
    if (html.indexOf('<img') !== -1) { return false; }
    var text = html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ');
    return text.replace(/\s|&#\d+;/g, '') === '';
  }

  function loadDocument(payload) {
    var html = (payload && payload.html) || '';
    quill.setContents([{ insert: '\n' }], 'silent');
    if (html) {
      quill.clipboard.dangerouslyPasteHTML(html, 'api');
    }
    quill.setSelection(0, 0, 'silent');
    workspace.scrollTop = 0;
    updateRibbonState();
  }

  // ── Ribbon ───────────────────────────────────────────────────

  function selectTab(id) {
    var tabs = document.querySelectorAll('#ribbon-tabs button');
    var panes = document.querySelectorAll('#ribbon-panes .pane');
    Array.prototype.forEach.call(tabs, function (button) {
      button.classList.toggle('active', button.dataset.tab === id);
    });
    Array.prototype.forEach.call(panes, function (pane) {
      pane.classList.toggle('active', pane.dataset.pane === id);
    });
  }

  function renderItem(item) {
    if (item.type === 'select') { return renderSelect(item); }
    if (item.type === 'input') { return renderInput(item); }
    return renderButton(item);
  }

  function renderButton(item) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'rbtn';
    if (item.style) { button.classList.add(item.style); }
    var iconNames = {undo: 'undo', redo: 'redo'};
    if (item.command === 'align') {
      iconNames.align = item.value === 'center' ? 'align-center' : item.value === 'right' ? 'align-right' : 'align-left';
    }
    if (iconNames[item.command]) {
      var image = document.createElement('img');
      image.src = '../icons/' + iconNames[item.command] + '.svg';
      image.width = image.height = 16;
      image.alt = '';
      button.appendChild(image);
      button.setAttribute('aria-label', item.label);
    } else {
      var displayLabels = {'bold': '加粗', 'italic': '斜体', 'underline': '下划线', 'code-block': '代码块', 'find-replace': '查找替换'};
      var displayLabel = displayLabels[item.command];
      if (item.command === 'header') displayLabel = item.value ? '标题 ' + item.value : '正文';
      if (item.command === 'list') displayLabel = item.value === 'bullet' ? '项目符号' : '编号';
      button.textContent = displayLabel || item.label;
    }
    if (item.tip) { button.title = item.tip; }
    button.dataset.command = item.command;
    if (item.mode) { button.dataset.mode = item.mode; }
    if (item.group) { button.dataset.group = item.group; }
    if (Object.prototype.hasOwnProperty.call(item, 'value')) {
      button.dataset.value = JSON.stringify(item.value);
    }
    button.addEventListener('click', function () {
      runCommand(item);
    });
    return button;
  }

  function renderSelect(item) {
    var select = document.createElement('select');
    select.className = 'rsel';
    select.dataset.command = item.command;
    select.dataset.mode = 'select';
    if (item.tip) { select.title = item.tip; }
    (item.options || []).forEach(function (option) {
      var entry = document.createElement('option');
      entry.value = option.value;
      entry.textContent = option.label;
      select.appendChild(entry);
    });
    select.addEventListener('change', function () {
      quill.focus();
      // An empty option means "whatever the document default is", which is
      // how Quill spells "remove this format".
      quill.format(item.command, select.value === '' ? false : select.value, 'user');
    });
    return select;
  }

  function renderInput(item) {
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'rtext';
    input.value = item.value || '';
    input.dataset.command = item.command;
    input.dataset.mode = 'input';
    input.dataset.unit = item.unit || '';
    if (item.tip) { input.title = item.tip; }
    var apply = function () {
      var raw = String(input.value || '').replace(/[^0-9.]/g, '');
      var size = parseFloat(raw);
      if (!size || size < 4 || size > 200) {
        updateRibbonState();
        return;
      }
      quill.focus();
      quill.format(item.command, size + (input.dataset.unit || ''), 'user');
    };
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        apply();
        quill.focus();
      }
    });
    input.addEventListener('blur', apply);
    return input;
  }

  function renderRibbon(spec) {
    window.__ribbonSpec = spec;
    var tabs = document.getElementById('ribbon-tabs');
    var panes = document.getElementById('ribbon-panes');
    var active = document.querySelector('#ribbon-tabs button.active');
    var wanted = (active && active.dataset.tab) || spec.initialTab;
    tabs.innerHTML = '';
    panes.innerHTML = '';
    (spec.tabs || []).forEach(function (tab) {
      var tabButton = document.createElement('button');
      tabButton.type = 'button';
      tabButton.textContent = tab.label;
      tabButton.dataset.tab = tab.id;
      tabButton.addEventListener('click', function () { selectTab(tab.id); });
      tabs.appendChild(tabButton);

      var pane = document.createElement('div');
      pane.className = 'pane';
      pane.dataset.pane = tab.id;
      (tab.groups || []).forEach(function (group) {
        var box = document.createElement('div');
        box.className = 'rgroup';
        (group.items || []).forEach(function (item) {
          box.appendChild(renderItem(item));
        });
        pane.appendChild(box);
      });
      panes.appendChild(pane);
    });
    selectTab(wanted || (spec.tabs && spec.tabs[0] && spec.tabs[0].id));
    updateRibbonState();
  }

  /* Clicking a ribbon control must not steal the selection, so the buttons
   * swallow mousedown and Quill keeps its saved range. */
  document.getElementById('ribbon').addEventListener('mousedown', function (event) {
    if (event.target.closest('button')) { event.preventDefault(); }
  });

  function updateRibbonState() {
    var formats = quill.getFormat();
    var nodes = document.querySelectorAll('#ribbon-panes [data-mode]');
    Array.prototype.forEach.call(nodes, function (node) {
      var command = node.dataset.command;
      var mode = node.dataset.mode;
      if (mode === 'select') {
        node.value = formats[command] || '';
      } else if (mode === 'input') {
        // No size of its own means "inherit"; keep the last value on screen
        // instead of blanking the box while the caret sits in plain text.
        var size = formatSize(formats[command]);
        if (size) { node.value = size; }
      } else if (mode === 'toggle') {
        node.classList.toggle('active', !!formats[command]);
      } else if (mode === 'radio') {
        var wanted = JSON.parse(node.dataset.value);
        node.classList.toggle('active', sameFormat(formats[command], wanted));
      }
    });
  }

  function formatSize(value) {
    if (!value) { return ''; }
    return String(value).replace(/(px|pt|em|%)$/, '');
  }

  function sameFormat(current, wanted) {
    var blank = function (value) {
      return value === undefined || value === null || value === false || value === '';
    };
    if (blank(current) && blank(wanted)) { return true; }
    return String(current) === String(wanted);
  }

  // ── Commands ─────────────────────────────────────────────────

  function runCommand(item) {
    if (item.mode === 'toggle' || item.mode === 'radio') {
      quill.focus();
      var wanted = item.mode === 'toggle'
        ? !quill.getFormat()[item.command]
        : item.value;
      quill.format(item.command, wanted === null ? false : wanted, 'user');
      updateRibbonState();
      return;
    }
    var action = ACTIONS[item.command];
    if (action) {
      action();
      return;
    }
    requestCommand(item.command, item.payload);
  }

  var ACTIONS = {
    undo: function () { quill.focus(); quill.history.undo(); },
    redo: function () { quill.focus(); quill.history.redo(); },
    'clear-format': function () {
      var range = quill.getSelection(true);
      if (range && range.length) {
        quill.removeFormat(range.index, range.length, 'user');
      } else {
        setStatus(t('selectTextFirst'));
      }
    },
    'find-replace': function () { toggleFindBar(true); },
    'insert-image': function () { requestCommand('insert-image'); },
    'insert-canvas': function () { requestCommand('insert-canvas'); },
    'insert-table': function () { requestCommand('insert-table'); },
    'insert-link': function () { requestCommand('insert-link'); },
    'text-color': function () { requestCommand('text-color'); },
    'image-size': function () { requestCommand('image-size', imageSizeInfo() || {}); },
    'image-delete': function () { deleteImage(); },
    'table-add-row': function () { tableCommand('insertRowBelow'); },
    'table-del-row': function () { tableCommand('deleteRow'); },
    'table-add-col': function () { tableCommand('insertColumnRight'); },
    'table-del-col': function () { tableCommand('deleteColumn'); },
    'page-view': function () { setPageView(true); },
    'web-view': function () { setPageView(false); },
    'zoom-in': function () { setZoom(0.1); },
    'zoom-out': function () { setZoom(-0.1); },
    'zoom-reset': function () { pageZoom = 1; applyZoom(); },
  };

  function requestCommand(id, payload) {
    if (bridge) {
      bridge.requestCommand(id, JSON.stringify(payload || {}));
    }
  }

  function setPageView(enabled) {
    document.body.classList.toggle('web-view', !enabled);
    var nodes = document.querySelectorAll('#ribbon-panes [data-mode="radio"]');
    Array.prototype.forEach.call(nodes, function (node) {
      if (node.dataset.group !== 'view') { return; }
      node.classList.toggle('active', node.dataset.command === (enabled ? 'page-view' : 'web-view'));
    });
  }

  var pageZoom = 1;
  function setZoom(step) {
    pageZoom = Math.min(2, Math.max(0.4, Math.round((pageZoom + step) * 10) / 10));
    applyZoom();
  }
  function applyZoom() {
    document.getElementById('page').style.zoom = String(pageZoom);
    setStatus(t('zoomLabel').replace('{percent}', String(Math.round(pageZoom * 100))), 2500);
  }

  // ── Tables ───────────────────────────────────────────────────

  function tableCommand(name) {
    var table = quill.getModule('table');
    if (!table) { return; }
    quill.focus();
    var range = quill.getSelection(true);
    var inside = null;
    try {
      inside = table.getTable(range);
    } catch (error) {
      inside = null;
    }
    // getTable() hands back [table, row, cell, offset] and reports "no table"
    // with a null *first* element, so testing the array itself never fails.
    if (!inside || !inside[0]) {
      setStatus(t('tableHint'));
      return;
    }
    table[name]();
    updateRibbonState();
  }

  function insertTable(rows, cols) {
    rows = Math.max(1, Math.min(50, rows || 3));
    cols = Math.max(1, Math.min(20, cols || 3));
    var range = quill.getSelection(true);
    var index = range ? range.index + (range.length || 0) : quill.getLength() - 1;
    var html = ['<table><tbody>'];
    for (var row = 0; row < rows; row += 1) {
      html.push('<tr>');
      for (var col = 0; col < cols; col += 1) { html.push('<td><br></td>'); }
      html.push('</tr>');
    }
    html.push('</tbody></table><p><br></p>');
    quill.clipboard.dangerouslyPasteHTML(index, html.join(''), 'user');
  }

  // ── Images ───────────────────────────────────────────────────

  function insertImages(urls, index) {
    if (!urls || !urls.length) { return; }
    quill.focus();
    var range = quill.getSelection(true);
    var at = typeof index === 'number' && index >= 0
      ? index
      : (range ? range.index + (range.length || 0) : quill.getLength() - 1);
    urls.forEach(function (url) {
      quill.insertEmbed(at, 'image', url, 'user');
      quill.insertText(at + 1, '\n', 'user');
      at += 2;
    });
    quill.setSelection(at, 0, 'user');
    if (changeTimer) { window.clearTimeout(changeTimer); }
    if (bridge) { bridge.pushContent(getHtml()); }
  }

  var pendingImages = {};
  var tokenCounter = 0;

  function handleFiles(files, index) {
    Array.prototype.forEach.call(files, function (file) {
      var name = file.name || 'pasted.png';
      var mime = file.type || '';
      if (mime && mime.indexOf('image/') !== 0 && !/\.(png|jpe?g|gif|bmp|webp|tiff?|svg)$/i.test(name)) {
        setStatus(t('unsupportedImage').replace('{name}', name));
        return;
      }
      tokenCounter += 1;
      var token = 'img-' + tokenCounter;
      pendingImages[token] = { name: name, index: index };
      var reader = new FileReader();
      reader.onload = function () {
        var dataUrl = String(reader.result || '');
        var comma = dataUrl.indexOf(',');
        if (comma < 0 || !bridge) {
          delete pendingImages[token];
          setStatus(t('imageFailed').replace('{name}', name));
          return;
        }
        bridge.storeImage(token, name, mime || 'image/png', dataUrl.slice(comma + 1));
      };
      reader.onerror = function () {
        delete pendingImages[token];
        setStatus(t('imageFailed').replace('{name}', name));
      };
      reader.readAsDataURL(file);
    });
  }

  function onImageStored(token, url) {
    var entry = pendingImages[token];
    delete pendingImages[token];
    if (!entry) { return; }
    if (!url) {
      setStatus(t('imageFailed').replace('{name}', entry.name));
      return;
    }
    insertImages([url], entry.index);
  }

  /* Quill only knows where the caret is while it holds the focus, and
   * clicking a ribbon button takes that focus away: by the time a command
   * runs, getSelection() is null and every picture command would answer
   * "select an image first" no matter what was selected.  Quill's own
   * focus() puts the caret back where it was - it reapplies the range the
   * editor saved when it lost focus - so it has to come first. */
  function currentImage() {
    quill.focus();
    var range = quill.getSelection();
    if (!range) { return null; }
    var found = quill.scroll.descendant(PrismImage, range.index);
    var blot = found && found[0];
    if (!blot && range.index > 0) {
      found = quill.scroll.descendant(PrismImage, range.index - 1);
      blot = found && found[0];
    }
    return blot || null;
  }

  function imageSizeInfo() {
    var blot = currentImage();
    if (!blot) { return null; }
    var node = blot.domNode;
    var width = node.style.width ? parseFloat(node.style.width) : (node.naturalWidth || node.width || 0);
    var height = node.style.height ? parseFloat(node.style.height) : (node.naturalHeight || node.height || 0);
    return { width: Math.round(width) || 0, height: Math.round(height) || 0 };
  }

  function setImageSize(width) {
    var blot = currentImage();
    if (!blot) {
      setStatus(t('imageHint'));
      return false;
    }
    var node = blot.domNode;
    var baseWidth = node.naturalWidth || node.width || parseFloat(node.style.width) || 0;
    var baseHeight = node.naturalHeight || node.height || parseFloat(node.style.height) || 0;
    var height = baseWidth ? Math.round(width * (baseHeight / baseWidth)) : 0;
    var index = quill.getIndex(blot);
    quill.formatText(index, 1, {
      width: width + 'px',
      height: height ? height + 'px' : '',
    }, 'user');
    return true;
  }

  /* Deleting a picture is possible from the keyboard - put the caret next to
   * it and press Backspace - but nothing on screen says so.  The ribbon
   * button makes it discoverable, and works no matter where the caret is. */
  function deleteImage() {
    var blot = currentImage();
    if (!blot) {
      setStatus(t('imageHint'));
      return false;
    }
    quill.deleteText(quill.getIndex(blot), 1, 'user');
    return true;
  }

  /* Ctrl+Shift+V means "paste without the formatting".  A ClipboardEvent
   * carries no modifier keys of its own, so the combination is remembered
   * while the keys are held and spent on the paste that follows.  Without
   * this the only way to get clean text out of Word or a web page would be
   * to paste it and then strip the styling by hand. */
  var pasteAsPlainText = false;

  // Files dragged in from Explorer, and screenshots pasted in.
  document.getElementById('editor').addEventListener('paste', function (event) {
    var files = (event.clipboardData && event.clipboardData.files) || [];
    if (!files.length) {
      if (pasteAsPlainText) {
        event.preventDefault();
        event.stopPropagation();
        pasteAsPlainText = false;
        insertPlainText(pasteText(event));
      }
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    handleFiles(files, null);
  }, true);

  document.addEventListener('keydown', function (event) {
    if ((event.ctrlKey || event.metaKey) && event.shiftKey
        && String(event.key).toLowerCase() === 'v') {
      pasteAsPlainText = true;
    }
  }, true);

  document.addEventListener('keyup', function (event) {
    if (String(event.key).toLowerCase() === 'v') {
      pasteAsPlainText = false;
    }
  }, true);

  function pasteText(event) {
    var data = event.clipboardData;
    if (!data) { return ''; }
    try {
      return data.getData('text/plain') || '';
    } catch (error) {
      return '';
    }
  }

  /* Each line break becomes a real paragraph, so pasting a list does not
   * leave one long line carrying invisible newlines. */
  function insertPlainText(text) {
    if (!text) { return; }
    quill.focus();
    var range = quill.getSelection(true);
    var at = range ? range.index + (range.length || 0) : quill.getLength() - 1;
    var lines = text.replace(/\r\n?/g, '\n').split('\n');
    var cursor = at;
    lines.forEach(function (line, position) {
      if (position) {
        quill.insertText(cursor, '\n', 'user');
        cursor += 1;
      }
      if (line) {
        quill.insertText(cursor, line, 'user');
        cursor += line.length;
      }
    });
    quill.setSelection(cursor, 0, 'user');
    if (changeTimer) { window.clearTimeout(changeTimer); }
    if (bridge) { bridge.pushContent(getHtml()); }
  }

  workspace.addEventListener('dragover', function (event) {
    if (!event.dataTransfer) { return; }
    var types = Array.prototype.slice.call(event.dataTransfer.types || []);
    if (types.indexOf('Files') === -1) { return; }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    dropIndex = caretIndexAt(event.clientX, event.clientY);
  });

  workspace.addEventListener('drop', function (event) {
    var files = (event.dataTransfer && event.dataTransfer.files) || [];
    if (!files.length) { return; }
    event.preventDefault();
    handleFiles(files, dropIndex);
    dropIndex = null;
  });

  var dropIndex = null;

  /* Turning the pointer position into a document index keeps a dropped picture
   * where the user let go of it instead of at the old caret. */
  function caretIndexAt(x, y) {
    try {
      if (!document.caretRangeFromPoint) { return null; }
      var range = document.caretRangeFromPoint(x, y);
      if (!range || !quill.root.contains(range.startContainer)) { return null; }
      var blot = Quill.find(range.startContainer);
      if (!blot) { return null; }
      return quill.getIndex(blot) + range.startOffset;
    } catch (error) {
      return null;
    }
  }

  // ── Find and replace ─────────────────────────────────────────

  /* The find bar holds the focus for as long as it is open, so
   * quill.getSelection() is null for every press of its buttons - asking
   * Quill where the caret is would send "find next" back to the first hit
   * every time, and make "replace" a no-op.  The position of the last match
   * is therefore remembered here, and Quill is only consulted when the
   * editor actually holds the focus (the user can click into the text with
   * the bar still open).  It must not steal the focus back: that would stop
   * the user from typing the next search term. */
  var lastFind = { needle: '', index: -1 };

  function findStart(needle) {
    var range = quill.hasFocus() ? quill.getSelection() : null;
    if (range) {
      return range.index + (range.length || 0);
    }
    if (lastFind.needle === needle && lastFind.index >= 0) {
      return lastFind.index + needle.length;
    }
    return 0;
  }

  function rememberMatch(needle, index) {
    lastFind.needle = needle;
    lastFind.index = index;
  }

  function toggleFindBar(show) {
    findbar.hidden = !show;
    if (show) {
      findText.focus();
      findText.select();
    } else {
      quill.focus();
    }
  }

  function findNext() {
    var needle = findText.value;
    if (!needle) { return; }
    var haystack = quill.getText();
    var start = findStart(needle);
    var index = haystack.indexOf(needle, start);
    if (index === -1 && start > 0) {
      // Past the end: start over, so pressing "find next" repeatedly walks
      // the whole document and then wraps around.
      index = haystack.indexOf(needle);
    }
    if (index === -1) {
      rememberMatch(needle, -1);
      setStatus(t('notFound'));
      return;
    }
    rememberMatch(needle, index);
    quill.setSelection(index, needle.length, 'user');
    var bounds = quill.getBounds(index, needle.length);
    if (bounds) {
      workspace.scrollTop += bounds.top - 140;
    }
  }

  function replaceOne() {
    var needle = findText.value;
    if (!needle) { return; }
    var index = lastFind.needle === needle ? lastFind.index : -1;
    if (index >= 0 && quill.getText(index, needle.length) === needle) {
      quill.deleteText(index, needle.length, 'user');
      if (replaceText.value) {
        quill.insertText(index, replaceText.value, 'user');
      }
      rememberMatch(needle, index);
    }
    findNext();
  }

  function replaceAll() {
    var needle = findText.value;
    if (!needle) { return; }
    var haystack = quill.getText();
    var matches = [];
    var index = haystack.indexOf(needle);
    while (index !== -1) {
      matches.push(index);
      index = haystack.indexOf(needle, index + needle.length);
    }
    if (!matches.length) {
      setStatus(t('notFound'));
      return;
    }
    var replacement = replaceText.value;
    var ops = [];
    var last = 0;
    matches.forEach(function (position) {
      if (position > last) { ops.push({ retain: position - last }); }
      ops.push({ delete: needle.length });
      if (replacement) { ops.push({ insert: replacement }); }
      last = position + needle.length;
    });
    quill.updateContents({ ops: ops }, 'user');
    setStatus(t('replaceDone').replace('{count}', String(matches.length)));
  }

  document.getElementById('find-next').addEventListener('click', findNext);
  document.getElementById('replace-one').addEventListener('click', replaceOne);
  document.getElementById('replace-all').addEventListener('click', replaceAll);
  document.getElementById('find-close').addEventListener('click', function () {
    toggleFindBar(false);
  });
  [findText, replaceText].forEach(function (input) {
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        findNext();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        toggleFindBar(false);
      }
    });
  });

  // ── Host commands ────────────────────────────────────────────

  function applyHostCommand(payload) {
    if (!payload) { return; }
    if (payload.id === 'insert-images') {
      insertImages(payload.urls || [], typeof payload.index === 'number' ? payload.index : undefined);
    } else if (payload.id === 'insert-table') {
      insertTable(payload.rows, payload.cols);
    } else if (payload.id === 'insert-link') {
      insertLink(payload.text, payload.url);
    } else if (payload.id === 'set-color') {
      quill.focus();
      quill.format('color', payload.color || false, 'user');
      updateRibbonState();
    } else if (payload.id === 'image-size') {
      setImageSize(payload.width);
    } else if (payload.id === 'insert-text') {
      var range = quill.getSelection(true);
      var at = range ? range.index + (range.length || 0) : quill.getLength() - 1;
      quill.insertText(at, payload.text || '', 'user');
    } else if (payload.id === 'focus-editor') {
      quill.focus();
    } else if (payload.id === 'status') {
      setStatus(payload.text || '');
    }
  }

  function insertLink(text, url) {
    quill.focus();
    var range = quill.getSelection(true);
    var index = range ? range.index + (range.length || 0) : quill.getLength() - 1;
    if (range && range.length) {
      quill.formatText(range.index, range.length, 'link', url, 'user');
      return;
    }
    if (!text) { return; }
    quill.insertText(index, text, { link: url }, 'user');
    quill.setSelection(index + text.length, 0, 'user');
  }

  function applyLabels() {
    document.getElementById('find-next').textContent = t('findNext', 'Find next');
    document.getElementById('replace-one').textContent = t('replaceOne', 'Replace');
    document.getElementById('replace-all').textContent = t('replaceAll', 'Replace all');
    document.getElementById('find-text').placeholder = t('findPlaceholder', 'Find');
    document.getElementById('replace-text').placeholder = t('replacePlaceholder', 'Replace with');
    document.getElementById('find-close').title = t('close', 'Close');
  }

  // ── Startup ──────────────────────────────────────────────────

  window.__flush = function () { return getHtml(); };
  window.__quill = quill;
  window.__restoreDocumentView = function (state) {
    if (state.selection) quill.setSelection(state.selection.index, state.selection.length, 'silent');
    else quill.setSelection(null, 'silent');
    const workspace = document.getElementById('workspace');
    workspace.scrollTop = state.top;
    workspace.scrollLeft = state.left;
  };
  window.__locateDocumentText = function (query) {
    const text = quill.getText().toLocaleLowerCase();
    const words = String(query || '').toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const word = words.find(value => text.includes(value));
    if (!word) return false;
    quill.setSelection(text.indexOf(word), word.length, 'api');
    quill.scrollSelectionIntoView();
    return true;
  };
  window.__isBlank = function () { return isBlank(quill.getSemanticHTML()); };

  if (typeof QWebChannel === 'undefined') {
    setStatus('QWebChannel unavailable');
  } else {
    new QWebChannel(qt.webChannelTransport, function (channel) {
      bridge = channel.objects.bridge;
      bridge.setLabels.connect(function (json) {
        try {
          labels = JSON.parse(json);
        } catch (error) {
          labels = {};
        }
        applyLabels();
      });
      bridge.setRibbon.connect(function (json) {
        try {
          renderRibbon(JSON.parse(json));
        } catch (error) {
          setStatus(String(error));
        }
      });
      bridge.loadDocument.connect(function (json) {
        var payload = null;
        try {
          payload = JSON.parse(json);
        } catch (error) {
          setStatus(String(error));
          return;
        }
        loadDocument(payload);
        bridge.notifyDocumentLoaded(true, '');
      });
      bridge.applyCommand.connect(function (json) {
        try {
          applyHostCommand(JSON.parse(json));
        } catch (error) {
          setStatus(String(error));
        }
      });
      bridge.imageStored.connect(onImageStored);
      bridge.notifyReady();
    });
  }
}());
