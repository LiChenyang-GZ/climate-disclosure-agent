"use client";

import { createContext, useCallback, useContext, useState } from "react";

import type { Me } from "@/lib/api";
import { useLoad } from "@/lib/use-load";
import { type Handle, USER_COOKIE } from "@/lib/users";

interface UserState {
  handle: Handle;
  /** null until /api/me has answered for the current handle. */
  me: Me | null;
  meFailed: boolean;
  reloadMe: () => void;
  switchUser: (handle: Handle) => void;
  flash: string | null;
  setFlash: (message: string | null) => void;
}

const UserContext = createContext<UserState | null>(null);

export function UserProvider({
  initialHandle,
  children,
}: {
  initialHandle: Handle;
  children: React.ReactNode;
}) {
  const [handle, setHandle] = useState<Handle>(initialHandle);
  const [flash, setFlash] = useState<string | null>(null);
  const me = useLoad<Me>("/me", handle);

  const switchUser = useCallback((next: Handle) => {
    document.cookie = `${USER_COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
    setFlash(null);
    setHandle(next);
  }, []);

  return (
    <UserContext.Provider
      value={{
        handle,
        me: me.state === "ok" ? me.data : null,
        meFailed: me.state === "error",
        reloadMe: me.reload,
        switchUser,
        flash,
        setFlash,
      }}
    >
      {children}
    </UserContext.Provider>
  );
}

export function useUser(): UserState {
  const value = useContext(UserContext);
  if (value === null) throw new Error("useUser outside UserProvider");
  return value;
}
