import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useWizardProgress } from "../../hooks/useWizardProgress";
import { WizardNextBar } from "../atelier/WizardNextBar";
import {
  ProjectWizardShellContext,
  type ProjectWizardShellContextValue,
  type WizardBarConfig,
} from "./ProjectWizardShellContext";

export function ProjectWizardShell(props: { projectId: string; children: React.ReactNode }) {
  const { projectId, children } = props;
  const location = useLocation();
  const navigate = useNavigate();
  const wizard = useWizardProgress(projectId);
  const [barConfig, setBarConfig] = useState<WizardBarConfig | null>(null);

  const ctxValue = useMemo<ProjectWizardShellContextValue>(
    () => ({
      loading: wizard.loading,
      progress: wizard.progress,
      refreshWizard: wizard.refresh,
      bumpWizardLocal: wizard.bumpLocal,
      setBarConfig,
    }),
    [wizard.bumpLocal, wizard.loading, wizard.progress, wizard.refresh],
  );

  const onWizardOverview = location.pathname.endsWith("/wizard");

  const overviewBarConfig = useMemo<WizardBarConfig | null>(() => {
    if (!onWizardOverview) return null;
    const next = wizard.progress.nextStep;
    return {
      currentStep: next?.key ?? "export",
      primaryAction: next
        ? {
            label: `下一步：${next.title}`,
            onClick: () => navigate(next.href),
          }
        : {
            label: "已完成：回到项目概览",
            onClick: () => navigate("/"),
          },
    };
  }, [navigate, onWizardOverview, wizard.progress.nextStep]);

  const hideBar = location.pathname.endsWith("/prompt-studio");
  const effectiveBarConfig = onWizardOverview ? overviewBarConfig : barConfig;
  const showBar = Boolean(effectiveBarConfig && !hideBar);

  return (
    <ProjectWizardShellContext.Provider value={ctxValue}>
      {children}
      {showBar ? (
        <WizardNextBar
          projectId={projectId}
          currentStep={effectiveBarConfig!.currentStep}
          progress={wizard.progress}
          loading={wizard.loading}
          loadError={wizard.error}
          onRetryLoad={wizard.reload}
          retryBlockedReason={effectiveBarConfig!.dirty ? "请先保存或放弃未保存修改再重试" : undefined}
          dirty={effectiveBarConfig!.dirty}
          saving={effectiveBarConfig!.saving}
          onSave={effectiveBarConfig!.onSave}
          primaryAction={effectiveBarConfig!.primaryAction}
        />
      ) : null}
    </ProjectWizardShellContext.Provider>
  );
}
