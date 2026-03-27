document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("iframe").forEach(iframe => {
    const resize = () => {
      try {
        const h = iframe.contentDocument.documentElement.scrollHeight;
        if (h > 0) iframe.style.height = h + "px";
      } catch (_) {}
    };
    iframe.addEventListener("load", resize);
    if (iframe.contentDocument?.readyState === "complete") resize();
  });
});
