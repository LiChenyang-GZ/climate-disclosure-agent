"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";

type Outcome = { ok: true; message: string } | { ok: false; message: string } | null;

function failureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "salesforce_rejected") return "Salesforce rejected the credentials.";
    if (error.code === "invalid_instance_url" || error.code === "salesforce_limit") {
      return error.message;
    }
    if (error.code === "forbidden") return "Only a team lead can connect Salesforce.";
  }
  return "Couldn't reach Salesforce. Try again.";
}

/**
 * Connect Salesforce (D85): tests a connected app's client credentials. The API keeps
 * them in memory only; the secret is cleared from the form once the test succeeds.
 */
export function SalesforceConnectForm({
  initialUrl,
  onConnected,
}: {
  initialUrl: string;
  onConnected: () => void;
}) {
  const [instanceUrl, setInstanceUrl] = useState(initialUrl);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [testing, setTesting] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  const complete = instanceUrl.trim() !== "" && clientId.trim() !== "" && clientSecret !== "";

  async function test(event: React.FormEvent) {
    event.preventDefault();
    if (!complete || testing) return;
    setTesting(true);
    setOutcome(null);
    try {
      const result = await api<{ connected: boolean; edition: string | null }>(
        "/integrations/salesforce/test",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            instance_url: instanceUrl.trim(),
            client_id: clientId.trim(),
            client_secret: clientSecret,
          }),
        },
      );
      setOutcome({
        ok: true,
        message: result.edition
          ? `Connected to Salesforce (${result.edition}).`
          : "Connected to Salesforce.",
      });
      setClientSecret("");
      onConnected();
    } catch (error) {
      setOutcome({ ok: false, message: failureMessage(error) });
    } finally {
      setTesting(false);
    }
  }

  return (
    <form
      aria-labelledby="salesforce-title"
      onSubmit={test}
      className="max-w-lg space-y-4 rounded border border-slate-200 bg-white p-6"
    >
      <h2 id="salesforce-title" className="text-lg font-semibold">
        Connect Salesforce
      </h2>
      <p className="text-sm text-slate-600">
        Use a connected app with the client credentials flow. The credentials are kept in the
        backend&apos;s memory only and are forgotten when it restarts.
      </p>
      {outcome?.ok === true && (
        <p role="status" className="rounded bg-emerald-50 p-2 text-sm text-emerald-900">
          {outcome.message}
        </p>
      )}
      {outcome?.ok === false && (
        <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
          {outcome.message}
        </p>
      )}
      <label className="block text-sm">
        <span className="mb-1 block font-medium text-slate-700">Instance URL</span>
        <input
          type="url"
          className="w-full rounded border border-slate-300 px-3 py-2"
          placeholder="https://yourcompany.my.salesforce.com"
          value={instanceUrl}
          onChange={(event) => setInstanceUrl(event.target.value)}
        />
      </label>
      <label className="block text-sm">
        <span className="mb-1 block font-medium text-slate-700">Client ID</span>
        <input
          className="w-full rounded border border-slate-300 px-3 py-2"
          autoComplete="off"
          value={clientId}
          onChange={(event) => setClientId(event.target.value)}
        />
      </label>
      <label className="block text-sm">
        <span className="mb-1 block font-medium text-slate-700">Client secret</span>
        <input
          type="password"
          className="w-full rounded border border-slate-300 px-3 py-2"
          autoComplete="off"
          value={clientSecret}
          onChange={(event) => setClientSecret(event.target.value)}
        />
      </label>
      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!complete || testing}
          aria-busy={testing}
          className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
      </div>
    </form>
  );
}
