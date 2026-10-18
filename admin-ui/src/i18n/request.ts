import { cookies, headers } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { LOCALE_COOKIE, negotiateLocale } from "./locale";

// Locale comes from the cookie set by the language switcher or the browser's
// Accept-Language header; URLs stay locale-free.
export default getRequestConfig(async () => {
  const [cookieStore, headerStore] = await Promise.all([cookies(), headers()]);
  const locale = negotiateLocale(
    cookieStore.get(LOCALE_COOKIE)?.value,
    headerStore.get("accept-language"),
  );
  return {
    locale,
    // `locale` is validated against the allowlist before reaching the import.
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: "UTC",
  };
});
