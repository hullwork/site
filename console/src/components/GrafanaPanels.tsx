import { useState } from "react";
import { useI18n } from "../i18n";
import type { GrafanaCapability } from "../types";

/**
 * Embedded Grafana panels, rendered as same-origin iframes.
 *
 * 🔴 Why an iframe to our own origin rather than to Grafana: a cross-origin
 * frame would need `frame-src` opened in the console CSP, `allow_embedding` and
 * `cookie_samesite=none` on the Grafana side, and would leave a Grafana
 * credential in the browser. sites-api proxies the panel instead and holds the
 * service-account token server-side, so the console CSP is untouched.
 *
 * 🔴 Why the URL is assembled here from a fixed catalog and a fixed range list:
 * whatever this component puts in the query string is what sites-api forwards to
 * Grafana. A free-form range or panel id would hand Grafana URL construction to
 * the page. Panel ids come from `/v1/admin/health`, which reads them from the
 * shipped dashboard; the range is one of the literals below.
 *
 * Rendering nothing is not the access decision: sites-api answers 403 for a
 * non-administrator and 404 when no Grafana is configured, on every request.
 */

const RANGES = ["6h", "24h", "7d"] as const;
type RangeId = (typeof RANGES)[number];

export default function GrafanaPanels({ grafana }: { grafana: GrafanaCapability }) {
  const { t } = useI18n();
  const [range, setRange] = useState<RangeId>("6h");
  const panels = grafana.panels ?? [];
  const route = grafana.route ?? "/grafana/";
  const uid = grafana.dashboardUid ?? "";

  if (!panels.length || !uid) return null;

  return (
    <section className="page-section" aria-label={t("Grafana panels")}>
      <header className="grafana-header">
        <div>
          <h3>{t("Grafana panels")}</h3>
          <p className="grafana-note">
            {t("Live panels from the operator's Grafana, proxied on this origin. Read-only.")}
          </p>
          <p className="grafana-note">
            {t("These panels are platform-wide. The metrics behind them carry no tenant dimension, so nothing here can be scoped to one tenant.")}
          </p>
        </div>
        <div className="segmented-control" aria-label={t("Time range")}>
          {RANGES.map((item) => (
            <button
              type="button"
              key={item}
              className={range === item ? "is-active" : ""}
              aria-pressed={range === item}
              onClick={() => setRange(item)}
            >
              {item}
            </button>
          ))}
        </div>
      </header>
      <div className="grafana-grid">
        {panels.map((panel) => {
          const query = new URLSearchParams({
            panelId: String(panel.id),
            from: `now-${range}`,
            to: "now",
            theme: "light",
          });
          return (
            <iframe
              key={panel.id}
              className="grafana-frame"
              title={t("{title} panel", { title: panel.title })}
              src={`${route}d-solo/${encodeURIComponent(uid)}?${query.toString()}`}
              loading="lazy"
              // Nothing in a chart needs top-level navigation, popups or form
              // submission; the frame only has to run Grafana's bundle.
              sandbox="allow-scripts allow-same-origin"
              referrerPolicy="no-referrer"
            />
          );
        })}
      </div>
    </section>
  );
}
