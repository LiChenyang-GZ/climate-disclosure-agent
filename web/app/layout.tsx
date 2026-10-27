import type { Metadata } from "next";
import { cookies } from "next/headers";

import { TopBar } from "@/components/top-bar";
import { UserProvider } from "@/components/user-context";
import { USER_COOKIE, toHandle } from "@/lib/users";

import "./globals.css";

export const metadata: Metadata = {
  title: "Climate Disclosure Agent",
  description: "Locates evidence for Australian climate disclosure requirements.",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const handle = toHandle((await cookies()).get(USER_COOKIE)?.value);
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <UserProvider initialHandle={handle}>
          <TopBar />
          <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>
        </UserProvider>
      </body>
    </html>
  );
}
