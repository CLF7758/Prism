document.addEventListener("contextmenu", event => {
  const image = event.target?.closest?.("img");
  if (!event.altKey || !image) return;
  const url = image.currentSrc || image.src;
  if (!url || !/^https?:/i.test(url)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  chrome.runtime.sendMessage({type: "capture-image", url});
}, true);
