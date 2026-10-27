"use client";

import Link from "next/link";

import { USERS, type Handle } from "@/lib/users";

import { useUser } from "./user-context";

export function TopBar() {
  const { handle, switchUser, flash, setFlash } = useUser();
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
        <Link href="/" className="text-lg font-semibold text-emerald-800">
          Climate Disclosure Agent
        </Link>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Signed in as
          <select
            aria-label="Demo user"
            className="rounded border border-slate-300 bg-white px-2 py-1 text-slate-900"
            value={handle}
            onChange={(event) => switchUser(event.target.value as Handle)}
          >
            {USERS.map((user) => (
              <option key={user} value={user}>
                {user}
              </option>
            ))}
          </select>
        </label>
      </div>
      {flash && (
        <div
          role="status"
          className="flex items-center justify-between bg-amber-100 px-6 py-2 text-sm text-amber-900"
        >
          <span>{flash}</span>
          <button className="underline" onClick={() => setFlash(null)}>
            Dismiss
          </button>
        </div>
      )}
    </header>
  );
}
