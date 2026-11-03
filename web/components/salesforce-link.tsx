"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError, api, type PackDetail, type SalesforceAccount, type SalesforceStatus } from "@/lib/api";
import { useLoad } from "@/lib/use-load";

import { useUser } from "./user-context";

/** The pack's linked Salesforce Account (D85); only a lead can link or unlink. */
export function SalesforceLink({
  pack,
  isLead,
  onChanged,
}: {
  pack: PackDetail;
  isLead: boolean;
  onChanged: () => void;
}) {
  const account = pack.salesforce_account;
  if (account) {
    return <Linked pack={pack} account={account} isLead={isLead} onChanged={onChanged} />;
  }
  return isLead ? <LinkAccount packId={pack.id} onChanged={onChanged} /> : null;
}

function Linked({
  pack,
  account,
  isLead,
  onChanged,
}: {
  pack: PackDetail;
  account: SalesforceAccount;
  isLead: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function unlink() {
    setBusy(true);
    setFailed(false);
    try {
      await api(`/packs/${pack.id}/salesforce-account`, { method: "DELETE" });
      onChanged();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="salesforce-link" className="flex flex-wrap items-center gap-3 text-sm">
      <span>
        Salesforce account: <strong>{account.name}</strong>
      </span>
      {isLead && (
        <button className="underline disabled:opacity-50" onClick={unlink} disabled={busy}>
          Unlink
        </button>
      )}
      {failed && (
        <span role="alert" className="text-red-700">
          Couldn&apos;t unlink the account. Try again.
        </span>
      )}
    </div>
  );
}

function searchFailure(error: unknown): string {
  if (error instanceof ApiError && error.code === "salesforce_limit") return error.message;
  if (error instanceof ApiError && error.code === "salesforce_rejected") {
    return "Salesforce rejected the credentials. Check them in Settings.";
  }
  return "Couldn't search Salesforce. Try again.";
}

function LinkAccount({ packId, onChanged }: { packId: string; onChanged: () => void }) {
  const { handle } = useUser();
  const status = useLoad<SalesforceStatus>("/integrations/salesforce", handle);
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const [results, setResults] = useState<SalesforceAccount[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [linking, setLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (status.state !== "ok") return null;
  if (!status.data.configured) {
    return (
      <p data-testid="salesforce-link" className="text-sm text-slate-600">
        Connect Salesforce in{" "}
        <Link href="/settings" className="underline">
          Settings
        </Link>{" "}
        first
      </p>
    );
  }
  if (!open) {
    return (
      <div data-testid="salesforce-link">
        <button className="text-sm underline" onClick={() => setOpen(true)}>
          Link Salesforce account
        </button>
      </div>
    );
  }

  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (term.trim() === "" || searching) return;
    setSearching(true);
    setError(null);
    try {
      setResults(
        await api<SalesforceAccount[]>(
          `/integrations/salesforce/accounts?q=${encodeURIComponent(term.trim())}`,
        ),
      );
    } catch (failure) {
      setResults(null);
      setError(searchFailure(failure));
    } finally {
      setSearching(false);
    }
  }

  async function pick(account: SalesforceAccount) {
    setLinking(true);
    setError(null);
    try {
      await api(`/packs/${packId}/salesforce-account`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(account),
      });
      onChanged();
    } catch {
      setError("Couldn't link the account. Try again.");
      setLinking(false);
    }
  }

  return (
    <div data-testid="salesforce-link" className="max-w-lg space-y-2 rounded border border-slate-200 bg-white p-4 text-sm">
      <form onSubmit={search} className="flex gap-2">
        <input
          aria-label="Search Salesforce accounts"
          placeholder="Account name"
          className="flex-1 rounded border border-slate-300 px-3 py-1"
          value={term}
          onChange={(event) => setTerm(event.target.value)}
        />
        <button
          type="submit"
          disabled={term.trim() === "" || searching}
          className="rounded bg-slate-900 px-3 py-1 font-medium text-white disabled:bg-slate-300"
        >
          {searching ? "Searching…" : "Search"}
        </button>
        <button type="button" className="px-2" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </form>
      {error && (
        <p role="alert" className="text-red-700">
          {error}
        </p>
      )}
      {results !== null && results.length === 0 && (
        <p>No matching account. Create it in Salesforce first.</p>
      )}
      {results !== null && results.length > 0 && (
        <ul aria-label="Matching accounts" className="space-y-1">
          {results.map((account) => (
            <li key={account.id}>
              <button
                className="underline disabled:opacity-50"
                disabled={linking}
                onClick={() => pick(account)}
              >
                {account.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
