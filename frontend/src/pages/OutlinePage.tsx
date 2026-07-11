import { useEffect, useState } from "react";
import clsx from "clsx";

import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { GenerationFloatingCard } from "../components/ui/GenerationFloatingCard";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { copyText } from "../lib/copyText";
import { formatApiErrorFields } from "../lib/apiErrorPresentation";
import type { ApiError } from "../services/apiClient";

import { DetailedOutlineGenerationModal, DetailedOutlineSection } from "./outline/DetailedOutlineSection";
import {
  OutlineActionsBar,
  OutlineEditorSection,
  OutlineGenerationModal,
  OutlineParsingModal,
  OutlineGuideSection,
  OutlineHeaderSection,
  OutlineTitleModal,
} from "./outline/OutlinePageSections";
import { OUTLINE_COPY } from "./outline/outlineCopy";
import { useOutlinePageState } from "./outline/useOutlinePageState";

type TabId = "outline" | "detailed";

function OutlineQueryErrorCard(props: {
  error: ApiError;
  onRetry: () => void;
  retryBlockedReason?: string;
  variant: "blocking" | "warning";
}) {
  const requestId = props.error.requestId && props.error.requestId !== "unknown" ? props.error.requestId : undefined;
  const blocking = props.variant === "blocking";
  return (
    <div className={blocking ? "error-card" : "rounded-atelier border border-warning/30 bg-warning/5 p-4"}>
      <div className="state-title">{blocking ? "加载失败" : "最近一次刷新失败"}</div>
      <div className="state-desc">{formatApiErrorFields(props.error)}</div>
      {requestId ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-subtext">
          <span>request_id: {requestId}</span>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => void copyText(requestId, { title: "复制 request_id" })}
            type="button"
          >
            复制 request_id
          </button>
        </div>
      ) : null}
      {props.retryBlockedReason ? (
        <div className="mt-3 text-xs text-warning">{props.retryBlockedReason}</div>
      ) : (
        <button className="btn btn-primary mt-4" onClick={props.onRetry} type="button">
          重试
        </button>
      )}
    </div>
  );
}

function TabButton(props: { id: TabId; active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      className={clsx(
        "px-4 py-2 text-sm font-medium border-b-2 ui-transition-fast",
        props.active ? "border-accent text-accent" : "border-transparent text-subtext hover:text-ink",
      )}
      onClick={props.onClick}
    >
      {props.children}
    </button>
  );
}

export function OutlinePage() {
  const state = useOutlinePageState();
  const [activeTab, setActiveTab] = useState<TabId>("outline");

  useEffect(() => {
    if (state.switchToDetailedRequested) {
      setActiveTab("detailed");
      state.clearSwitchToDetailedRequest();
    }
  }, [state.switchToDetailedRequested, state.clearSwitchToDetailedRequest]);

  if (state.loading) return <div className="text-subtext">{OUTLINE_COPY.loading}</div>;

  if (state.blockingLoadError) {
    return (
      <OutlineQueryErrorCard error={state.blockingLoadError} onRetry={() => void state.reload()} variant="blocking" />
    );
  }

  const detailedCopy = OUTLINE_COPY.detailedOutline;
  const detailedState = state.detailedOutlineState;

  // Wire up tab switching from action bar and wizard bar
  const actionsBarProps = {
    ...state.actionsBarProps,
    onGoToDetailedTab: () => setActiveTab("detailed"),
  };
  const wizardBarProps = {
    ...state.wizardBarProps,
    primaryAction: state.wizardBarProps.primaryAction
      ? {
          ...state.wizardBarProps.primaryAction,
          onClick:
            detailedState.items.length > 0
              ? () => setActiveTab("detailed")
              : state.wizardBarProps.primaryAction?.onClick,
        }
      : undefined,
  };

  return (
    <div className="grid gap-4 pb-[calc(6rem+env(safe-area-inset-bottom))]">
      {state.showUnsavedGuard ? <UnsavedChangesGuard when={state.dirty} /> : null}
      {state.refreshLoadError ? (
        <OutlineQueryErrorCard
          error={state.refreshLoadError}
          onRetry={() => void state.reload()}
          retryBlockedReason={state.dirty ? "请先保存或放弃未保存修改再重试" : undefined}
          variant="warning"
        />
      ) : null}
      <OutlineHeaderSection {...state.headerProps} />

      {/* Tab navigation */}
      <div className="flex gap-1 border-b border-border">
        <TabButton id="outline" active={activeTab === "outline"} onClick={() => setActiveTab("outline")}>
          {detailedCopy.tabOutline}
        </TabButton>
        <TabButton id="detailed" active={activeTab === "detailed"} onClick={() => setActiveTab("detailed")}>
          {detailedCopy.tabDetailed}
          {detailedState.items.length > 0 ? ` (${detailedState.items.length}${detailedCopy.volumeSuffix})` : ""}
        </TabButton>
      </div>

      {/* Tab content */}
      {activeTab === "outline" ? (
        <>
          <OutlineActionsBar {...actionsBarProps} />
          <OutlineGuideSection />
          <OutlineEditorSection {...state.editorProps} />
        </>
      ) : (
        <DetailedOutlineSection {...detailedState} />
      )}

      <OutlineTitleModal {...state.titleModalProps} />
      <OutlineGenerationModal {...state.generationModalProps} />
      <OutlineParsingModal {...state.parsingModalProps} />
      <DetailedOutlineGenerationModal
        open={detailedState.generateModalOpen}
        generating={detailedState.generating}
        progress={detailedState.progress}
        onClose={detailedState.closeGenerateModal}
        onGenerate={(req) => void detailedState.generate(req)}
        onCancelGenerate={detailedState.cancelGenerate}
      />
      <WizardNextBar {...wizardBarProps} />
      <GenerationFloatingCard {...state.outlineGenFloatingProps} />
      <GenerationFloatingCard {...state.parsingFloatingProps} />
      <GenerationFloatingCard {...state.detailedGenFloatingProps} />
      <GenerationFloatingCard {...state.skeletonGenFloatingProps} />
    </div>
  );
}
