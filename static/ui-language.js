(() => {
  "use strict";

  const button = document.querySelector("[data-ui-language-switcher]");
  if (!button) return;

  button.addEventListener("click", () => {
    const nextLanguage = button.dataset.nextUiLang;
    if (nextLanguage !== "zh" && nextLanguage !== "en") return;

    document.cookie = [
      "murakami-corpus-ui-language=" + encodeURIComponent(nextLanguage),
      "Path=/",
      "Max-Age=" + (60 * 60 * 24 * 365),
      "SameSite=Lax",
    ].join("; ");
    window.location.reload();
  });
})();
