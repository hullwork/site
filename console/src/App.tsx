import {
  Boxes,
  Hammer,
  LayoutDashboard,
  ChartNoAxesCombined,
  LogOut,
  ShieldCheck,
  Store,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, SitesApiError, USE_MOCK, type AuthMethods } from "./api";
import BuildsView from "./components/BuildsView";
import DeploymentsView from "./components/DeploymentsView";
import LoginView from "./components/LoginView";
import MerchantsView from "./components/MerchantsView";
import OverviewView from "./components/OverviewView";
import MonitoringView from "./components/MonitoringView";
import TenantsView from "./components/TenantsView";
import { LanguageSwitcher, useI18n, type TranslationKey } from "./i18n";

export type ConsoleTab = "overview" | "monitoring" | "merchants" | "tenants" | "deployments" | "builds";

/**
* Console only puts the page ID into the hash; filters and credentials never enter the URL.
* The console holds no credential at all - the session is an HttpOnly cookie the browser
* cannot read (see api.ts). All page data comes from the same-origin sites-api; the browser
* never talks to Kubernetes, the database or the registry directly.
 */

const TABS: {
  id: ConsoleTab;
  label: TranslationKey;
  description: TranslationKey;
  icon: React.ReactNode;
}[] = [
  { id: "overview", label: "Overview", description: "health and risks", icon: <LayoutDashboard size={18} /> },
  { id: "monitoring", label: "Monitor", description: "Cluster and application metrics", icon: <ChartNoAxesCombined size={18} /> },
  { id: "merchants", label: "Merchants", description: "Organizations and total quotas", icon: <Store size={18} /> },
  { id: "tenants", label: "Tenants", description: "Identity and deployment quotas", icon: <Users size={18} /> },
  { id: "deployments", label: "Deployments", description: "Runtime status and verification", icon: <Boxes size={18} /> },
  { id: "builds", label: "Builds", description: "Artifacts and image references", icon: <Hammer size={18} /> },
];

function tabFromHash(): ConsoleTab {
  const value = window.location.hash.replace(/^#\/?/, "");
  return TABS.some((item) => item.id === value) ? value as ConsoleTab : "overview";
}

export default function App() {
  const { t } = useI18n();
  const [authenticated, setAuthenticated] = useState(false);
  const [methods, setMethods] = useState<AuthMethods | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [tab, setTab] = useState<ConsoleTab>(tabFromHash);
  const [merchantFilter, setMerchantFilter] = useState("");
  const [error, setError] = useState("");
  const [sessionNoteKey, setSessionNoteKey] = useState<TranslationKey | null>(null);

  const navigate = useCallback((next: ConsoleTab) => {
    setTab(next);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${next}`);
    window.scrollTo({ top: 0, behavior: "auto" });
    window.requestAnimationFrame(() => document.querySelector<HTMLElement>("#main-content h2")?.focus());
  }, []);

  useEffect(() => {
    const handleHashChange = () => {
      setTab(tabFromHash());
      window.scrollTo({ top: 0, behavior: "auto" });
      window.requestAnimationFrame(() => document.querySelector<HTMLElement>("#main-content h2")?.focus());
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => { window.scrollTo({ top: 0, behavior: "auto" }); }, []);

  const logout = useCallback(() => {
    void api.logout().catch(() => undefined);
    setAuthenticated(false);
    setError("");
    setMerchantFilter("");
    navigate("overview");
    setSessionNoteKey("Logged out.");
  }, [navigate]);

  // One probe decides the whole screen: a usable session, or the set of doors this control
  // plane actually has. Asking for the methods first would flash a login form at someone who
  // is already signed in.
  useEffect(() => {
    if (authenticated) return;
    let cancelled = false;
    void api.session()
      .then(() => { if (!cancelled) setAuthenticated(true); })
      .catch(() => api.authMethods().then((available) => {
        if (!cancelled) setMethods(available);
      }).catch(() => undefined))
      .finally(() => { if (!cancelled) setCheckingSession(false); });
    return () => { cancelled = true; };
  }, [authenticated]);

  const handleError = useCallback((cause: unknown) => {
    if (cause instanceof SitesApiError && (cause.status === 401 || cause.status === 403)) {
      logout();
      return;
    }
    setError(cause instanceof Error ? cause.message : String(cause));
  }, [logout]);

  useEffect(() => { setError(""); }, [tab]);

  if (checkingSession) {
    return <p className="session-note" role="status">{t("Confirming admin session...")}</p>;
  }

  if (!authenticated) {
    return (
      <>
        {sessionNoteKey ? <p className="session-note" role="status">{t(sessionNoteKey)}</p> : null}
        <LoginView
          methods={methods}
          onAuthenticated={() => {
            setAuthenticated(true);
            setSessionNoteKey(null);
          }}
        />
      </>
    );
  }

  const openTenantsFor = (merchantId: string) => {
    setMerchantFilter(merchantId);
    navigate("tenants");
  };

  return (
    <div className="app">
      <a className="skip-link" href="#main-content">{t("Skip to main content")}</a>
      <aside className="app-sidebar">
        <header className="app-header">
          <span className="brand-mark" aria-hidden="true"><ShieldCheck size={19} /></span>
          <div>
            <h1 className="app-title">Sites</h1>
            <p className="app-subtitle">{t("Deployment control plane")}{USE_MOCK ? ` · ${t("Mock")}` : ` · ${t("Admin")}`}</p>
          </div>
        </header>

        <nav className="tabs" aria-label={t("Console navigation")}>
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tab ${tab === item.id ? "is-active" : ""}`}
              aria-label={`${t(item.label)}: ${t(item.description)}`}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => navigate(item.id)}
            >
              <span className="tab-icon" aria-hidden="true">{item.icon}</span>
              <span className="tab-copy">
                <strong>{t(item.label)}</strong>
                <small>{t(item.description)}</small>
              </span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <LanguageSwitcher className="sidebar-language" />
          <span className="admin-session"><span className="status-dot" />{t("Administrator session")}</span>
          <button
            type="button"
            className="button sidebar-logout"
            aria-label={t("Exit administrator session")}
            onClick={logout}
          >
            <LogOut size={16} /><span>{t("Log out")}</span>
          </button>
        </div>
      </aside>

      <main className="app-main" id="main-content" tabIndex={-1}>
        {error ? (
          <div className="banner banner-bad global-banner" role="alert">
            <TriangleAlert size={18} aria-hidden="true" />
            <div><strong>{t("Request not completed")}</strong><p>{error}</p></div>
            <button type="button" className="icon-button" aria-label={t("Close prompt")} onClick={() => setError("")}>
              <X size={16} />
            </button>
          </div>
        ) : null}

        {tab === "overview" ? <OverviewView onError={handleError} onNavigate={navigate} /> : null}
        {tab === "monitoring" ? <MonitoringView onError={handleError} /> : null}
        {tab === "merchants" ? <MerchantsView onError={handleError} onInspectTenants={openTenantsFor} /> : null}
        {tab === "tenants" ? (
          <TenantsView merchantFilter={merchantFilter} onMerchantFilterChange={setMerchantFilter} onError={handleError} />
        ) : null}
        {tab === "deployments" ? (
          <DeploymentsView merchantFilter={merchantFilter} onMerchantFilterChange={setMerchantFilter} onError={handleError} />
        ) : null}
        {tab === "builds" ? (
          <BuildsView merchantFilter={merchantFilter} onMerchantFilterChange={setMerchantFilter} onError={handleError} />
        ) : null}
      </main>
    </div>
  );
}
