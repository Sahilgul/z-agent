import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./en/translation.json";

/** i18n-ready scaffold: English-only at launch, but the layer is in place
 *  so a second locale is a drop-in (add a `es/translation.json` + lng entry).
 *  No server extraction tooling — strings are authored inline. */
void i18n.use(initReactI18next).init({
  resources: { en: { translation: en } },
  lng: "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export default i18n;
export { useTranslation as useAppTranslation } from "react-i18next";
