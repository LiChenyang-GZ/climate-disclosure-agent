"use client";

import { SalesforceConnectForm } from "@/components/salesforce-connect-form";
import { useUser } from "@/components/user-context";
import type { SalesforceStatus } from "@/lib/api";
import { useLoad } from "@/lib/use-load";

export default function SettingsPage() {
  const { handle, me } = useUser();
  const status = useLoad<SalesforceStatus>("/integrations/salesforce", handle);

  if (me === null || status.state === "loading") {
    return <div aria-busy="true" className="h-24 animate-pulse rounded bg-slate-200" />;
  }
  if (me.role !== "lead") {
    return (
      <section className="space-y-4">
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-slate-600">Only a team lead can change settings.</p>
      </section>
    );
  }
  if (status.state === "error") {
    return (
      <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-red-900">
        <p>Couldn&apos;t load the settings.</p>
        <button className="mt-2 underline" onClick={status.reload}>
          Retry
        </button>
      </div>
    );
  }

  const current = status.data;
  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-semibold">Settings</h1>
      <p data-testid="salesforce-state" className="text-sm text-slate-600">
        {current.configured
          ? `Salesforce: connected to ${current.instance_url}${
              current.edition ? ` (${current.edition})` : ""
            }.`
          : "Salesforce: not connected."}
      </p>
      <SalesforceConnectForm
        initialUrl={current.instance_url ?? ""}
        onConnected={status.reload}
      />
    </section>
  );
}
