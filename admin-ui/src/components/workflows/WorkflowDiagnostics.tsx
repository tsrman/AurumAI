import type { WorkflowRunDiagnostic } from "@/lib/api";
import { useTranslations } from "next-intl";

export function WorkflowDiagnostics({ diagnostic }: { diagnostic: WorkflowRunDiagnostic }) {
  const t = useTranslations("workflows.diagnostics");
  return (
    <section aria-label={t("label")} className="space-y-3 rounded-md border p-4">
      <h2 className="font-semibold">{t("run", { id: diagnostic.id })}</h2>
      <p>{t("status", { status: diagnostic.status })}</p>
      {diagnostic.errorCode ? <p>{t("error", { code: diagnostic.errorCode })}</p> : null}
      {diagnostic.nodes.map((node) => (
        <article key={node.phaseKey} className="rounded border p-3">
          <h3 className="font-medium">{node.agentName} — {node.status}</h3>
          <p>{t("attempts", { count: node.attemptCount })}</p>
          {node.outputExpired ? (
            <p>{t("purged")}</p>
          ) : node.output ? (
            <pre className="mt-2 whitespace-pre-wrap">{node.output}</pre>
          ) : null}
          {node.internalError && !node.outputExpired ? <pre>{node.internalError}</pre> : null}
        </article>
      ))}
    </section>
  );
}
