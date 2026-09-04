/**
 * Presentation-layer pure functions: phase labels, time formatting, and URL sanitizing.
 *
 * These functions turn server fields into human-readable text without making requests
 * or holding state. This module is the console's source of truth for phase wording
 * and polling intervals.
 */

/** Phases that are still progressing and deserve closer attention in list views. */
export const ACTIVE_PHASES = new Set(["Pending", "Building", "Deploying", "Deleting"]);

// Poll faster while deployments progress, but keep polling when idle: another session
// may create a deployment. Checking only currently active rows would prevent new rows
// from ever appearing.
export const ACTIVE_POLL_MS = 5000;
export const IDLE_POLL_MS = 20000;
// The Sites control plane syncs a database snapshot every 2 seconds. The server computes
// this age at response time, so browser-clock skew cannot affect it. After 15 missed
// cycles, the snapshot is stale rather than merely unchanged.
export const SNAPSHOT_STALE_SECONDS = 30;

export function phaseLabel(phase: string, t: Translator): string {
  switch (phase) {
    case "Pending": return t("Pending");
    case "Building": return t("Building");
    case "Deploying": return t("Deploying");
    case "Running": return t("Running");
    case "Failed": return t("Failed");
    case "Deleting": return t("Deleting");
    case "Deleted": return t("Deleted");
    default: return phase || t("Unknown");
  }
}

export function runtimeLabel(runtime: string | null | undefined, t: Translator): string {
  switch (runtime) {
    case "Active": return t("Active replicas");
    case "Dormant": return t("Dormant");
    case "Unknown": return t("Scaling state not reported");
    default: return t("Unknown scaling state");
  }
}

export function relativeTime(
  value: string | null | undefined,
  locale: LocaleTag,
  t: Translator,
): string {
  if (!value) return t("Unknown time");
  const stamp = new Date(value).getTime();
  if (Number.isNaN(stamp)) return t("Unknown time");
  const seconds = Math.max(0, Math.round((Date.now() - stamp) / 1000));
  if (seconds < 60) return t("just now");
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return t(minutes === 1 ? "{count} minute ago" : "{count} minutes ago", { count: minutes });
  }
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    return t(hours === 1 ? "{count} hour ago" : "{count} hours ago", { count: hours });
  }
  return new Date(stamp).toLocaleDateString(locale, {
    year: "numeric",
    month: "numeric",
    day: "numeric",
  });
}

export function formatAge(seconds: number, t: Translator): string {
  if (seconds < 60) {
    const count = Math.round(seconds);
    return t(count === 1 ? "{count} second" : "{count} seconds", { count });
  }
  if (seconds < 3600) {
    const count = Math.floor(seconds / 60);
    return t(count === 1 ? "{count} minute" : "{count} minutes", { count });
  }
  const count = Math.floor(seconds / 3600);
  return t(count === 1 ? "{count} hour" : "{count} hours", { count });
}

/** Distinguish a missing server value from zero; the meanings are completely different. */
export function formatCount(value: number | undefined, t: Translator): string {
  return value === undefined ? t("Not reported") : String(value);
}

export function formatSeconds(value: number | null | undefined, t: Translator): string {
  if (value === null) return t("never succeeded");
  if (value === undefined) return t("Not reported");
  return t("{count} seconds ago", { count: value.toFixed(1) });
}

/**
 * Sanitize site URLs: allow only http and https, and return null for everything else.
 *
 * 🔴 This deployment-record field is **externally controlled text**. A `javascript:`
 * URL in `<a href>` becomes click-triggered XSS on a page holding the administrator
 * token. Relative URLs are also rejected because management links must point to the
 * site itself rather than routes belonging to the console.
 * AI-LOCK: Do not widen this whitelist for private-network schemes.
 */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.toString();
}

/**
 * Show the result probed by the control plane itself—not Kubernetes readiness and not
 * a tenant's self-report. If ok=false, the label must say failed even when phase=Running.
 */
export function verificationLabel(
  verification: { ok: boolean; httpStatus?: number | null; error?: string | null }
    | null
    | undefined,
  t: Translator,
): string {
  if (!verification) return t("No verification evidence in aggregate view");
  if (verification.ok) return t("Passed (HTTP {status})", { status: verification.httpStatus ?? "?" });
  if (verification.httpStatus) return t("Failed (HTTP {status})", { status: verification.httpStatus });
  return t("Failed ({reason})", {
    reason: verification.error || t("control plane could not reach the site"),
  });
}

/** Truncate digest fields for table layouts; full SHA-256 hex values are too wide. */
export function shortDigest(value: string | null | undefined): string {
  if (!value) return "—";
  return value.length > 16 ? `${value.slice(0, 16)}…` : value;
}
import type { LocaleTag, TranslationKey, TranslationValues } from "./i18n";

export type Translator = (
  key: TranslationKey,
  values?: TranslationValues,
) => string;
