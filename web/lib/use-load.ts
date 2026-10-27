"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "./api";

export type Load<T> =
  | { state: "loading" }
  | { state: "error"; error: ApiError }
  | { state: "ok"; data: T };

/** GETs `path` whenever it or `key` changes (key = current demo user). */
export function useLoad<T>(path: string, key: string): Load<T> & { reload: () => void } {
  const [attempt, setAttempt] = useState(0);
  const request = `${attempt}|${key}|${path}`;
  // A result belongs to one request; anything older reads as loading.
  const [result, setResult] = useState<{ request: string; load: Load<T> } | null>(null);

  useEffect(() => {
    let live = true;
    api<T>(path)
      .then((data) => live && setResult({ request, load: { state: "ok", data } }))
      .catch((error: unknown) => {
        if (!live) return;
        const failure =
          error instanceof ApiError ? error : new ApiError(0, "unknown", String(error));
        setResult({ request, load: { state: "error", error: failure } });
      });
    return () => {
      live = false;
    };
  }, [path, request]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const load: Load<T> = result?.request === request ? result.load : { state: "loading" };
  return { ...load, reload };
}
