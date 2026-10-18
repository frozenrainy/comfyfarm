// UI language. The English text is the key: t("Queue") looks it up in the chosen language's table and falls back to
// the English itself, so a string nobody translated yet still reads fine. Messages from the server (errors) stay in
// English on purpose — they match the logs and the documentation word for word.
import ko from "./lang/ko.js";

const TABLES = { ko };
export const LANGS = [["auto", "Auto (browser language)"], ["en", "English"], ["ko", "한국어"]];
const KEY = "comfyfarm.lang";

function resolve(pref) {
  if (pref === "en" || TABLES[pref]) return pref;
  const langs = navigator.languages?.length ? navigator.languages : [navigator.language || "en"];
  for (const l of langs) {
    const base = String(l).toLowerCase().split("-")[0];
    if (base === "en" || TABLES[base]) return base;
  }
  return "en";
}

let lang = resolve(localStorage.getItem(KEY) || "auto");

export function currentLang() { return lang; }
export function locale() { return lang === "en" ? undefined : lang; }       // for toLocale*String

// The preference ("auto" | "en" | "ko"): kept in localStorage for the first paint, and in a cookie for the login
// page, which the gateway renders before anyone is known.
export function setLang(pref) {
  localStorage.setItem(KEY, pref);
  document.cookie = `comfyfarm_lang=${pref}; path=/; max-age=31536000; samesite=lax`;
  const next = resolve(pref);
  const changed = next !== lang;
  lang = next;
  document.documentElement.lang = lang;
  translateStatic();
  return changed;
}

export function t(text, vars) {
  let s = TABLES[lang]?.[text] ?? text;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return s;
}

// Static markup (index.html) carries its English text in data-i18n / data-i18n-title.
export function translateStatic(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
}
document.documentElement.lang = lang;
