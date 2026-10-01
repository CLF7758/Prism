# Prism Image Capture

1. Start Prism.
2. Open `chrome://extensions`, enable Developer mode, and choose **Load unpacked**.
3. Select this `browser-extension` directory.
4. Hold **Alt** and right-click an image, or use the normal image context-menu
   command **保存图片到 Prism**.

The extension talks only to `127.0.0.1:47653`; Prism downloads and embeds the
image using its normal import pipeline. The keyboard shortcut repeats capture
of the last image sent through the extension.
