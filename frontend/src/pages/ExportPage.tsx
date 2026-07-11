import { toastApiError } from "../lib/apiErrorPresentation";
import { toApiError } from "../services/apiError";
import { type ReactNode, useCallback, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { Check } from "lucide-react";

import { GhostwriterIndicator } from "../components/atelier/GhostwriterIndicator";
import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { useToast } from "../components/ui/toast";
import { useWizardProgress } from "../hooks/useWizardProgress";
import { apiDownloadMarkdown } from "../services/apiClient";
import { markWizardExported } from "../services/wizard";

type ExportForm = {
  include_settings: boolean;
  include_characters: boolean;
  include_outline: boolean;
  chapters: "all" | "done";
};

type AtelierOptionControlProps = {
  type: "checkbox" | "radio";
  checked: boolean;
  disabled?: boolean;
  name?: string;
  onCheckedChange: (next: boolean) => void;
  children: ReactNode;
};

function AtelierOptionControl({ type, checked, disabled, name, onCheckedChange, children }: AtelierOptionControlProps) {
  const isRadio = type === "radio";
  return (
    <label className="group flex items-center gap-2 text-sm text-ink">
      <input
        className="peer sr-only"
        checked={checked}
        disabled={disabled}
        name={name}
        onChange={(e) => onCheckedChange(e.target.checked)}
        type={type}
      />
      <span
        className={[
          "inline-flex h-4 w-4 items-center justify-center border border-border bg-canvas ui-transition-fast",
          isRadio ? "rounded-full" : "rounded",
          "group-hover:border-accent/35",
          "peer-focus-visible:outline-none peer-focus-visible:ring-2 peer-focus-visible:ring-accent peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-canvas",
          "peer-checked:border-accent/50 peer-checked:bg-accent/10",
          "peer-disabled:opacity-60 peer-disabled:cursor-not-allowed",
        ].join(" ")}
      >
        {isRadio ? (
          <span className="h-2 w-2 rounded-full bg-accent opacity-0 peer-checked:opacity-100" aria-hidden="true" />
        ) : (
          <Check className="h-3 w-3 text-accent opacity-0 peer-checked:opacity-100" aria-hidden="true" />
        )}
      </span>
      <span className="select-none">{children}</span>
    </label>
  );
}

export function ExportPage() {
  const { projectId } = useParams();
  const toast = useToast();
  const wizard = useWizardProgress(projectId);
  const bumpWizardLocal = wizard.bumpLocal;

  const [exporting, setExporting] = useState(false);
  const [form, setForm] = useState<ExportForm>({
    include_settings: true,
    include_characters: true,
    include_outline: true,
    chapters: "all",
  });

  const url = useMemo(() => {
    if (!projectId) return "";
    const qs = new URLSearchParams();
    qs.set("include_settings", form.include_settings ? "1" : "0");
    qs.set("include_characters", form.include_characters ? "1" : "0");
    qs.set("include_outline", form.include_outline ? "1" : "0");
    qs.set("chapters", form.chapters);
    return `/api/projects/${projectId}/export/markdown?${qs.toString()}`;
  }, [form, projectId]);

  const doExport = useCallback(async (): Promise<boolean> => {
    if (!projectId) return false;
    if (!url) return false;
    if (exporting) return false;
    setExporting(true);
    try {
      const { filename, content } = await apiDownloadMarkdown(url);
      const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename || "ainovel.md";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      toast.toastSuccess("已导出 Markdown，已开始下载");
      markWizardExported(projectId);
      bumpWizardLocal();
      return true;
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
      return false;
    } finally {
      setExporting(false);
    }
  }, [bumpWizardLocal, exporting, projectId, toast, url]);

  return (
    <div className="grid gap-6 pb-24">
      <section className="panel p-4 sm:p-8">
        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-2">
            <div className="font-content text-xl">导出 Markdown</div>
            <div className="text-xs text-subtext">
              按选项生成并下载 `.md` 文件（如浏览器拦截下载，请允许该站点下载）。
            </div>
          </div>
          <button
            className="btn btn-primary"
            disabled={!projectId || exporting}
            onClick={() => void doExport()}
            type="button"
          >
            {exporting ? "导出中…" : "导出 Markdown"}
          </button>
        </div>

        {exporting ? <GhostwriterIndicator className="mt-4" label="导出中：正在生成并下载 Markdown…" /> : null}

        <div className="mt-5 grid gap-4">
          <div className="grid gap-2">
            <div className="text-xs text-subtext">包含内容</div>
            <AtelierOptionControl
              checked={form.include_settings}
              disabled={exporting}
              name="include_settings"
              onCheckedChange={(next) => setForm((v) => ({ ...v, include_settings: next }))}
              type="checkbox"
            >
              设定
            </AtelierOptionControl>
            <AtelierOptionControl
              checked={form.include_characters}
              disabled={exporting}
              name="include_characters"
              onCheckedChange={(next) => setForm((v) => ({ ...v, include_characters: next }))}
              type="checkbox"
            >
              角色卡
            </AtelierOptionControl>
            <AtelierOptionControl
              checked={form.include_outline}
              disabled={exporting}
              name="include_outline"
              onCheckedChange={(next) => setForm((v) => ({ ...v, include_outline: next }))}
              type="checkbox"
            >
              大纲
            </AtelierOptionControl>
          </div>

          <div className="grid gap-2">
            <div className="text-xs text-subtext">章节范围</div>
            <AtelierOptionControl
              checked={form.chapters === "all"}
              disabled={exporting}
              name="chapters"
              onCheckedChange={(next) => {
                if (!next) return;
                setForm((v) => ({ ...v, chapters: "all" }));
              }}
              type="radio"
            >
              全部章节
            </AtelierOptionControl>
            <AtelierOptionControl
              checked={form.chapters === "done"}
              disabled={exporting}
              name="chapters"
              onCheckedChange={(next) => {
                if (!next) return;
                setForm((v) => ({ ...v, chapters: "done" }));
              }}
              type="radio"
            >
              仅定稿章节
            </AtelierOptionControl>
            <div className="text-[11px] text-subtext">定稿章节：章节状态为“定稿（done）”。</div>
          </div>

          <details className="surface p-3 text-xs text-subtext">
            <summary className="ui-transition-fast cursor-pointer hover:text-ink">排障信息（请求 URL）</summary>
            <div className="mt-2 break-all">{url || "（请选择项目）"}</div>
          </details>
        </div>
      </section>

      <WizardNextBar
        projectId={projectId}
        currentStep="export"
        progress={wizard.progress}
        loading={wizard.loading}
        loadError={wizard.error}
        onRetryLoad={wizard.reload}
        primaryAction={
          wizard.progress.nextStep?.key === "export"
            ? { label: "本页：导出 Markdown", disabled: exporting, onClick: doExport }
            : undefined
        }
      />
    </div>
  );
}
