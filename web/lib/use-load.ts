"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "./api";

export type Load<T> =
  | { state: "loading" }
  | { state: "error"; error: ApiError }
  | { state: "ok"; data: T };

/**
 * GETs `path` whenever it or `key` changes (key = current demo user). `reload`
 * refetches but keeps showing the last result until the new one arrives.
 */
export function useLoad<T>(path: string, key: string): Load<T> & { reload: () => void } {
  const [attempt, setAttempt] = useState(0);
  const base = `${key}|${path}`;
  const request = `${attempt}|${base}`;
  // A result belongs to one user and path; a result for another reads as loading.
  const [result, setResult] = useState<{ base: string; load: Load<T> } | null>(null);

  useEffect(() => {
    let live = true;
    api<T>(path)
      .then((data) => live && setResult({ base, load: { state: "ok", data } }))
      .catch((error: unknown) => {
        if (!live) return;
        const failure =
          error instanceof ApiError ? error : new ApiError(0, "unknown", String(error));
        setResult({ base, load: { state: "error", error: failure } });
      });
    return () => {
      live = false;
    };
  }, [path, base, request]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const load: Load<T> = result?.base === base ? result.load : { state: "loading" };
  return { ...load, reload };
}
