import { CheckCircle2, CircleAlert, CircleDashed, LoaderCircle } from "lucide-react";
import { ACTIVE_PHASES, phaseLabel } from "../format";
import { useI18n } from "../i18n";

/** Phase icon mapping for the standalone console. */
export function PhaseIcon({ phase }: { phase: string }) {
  if (phase === "Running") return <CheckCircle2 size={16} />;
  if (phase === "Failed") return <CircleAlert size={16} />;
  if (ACTIVE_PHASES.has(phase)) return <LoaderCircle className="spin" size={16} />;
  return <CircleDashed size={16} />;
}

export function PhaseBadge({ phase }: { phase: string }) {
  const { t } = useI18n();
  return (
    <span className={`phase phase-${phase.toLowerCase()}`}>
      <PhaseIcon phase={phase} />
      {phaseLabel(phase, t)}
    </span>
  );
}
