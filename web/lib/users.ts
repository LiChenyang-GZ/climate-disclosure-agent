export const USERS = ["sarah", "alex"] as const;
export type Handle = (typeof USERS)[number];
export const USER_COOKIE = "cda_user";

export function toHandle(value: string | undefined | null): Handle {
  return USERS.includes(value as Handle) ? (value as Handle) : "sarah";
}
