"use client";

import { useCallback, useEffect, useState } from "react";

import { ImportDialog } from "@/components/import-dialog";
import { ResyncPanel } from "@/components/resync-panel";
import {
  ApiError,
  api,
  type DeviceCode,
  type MicrosoftStatus,
  type PackDetail,
  type PollState,
} from "@/lib/api";

type Notice = "cancelled" | "expired" | "not_configured" | "failed" | null;

const NOTICES: Record<Exclude<Notice, null>, string> = {
  cancelled: "Microsoft connection was cancelled.",
  expired: "Your Microsoft connection expired. Reconnect.",
  not_configured: "Microsoft sign-in isn't set up. Set CDA_MS_CLIENT_ID and restart the backend.",
  failed: "Couldn't connect to Microsoft. Try again.",
};

/**
 * Lead-only OneDrive / SharePoint panel on the pack page (X2): device-code sign-in, then
 * import and re-sync. The connection lives in API memory, so a restart asks to reconnect.
 */
export function MicrosoftPanel({ pack, onChanged }: { pack: PackDetail; onChanged: () => void }) {
  const [status, setStatus] = useState<MicrosoftStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [code, setCode] = useState<DeviceCode | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [starting, setStarting] = useState(false);
  const [view, setView] = useState<"none" | "import" | "sync">("none");

  useEffect(() => {
    let live = true;
    api<MicrosoftStatus>("/integrations/microsoft")
      .then((value) => live && setStatus(value))
      .catch(() => live && setUnavailable(true));
    return () => {
      live = false;
    };
  }, []);

  // Poll at Microsoft's interval until the user finishes, declines or the code runs out.
  useEffect(() => {
    if (code === null) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const result = await api<{ state: PollState; account?: string }>(
          "/integrations/microsoft/poll",
          { method: "POST" },
        );
        if (!live) return;
        if (result.state === "pending") {
          timer = setTimeout(tick, code.interval * 1000);
          return;
        }
        setCode(null);
        if (result.state === "connected") {
          setStatus({ configured: true, connected: true, account: result.account ?? null });
        } else {
          setNotice(result.state);
        }
      } catch {
        if (!live) return;
        setCode(null);
        setNotice("failed");
      }
    };
    timer = setTimeout(tick, code.interval * 1000);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [code]);

  const expired = useCallback(() => {
    setView("none");
    setStatus((current) => current && { ...current, connected: false, account: null });
    setNotice("expired");
  }, []);

  async function connect() {
    setStarting(true);
    setNotice(null);
    try {
      setCode(await api<DeviceCode>("/integrations/microsoft/device-code", { method: "POST" }));
    } catch (error) {
      setNotice(
        error instanceof ApiError && error.code === "microsoft_not_configured"
          ? "not_configured"
          : "failed",
      );
    } finally {
      setStarting(false);
    }
  }

  async function disconnect() {
    try {
      await api("/integrations/microsoft", { method: "DELETE" });
    } catch {
      // The connection is in API memory only; treat a failed call as disconnected here too.
    }
    setView("none");
    setNotice(null);
    setStatus((current) => current && { ...current, connected: false, account: null });
  }

  if (unavailable || status === null) return null;

  const imported = pack.documents.some((d) => d.source);
  const parsing = pack.status === "parsing";
  const button =
    "rounded border border-slate-300 px-3 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <section aria-label="OneDrive and SharePoint" className="space-y-3 rounded border border-slate-200 bg-white p-4">
      {!status.configured ? (
        <p className="text-sm text-slate-500">
          Importing from OneDrive or SharePoint isn&apos;t set up (CDA_MS_CLIENT_ID).
        </p>
      ) : code ? (
        <div role="status" className="space-y-2 text-sm">
          <p>
            Open{" "}
            <a href={code.verification_uri} target="_blank" rel="noopener noreferrer" className="underline">
              {code.verification_uri}
            </a>{" "}
            and enter the code{" "}
            <strong data-testid="device-code" className="font-mono text-base">
              {code.user_code}
            </strong>
          </p>
          <p className="text-slate-600">Waiting for Microsoft sign-in…</p>
          <button className={button} onClick={() => setCode(null)}>
            Cancel
          </button>
        </div>
      ) : status.connected ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span>Connected as {status.account}</span>
          <button
            className={button}
            disabled={parsing}
            title={parsing ? "Wait for parsing to finish" : undefined}
            onClick={() => setView("import")}
          >
            Import from SharePoint
          </button>
          {imported && (
            <button className={button} onClick={() => setView(view === "sync" ? "none" : "sync")}>
              Re-sync
            </button>
          )}
          <button className="ml-auto text-slate-500 underline" onClick={disconnect}>
            Disconnect
          </button>
        </div>
      ) : (
        <div className="space-y-2 text-sm">
          {notice && (
            <p
              role={notice === "cancelled" ? "status" : "alert"}
              className={notice === "cancelled" ? "text-slate-700" : "text-red-800"}
            >
              {NOTICES[notice]}
            </p>
          )}
          <button className={button} disabled={starting} onClick={connect}>
            {notice === "expired" ? "Reconnect" : "Connect Microsoft"}
          </button>
        </div>
      )}
      {view === "sync" && status.connected && (
        <ResyncPanel packId={pack.id} onChanged={onChanged} onExpired={expired} />
      )}
      {view === "import" && status.connected && (
        <ImportDialog
          pack={pack}
          onImported={onChanged}
          onExpired={expired}
          onClose={() => setView("none")}
        />
      )}
    </section>
  );
}
