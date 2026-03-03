import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../contexts/auth";
import { UI_COPY } from "../lib/uiCopy";
import { DebugDetails } from "../components/atelier/DebugPageShell";
import { ApiError } from "../services/apiClient";
import { fetchAuthProviders } from "../services/authProviders";
import { DEFAULT_USER_ID, getCurrentUserId } from "../services/currentUser";
import { useToast } from "../components/ui/toast";

function safeNextPath(value: string | null): string {
  if (!value) return "/";
  if (!value.startsWith("/")) return "/";
  if (value.startsWith("//")) return "/";
  return value;
}

export function LoginPage() {
  const auth = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const nextPath = useMemo(() => safeNextPath(searchParams.get("next")), [searchParams]);
  const oidcError = useMemo(() => String(searchParams.get("oidc_error") || "").trim(), [searchParams]);
  const oidcRequestId = useMemo(() => String(searchParams.get("request_id") || "").trim() || "unknown", [searchParams]);

  useEffect(() => {
    if (!oidcError) return;
    toast.toastError(`${UI_COPY.auth.linuxdoLoginFailedPrefix}${oidcError}`, oidcRequestId);
    const next = new URLSearchParams(searchParams);
    next.delete("oidc_error");
    next.delete("request_id");
    setSearchParams(next, { replace: true });
  }, [oidcError, oidcRequestId, searchParams, setSearchParams, toast]);

  const [form, setForm] = useState(() => ({
    userId: getCurrentUserId() === DEFAULT_USER_ID ? "" : getCurrentUserId(),
    password: "",
  }));
  const [busy, setBusy] = useState(false);
  const submitDisabled = busy || !form.userId.trim() || !form.password;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitDisabled) return;
    setBusy(true);
    try {
      await auth.login({ userId: form.userId.trim(), password: form.password });
      toast.toastSuccess(UI_COPY.auth.loginSuccess);
      navigate(nextPath, { replace: true });
    } catch (e) {
      const err = e as ApiError;
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    } finally {
      setBusy(false);
    }
  };

  if (auth.status === "authenticated") {
    return <Navigate to={nextPath} replace />;
  }

  return (
    <div className="min-h-screen bg-canvas text-ink">
      <div className="mx-auto flex min-h-screen max-w-screen-sm items-center px-4 py-12">
        <div className="w-full">
          <div className="surface p-6 sm:p-8">
            <div className="font-content text-2xl text-ink">{UI_COPY.auth.loginTitle}</div>
            <div className="mt-1 grid gap-1 text-sm text-subtext">
              <div>{UI_COPY.auth.loginSubtitle}</div>
              {nextPath !== "/" ? (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span>登录后将返回：</span>
                  <span className="max-w-full truncate rounded border border-border bg-surface px-2 py-0.5 font-mono text-[11px] text-ink">
                    {nextPath}
                  </span>
                </div>
              ) : null}
            </div>

            {auth.status === "dev_fallback" ? (
              <div className="mt-4 grid gap-3">
                <div className="rounded-atelier border border-border bg-canvas p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="text-xs text-subtext">
                      <div className="flex flex-wrap items-center gap-2 text-ink">
                        <span>{UI_COPY.auth.devFallbackHint}</span>
                        <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-subtext">
                          {UI_COPY.auth.devFallbackTag}
                        </span>
                      </div>
                      <div className="mt-1">你可以先跳过登录直接进入体验；需要权限/协作/多用户时再回来登录即可。</div>
                    </div>
                    <button
                      className="btn btn-secondary"
                      onClick={() => navigate("/", { replace: true })}
                      type="button"
                    >
                      跳过登录，{UI_COPY.auth.continueInDevFallback}
                    </button>
                  </div>
                </div>
                <DebugDetails title="更多说明（可选）">
                  <div className="grid gap-1 text-xs text-subtext">
                    <div>{UI_COPY.auth.devFallbackRiskHint}</div>
                    <div>{UI_COPY.auth.devFallbackNextStepHint}</div>
                  </div>
                </DebugDetails>
              </div>
            ) : null}

            <form className="mt-6 grid gap-6" onSubmit={(event) => void handleSubmit(event)}>
              <div className="grid gap-3">
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">{UI_COPY.auth.userIdLabel}</span>
                  <input
                    className="input"
                    name="user_id"
                    value={form.userId}
                    onChange={(e) => setForm((v) => ({ ...v, userId: e.target.value }))}
                    autoComplete="username"
                    placeholder={UI_COPY.auth.userIdPlaceholder}
                  />
                </label>
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">{UI_COPY.auth.passwordLabel}</span>
                  <input
                    className="input"
                    name="password"
                    type="password"
                    value={form.password}
                    onChange={(e) => setForm((v) => ({ ...v, password: e.target.value }))}
                    autoComplete="current-password"
                    placeholder={UI_COPY.auth.passwordPlaceholder}
                  />
                </label>
              </div>

              <div className="flex items-center justify-end gap-2">
                <button
                  className="btn btn-secondary"
                  onClick={() => {
                    setForm({ userId: "", password: "" });
                  }}
                  type="button"
                >
                  {UI_COPY.auth.reset}
                </button>
                <button className="btn btn-primary" disabled={submitDisabled} type="submit">
                  {busy ? UI_COPY.auth.loggingIn : UI_COPY.auth.login}
                </button>
              </div>

              <div>
                <div className="my-3 flex items-center gap-3 text-xs text-subtext">
                  <div className="h-px flex-1 bg-border" />
                  <div>或</div>
                  <div className="h-px flex-1 bg-border" />
                </div>
                <button
                  className="btn btn-secondary w-full"
                  onClick={() => {
                    void (async () => {
                      try {
                        const providers = await fetchAuthProviders();
                        const enabled = Boolean(providers.linuxdo?.enabled);
                        if (!enabled) {
                          toast.toastWarning(UI_COPY.auth.linuxdoNotEnabledHint);
                          return;
                        }
                        const url = `/api/auth/oidc/linuxdo/start?next=${encodeURIComponent(nextPath)}`;
                        window.location.assign(url);
                      } catch (e) {
                        const err = e as ApiError;
                        toast.toastError(
                          `${UI_COPY.auth.linuxdoCheckFailedPrefix}${err.message} (${err.code})`,
                          err.requestId,
                        );
                      }
                    })();
                  }}
                  type="button"
                >
                  {UI_COPY.auth.linuxdoLogin}
                </button>
              </div>
            </form>
          </div>
          <div className="mt-4 text-center text-xs text-subtext">
            <div className="flex flex-wrap items-center justify-center gap-1">
              <span>{UI_COPY.auth.noAccountHint}</span>
              <Link
                className="text-ink underline decoration-border hover:decoration-ink"
                to={`/register?next=${encodeURIComponent(nextPath)}`}
              >
                {UI_COPY.auth.goRegister}
              </Link>
            </div>
            <div>{UI_COPY.auth.loginFooterHint}</div>
            <div className="mt-1">忘记密码？当前版本请联系管理员重置（MVP 暂不支持自助找回）。</div>
          </div>
        </div>
      </div>
    </div>
  );
}
