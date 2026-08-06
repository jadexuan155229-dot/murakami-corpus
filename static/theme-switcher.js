(() => {
  "use strict";

  const storageKey = "murakami-corpus-theme";
  const button = document.querySelector("[data-theme-switcher]");
  if (!button) return;

  const applyTheme = (theme) => {
    const isVintage = theme === "vintage";
    document.body.classList.toggle("theme-vintage-paper", isVintage);
    const nextThemeLabel = isVintage ? "原版夜色" : "复古纸页";
    const nextThemeTitle = isVintage ? "切换到原版夜色主题" : "切换到复古纸页主题";
    button.textContent = nextThemeLabel;
    button.title = nextThemeTitle;
    button.setAttribute("aria-label", nextThemeTitle);
    button.setAttribute("aria-pressed", String(isVintage));
  };

  const currentTheme = document.body.classList.contains("theme-vintage-paper")
    ? "vintage"
    : "classic";
  applyTheme(currentTheme);

  button.addEventListener("click", () => {
    const nextTheme = document.body.classList.contains("theme-vintage-paper")
      ? "classic"
      : "vintage";
    applyTheme(nextTheme);
    try {
      localStorage.setItem(storageKey, nextTheme);
    } catch (_) {
      // 隐私模式或受限环境下仍保留本次页面的视觉切换。
    }
  });
})();
