import { Ban, KeyRound, LoaderCircle, Pencil, Plus, Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { relativeTime } from "../format";
import { useI18n } from "../i18n";
import type { MerchantView, TenantTokenResponse, TenantView } from "../types";
import { EmptyState, PageHeader, RefreshButton, SearchField } from "./ConsolePrimitives";
import SecretDialog from "./SecretDialog";

/**
* Tenant page: cross-merchant list + filter by merchant, change quota, rotate token, and deactivate.
*
* 🔴 user_id is only unique within the merchant (Contract §0), so:
* - The React key of the form must be `merchant/user`. Only using userId will allow two merchants to download
* Tenants with the same name collapse into a row;
* - Each write operation comes with `merchantId`, the server will 400 if it is missing instead of guessing one.
*Similar to the merchant page, this page does not automatically poll.
 */

const DEFAULT_MAX_DEPLOYMENTS = 4;
// 2026-08-20 1→4: New form pre-filling is aligned with the deployment number (the server has mentioned 10 by default,
// See sites/validation.py DEFAULT_MAX_PUBLIC_ROUTES - original value 1 is concurrency
// Configuration root cause of session mutual deletion accident). Prefilling is just the UX default, and the server default is the real source.
const DEFAULT_MAX_PUBLIC_ROUTES = 4;

function rowKey(tenant: { merchantId: string; userId: string }): string {
  return `${tenant.merchantId}/${tenant.userId}`;
}

export default function TenantsView({
  merchantFilter,
  onMerchantFilterChange,
  onError,
}: {
  merchantFilter: string;
  onMerchantFilterChange: (merchantId: string) => void;
  onError: (cause: unknown) => void;
}) {
  const { localeTag, t } = useI18n();
  const [tenants, setTenants] = useState<TenantView[]>([]);
  const [merchants, setMerchants] = useState<MerchantView[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [busy, setBusy] = useState("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState("");
  const [draftMerchant, setDraftMerchant] = useState(merchantFilter);
  const [draftUser, setDraftUser] = useState("");
  const [draftDeployments, setDraftDeployments] = useState(String(DEFAULT_MAX_DEPLOYMENTS));
  const [draftRoutes, setDraftRoutes] = useState(String(DEFAULT_MAX_PUBLIC_ROUTES));
  const [editing, setEditing] = useState("");
  const [editDeployments, setEditDeployments] = useState("");
  const [editRoutes, setEditRoutes] = useState("");
  // The plaintext token only lives in this state, and the pop-up window disappears as soon as it is closed. No storage is entered.
  const [issued, setIssued] = useState<TenantTokenResponse | null>(null);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    try {
      const [tenantList, merchantList] = await Promise.all([
        api.listTenants(merchantFilter || undefined),
        api.listMerchants(),
      ]);
      setTenants(tenantList.tenants ?? []);
      setMerchants(merchantList.merchants ?? []);
      setLastUpdated(new Date());
    } catch (cause) {
      onError(cause);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [merchantFilter, onError]);

  useEffect(() => { void load(); }, [load]);

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const merchantId = draftMerchant.trim();
    const userId = draftUser.trim();
    const maxDeployments = Number(draftDeployments);
    const maxPublicRoutes = Number(draftRoutes);
    if (!merchantId || !userId) {
      setFormError(t("Merchant must be selected and Tenant ID must be filled in."));
      return;
    }
    if (!Number.isInteger(maxDeployments) || maxDeployments < 1) {
      setFormError(t("The deployment limit must be an integer no less than 1."));
      return;
    }
    // The public route quota is allowed to be 0: that means "this tenant is not allowed to occupy public network slots", which is not a wrong entry.
    if (!Number.isInteger(maxPublicRoutes) || maxPublicRoutes < 0) {
      setFormError(t("The public route limit must be an integer not less than 0."));
      return;
    }
    setFormError("");
    setBusy("__create__");
    try {
      setIssued(await api.createTenant({
        merchantId,
        userId,
        maxDeployments,
        maxPublicRoutes,
      }));
      setCreating(false);
      setDraftUser("");
      await load();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : String(cause));
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const startEdit = (tenant: TenantView) => {
    setEditing(rowKey(tenant));
    setEditDeployments(String(tenant.maxDeployments));
    setEditRoutes(String(tenant.maxPublicRoutes));
  };

  const submitEdit = async (tenant: TenantView) => {
    const maxDeployments = Number(editDeployments);
    const maxPublicRoutes = Number(editRoutes);
    if (!Number.isInteger(maxDeployments) || maxDeployments < 1
      || !Number.isInteger(maxPublicRoutes) || maxPublicRoutes < 0) {
      onError(new Error(t("Quota must be an integer: deployment limit ≥ 1, public route limit ≥ 0.")));
      return;
    }
    setBusy(rowKey(tenant));
    try {
      await api.updateTenant(tenant.merchantId, tenant.userId, {
        maxDeployments,
        maxPublicRoutes,
      });
      setEditing("");
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const rotate = async (tenant: TenantView) => {
    if (!window.confirm(
      t("Rotate the token for {owner}? The old token becomes invalid immediately. If the tenant is disabled, rotation also re-enables it.", {
        owner: `${tenant.merchantId}/${tenant.userId}`,
      }),
    )) return;
    setBusy(rowKey(tenant));
    try {
      setIssued(await api.rotateTenantToken(tenant.merchantId, tenant.userId));
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const disable = async (tenant: TenantView) => {
    if (!window.confirm(
      t("Disable {owner}? This revokes its credentials but does not delete existing workloads.", {
        owner: `${tenant.merchantId}/${tenant.userId}`,
      }),
    )) return;
    setBusy(rowKey(tenant));
    try {
      await api.disableTenant(tenant.merchantId, tenant.userId);
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const normalizedQuery = query.trim().toLowerCase();
  const visibleTenants = tenants.filter((tenant) => !normalizedQuery
    || `${tenant.merchantId} ${tenant.userId}`.toLowerCase().includes(normalizedQuery));
  const activeCount = tenants.filter((tenant) => !tenant.disabledAt).length;

  return (
    <div className="page">
      <PageHeader
        eyebrow={t("TENANT ACCESS")}
        title={t("Tenants")}
        description={t("Tenant IDs are unique within a merchant. Allocate deployment and public-route quotas here, and rotate tokens that are shown only once.")}
        meta={<span aria-live="polite">{t("{enabled} enabled · {disabled} disabled", { enabled: activeCount, disabled: tenants.length - activeCount })}{lastUpdated ? ` · ${t("Updated {time}", { time: lastUpdated.toLocaleTimeString(localeTag) })}` : ""}</span>}
        actions={<div className="page-actions"><RefreshButton refreshing={loading || refreshing} onRefresh={() => void load(true)} />
            <button
              type="button"
              className="button button-primary"
              onClick={() => {
                setCreating((value) => !value);
                setFormError("");
                if (merchantFilter) setDraftMerchant(merchantFilter);
              }}
            >
              <Plus size={15} />{t("New tenant")}
            </button></div>}
      />

      <section className="workspace-card">
        <div className="workspace-toolbar">
          <SearchField value={query} onChange={setQuery} label={t("Search tenants")} placeholder={t("Search for merchant or tenant ID")} />
          <div className="workspace-filters">
            <label className="compact-field">
              <span>{t("Merchant")}</span>
              <select
                value={merchantFilter}
                onChange={(event) => onMerchantFilterChange(event.target.value)}
              >
                <option value="">{t("All merchants")}</option>
                {merchants.map((merchant) => (
                  <option key={merchant.merchantId} value={merchant.merchantId}>
                    {merchant.merchantId}
                  </option>
                ))}
              </select>
            </label>
            <span className="card-count">{t("Showing {visible} / {total}", { visible: visibleTenants.length, total: tenants.length })}</span>
          </div>
        </div>

        {creating ? (
          <form className="form-panel" onSubmit={(event) => void submitCreate(event)}>
            <label className="field">
              <span>{t("Merchant")}</span>
              <select
                value={draftMerchant}
                onChange={(event) => setDraftMerchant(event.target.value)}
              >
                <option value="">{t("Please select")}</option>
                {merchants.map((merchant) => (
                  <option key={merchant.merchantId} value={merchant.merchantId}>
                    {merchant.merchantId}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("Tenant ID")}</span>
              <input
                value={draftUser}
                placeholder={t("The only one in the merchant")}
                onChange={(event) => setDraftUser(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("Deployment cap")}</span>
              <input
                type="number"
                min={1}
                value={draftDeployments}
                onChange={(event) => setDraftDeployments(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("Public route limit")}</span>
              <input
                type="number"
                min={0}
                value={draftRoutes}
                onChange={(event) => setDraftRoutes(event.target.value)}
              />
            </label>
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={busy === "__create__"}>
                {busy === "__create__" ? <LoaderCircle className="spin" size={16} /> : null}
                {t("Create and issue token")}
              </button>
              <button type="button" className="button" onClick={() => setCreating(false)}>{t("Cancel")}</button>
            </div>
            {formError ? <p className="form-error" role="alert">{formError}</p> : null}
          </form>
        ) : null}

        {loading ? (
          <div className="state"><LoaderCircle className="spin" size={20} /><strong>{t("Loading")}</strong></div>
        ) : visibleTenants.length === 0 ? (
          <EmptyState icon={<Users size={22} />} title={query || merchantFilter ? t("No matching tenant") : t("There are no tenants on the platform yet")} description={query || merchantFilter ? t("Adjust the search terms or merchant filter.") : t("First create a merchant, then issue tenant tokens and allocate deployment quotas.")} />
        ) : (
          <div className="table-scroll responsive-table">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("Merchant")}</th>
                  <th>{t("Tenant ID")}</th>
                  <th>{t("Deployment cap")}</th>
                  <th>{t("Public route limit")}</th>
                  <th>{t("Created")}</th>
                  <th>{t("Status")}</th>
                  <th aria-label={t("Operation")} />
                </tr>
              </thead>
              <tbody>
                {visibleTenants.map((tenant) => {
                  const key = rowKey(tenant);
                  const rowBusy = busy === key;
                  const isEditing = editing === key;
                  const disabled = Boolean(tenant.disabledAt);
                  return (
                    <tr key={key} className={disabled ? "row-disabled" : undefined}>
                      <td className="mono" data-label={t("Merchant")}>{tenant.merchantId}</td>
                      <td className="mono" data-label={t("Tenant ID")}>{tenant.userId}</td>
                      <td className="mono" data-label={t("Deployment cap")}>
                        {isEditing ? (
                          <input
                            className="cell-input cell-input-number"
                            type="number"
                            min={1}
                            value={editDeployments}
                            onChange={(event) => setEditDeployments(event.target.value)}
                          />
                        ) : (
                          tenant.maxDeployments
                        )}
                      </td>
                      <td className="mono" data-label={t("Public route limit")}>
                        {isEditing ? (
                          <input
                            className="cell-input cell-input-number"
                            type="number"
                            min={0}
                            value={editRoutes}
                            onChange={(event) => setEditRoutes(event.target.value)}
                          />
                        ) : (
                          tenant.maxPublicRoutes
                        )}
                      </td>
                  <td data-label={t("Created")}>{relativeTime(tenant.createdAt, localeTag, t)}</td>
                  <td data-label={t("Status")}>
                        <span className={`badge ${disabled ? "badge-bad" : "badge-ok"}`}>
                          {disabled ? t("Disabled") : t("Enabled")}
                        </span>
                      </td>
                  <td className="cell-actions" data-label={t("Operation")}>
                        {isEditing ? (
                          <>
                            <button
                              type="button"
                              className="button button-small button-primary"
                              disabled={rowBusy}
                              onClick={() => void submitEdit(tenant)}
                            >
                              {rowBusy ? <LoaderCircle className="spin" size={14} /> : null}{t("Save")}
                            </button>
                            <button type="button" className="button button-small" onClick={() => setEditing("")}>
                              {t("Cancel")}
                            </button>
                          </>
                        ) : (
                          <>
                            <button
                              type="button"
                              className="button button-small"
                              onClick={() => startEdit(tenant)}
                            >
                              <Pencil size={14} />{t("Change quota")}
                            </button>
                            <button
                              type="button"
                              className="button button-small"
                              disabled={rowBusy}
                              onClick={() => void rotate(tenant)}
                            >
                              <KeyRound size={14} />{t("Reissue token")}
                            </button>
                            <button
                              type="button"
                              className="button button-small button-danger"
                              disabled={rowBusy || disabled}
                              onClick={() => void disable(tenant)}
                            >
                              <Ban size={14} />{t("Deactivate")}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {issued ? (
        <SecretDialog
          title={t("Tenant token")}
          subject={`${issued.merchantId} / ${issued.userId}`}
          secret={issued.token}
          shownOnce
          note={issued.reenabled ? t("The tenant was originally deactivated and has been reactivated by issuing a token.") : issued.note}
          onClose={() => setIssued(null)}
        />
      ) : null}
    </div>
  );
}
