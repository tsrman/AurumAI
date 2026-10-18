import { getServerRuntimeConfig } from "@/lib/runtime-config.server";
import { useTranslations } from "next-intl";

export default function AboutPage() {
  const t = useTranslations("about");
  const { sourceCodeUrl } = getServerRuntimeConfig();
  return (
    <main className="mx-auto w-full max-w-3xl space-y-5 p-8">
      <h1 className="text-3xl font-semibold">AurumAI</h1>
      <p>{t("description")}</p>
      <p>{t("copyright")}</p>
      <p>{t("license")}</p>
      <p><a href="/LICENSE" className="underline">{t("licenseLink")}</a></p>
      {sourceCodeUrl ? (
        <p><a href={sourceCodeUrl} target="_blank" rel="noopener noreferrer" className="underline">{t("sourceCode")}</a></p>
      ) : (
        <p>{t("sourceIncluded")}</p>
      )}
      <p>{t("thirdParty")}</p>
      <p><a href="/login" className="underline">{t("returnToSignIn")}</a></p>
    </main>
  );
}
