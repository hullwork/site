import { Check, Copy, TriangleAlert, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useI18n } from "../i18n";

/**
* One-time clear text credential pop-up window (shared by merchant apiKey / tenant token).
*
* 🔴 Constraint: This value only appears once in the server response, and the library only has sha256. it can only stay at
* In the React state of the caller of this component, it disappears when closed:
* - do not write sessionStorage/localStorage/cookie/IndexedDB
* - Do not enter URL, do not enter document.title
* - Do not open console.log (the devtools log will be screenshotted and screen recorded)
* "Only show this once" warning is triggered by `apiKeyShownOnce` on the server side (Contract §4.3),
* The frontend does not judge by itself - if the judgment is wrong, it will make people think that it can be taken again.
* AI-LOCK: Do not add convenient functions such as "remember this key".
 */
export default function SecretDialog({
  title,
  subject,
  secret,
  shownOnce,
  note,
  onClose,
}: {
  title: string;
  subject: string;
  secret: string;
  shownOnce: boolean;
  note?: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const headingId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    const previous = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusable = dialog?.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    focusable?.[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || focusable === undefined || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    dialog?.addEventListener("keydown", onKeyDown);
    return () => {
      dialog?.removeEventListener("keydown", onKeyDown);
      if (previous?.isConnected) previous.focus();
    };
  }, [onClose]);

  const copy = async () => {
    setCopyError("");
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // The clipboard will be thrown when there is a non-security context or the user denies permission. Make it clear that you have to select manually.
      // Instead of letting the button click without any response - if you miss this moment, you will never get the value again.
      setCopyError(t("The browser has denied clipboard access. Please manually select the text below and copy it."));
    }
  };

  return (
    <div className="modal-backdrop">
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
      >
        <div className="modal-header">
          <h2 id={headingId}>{title}</h2>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label={t("Close")}
          >
            <X size={18} />
          </button>
        </div>

        {shownOnce ? (
          <div className="secret-warning" role="alert">
            <TriangleAlert size={18} aria-hidden="true" />
            <div>
              <strong>{t("Shown only this time")}</strong>
              <p>
                {t("This value cannot be viewed again after the dialog closes. The control plane stores only its SHA-256 digest, so a lost value must be replaced. Save it in your secret manager now.")}
              </p>
            </div>
          </div>
        ) : null}

        <dl className="modal-meta">
          <div>
            <dt>{t("Owner")}</dt>
            <dd>{subject}</dd>
          </div>
        </dl>

        <code className="secret-value">{secret}</code>

        {note ? <p className="modal-note">{note}</p> : null}
        {copyError ? (
          <p className="modal-error" role="alert">{copyError}</p>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="button" onClick={() => void copy()}>
            {copied ? <Check size={16} /> : <Copy size={16} />}
            {copied ? t("Copied") : t("Copy")}
          </button>
          <button type="button" className="button button-primary" onClick={onClose}>
            {t("I have saved")}
          </button>
        </div>
      </div>
    </div>
  );
}
