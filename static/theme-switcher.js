(() => {
  "use strict";

  const storageKey = "murakami-corpus-theme";
  const button = document.querySelector("[data-theme-switcher]");
  if (!button) return;

  // 按钮首屏文字由 Jinja 渲染。data 属性只是主题切换时的文案来源，
  // 因此即使静态资源缓存与模板短暂不同步，也绝不把按钮清空。
  const isEnglishUi = document.documentElement.lang === "en";
  const fallbackLabels = isEnglishUi
    ? {
        vintage: "Original Night",
        classic: "Vintage Paper",
        toVintage: "Switch to the original night theme",
        toClassic: "Switch to the vintage paper theme",
      }
    : {
        vintage: "原版夜色",
        classic: "复古纸页",
        toVintage: "切换到原版夜色主题",
        toClassic: "切换到复古纸页主题",
      };
  const labels = {
    vintage: button.dataset.themeVintageLabel?.trim() || button.textContent.trim() || fallbackLabels.vintage,
    classic: button.dataset.themeClassicLabel?.trim() || fallbackLabels.classic,
    toVintage: button.dataset.themeToVintage?.trim() || fallbackLabels.toVintage,
    toClassic: button.dataset.themeToClassic?.trim() || fallbackLabels.toClassic,
  };

  const applyTheme = (theme) => {
    const isVintage = theme === "vintage";
    document.body.classList.toggle("theme-vintage-paper", isVintage);
    const nextThemeLabel = isVintage ? labels.vintage : labels.classic;
    const nextThemeTitle = isVintage ? labels.toVintage : labels.toClassic;
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
