let selectionTimer;

document.addEventListener("selectionchange", () => {
  clearTimeout(selectionTimer);
  selectionTimer = setTimeout(() => {
    const text = document.getSelection().toString();
    if (text) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  }, 300);
});
