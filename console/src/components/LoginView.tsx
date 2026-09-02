import { CircleAlert, KeyRound, LoaderCircle, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api, SitesApiError, USE_MOCK, type AuthMethods } from "../api";
import { LanguageSwitcher, useI18n } from "../i18n";

/**
* Login page. Two doors, and the control plane decides which ones exist:
* the identity provider (a full-page redirect into `/v1/auth/login`), and the break-glass
* local token, which is POSTed once and traded for an HttpOnly session cookie.
*
* 🔴 The form is a consequence of `GET /v1/auth/methods`, never the enforcement. Hiding it
* would leave `/v1/auth/local` reachable; that endpoint refuses on its own when local login
* is disabled, and the negative test drives it directly with no browser involved.
 */
export default function LoginView({
  methods,
  onAuthenticated,
}: {
  methods: AuthMethods | null;
  onAuthenticated: () => void;
}) {
  const { t } = useI18n();
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  // null means the probe itself failed, so nothing is known. Offer the local form: its
  // response carries the control plane's own reason, which beats this page guessing one.
  const enabled = methods ?? { oidc: false, localLogin: true };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) {
      setError(t("Please fill in the admin token."));
      return;
    }
    setPending(true);
    setError("");
    try {
      await api.localLogin(candidate);
      onAuthenticated();
    } catch (cause) {
      if (cause instanceof SitesApiError && cause.status === 403) {
        // 403 here is the switch, not the token: say so, or the operator spends the evening
        // checking a secret that was never read.
        setError(t("Local login is disabled on this control plane."));
      } else if (cause instanceof SitesApiError && cause.status === 401) {
        setError(t("This token is not accepted by the control plane (401/403). Check SITES_SERVICE_TOKEN and try again."));
      } else if (cause instanceof SitesApiError && cause.status === 404) {
        // The control plane is running but there is no /v1/auth/local: the sites-api in the image predates the login retrofit.
        // This is completely different from "the token is wrong", and the investigation direction is also opposite, so they must be discussed separately.
        setError(t("No /v1/merchants on control plane (404). This version of sites-api does not yet have merchant endpoints, so upgrade the control plane first."));
      } else if (cause instanceof SitesApiError && cause.status === 0) {
        setError(cause.message);
      } else {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        <span className="login-icon" aria-hidden="true"><KeyRound size={22} /></span>
        <h1>{t("Sites Console")}</h1>
        <p className="login-subtitle">
          {t("Multi-merchant site control-plane admin console. A platform admin token can manage every merchant and tenant.")}
        </p>

        {enabled.oidc ? (
          <button
            type="button"
            className="button button-primary"
            onClick={() => window.location.assign("/v1/auth/login")}
          >
            <ShieldCheck size={16} aria-hidden="true" />
            {t("Sign in with your identity provider")}
          </button>
        ) : null}

        {enabled.localLogin ? (
          <>
            <label className="field">
              <span>{enabled.oidc ? t("Break-glass local login") : t("Admin token")}</span>
              <input
                type="password"
                value={token}
                autoComplete="off"
                spellCheck={false}
                placeholder="SITES_SERVICE_TOKEN"
                onChange={(event) => setToken(event.target.value)}
              />
            </label>

            {error ? (
              <p className="login-error" role="alert">
                <CircleAlert size={16} aria-hidden="true" />
                {error}
              </p>
            ) : null}

            <button type="submit" className="button" disabled={pending}>
              {pending ? <LoaderCircle className="spin" size={16} /> : null}
              {pending ? t("Verifying") : t("Enter the console")}
            </button>
          </>
        ) : (
          <p className="login-note">{t("Local login is disabled on this control plane.")}</p>
        )}

        <p className="login-note">
          {t("The session is an HttpOnly cookie held by the control plane. The token is sent once, is never stored in the browser, and every local login is written to the audit log.")}
        </p>
        {USE_MOCK ? (
          <p className="login-note login-note-mock">
            {t("Mock data mode is active. Enter")} <code>wrong</code> {t("to reproduce 401.")}
          </p>
        ) : null}
        <LanguageSwitcher className="login-language" />
      </form>
    </div>
  );
}
