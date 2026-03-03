import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../contexts/auth";
import { UI_COPY } from "../lib/uiCopy";
import { ApiError } from "../services/apiClient";
import { fetchAuthProviders } from "../services/authProviders";
import { useToast } from "../components/ui/toast";

function safeNextPath(value: string | null): string {
  if (!value) return "/";
  if (!value.startsWith("/")) return "/";
  if (value.startsWith("//")) return "/";
  return value;
}

export function RegisterPage() {
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
    userId: "",
    password: "",
    confirmPassword: "",
  }));
  const [busy, setBusy] = useState(false);

  const passwordMismatch = Boolean(form.password && form.confirmPassword && form.password !== form.confirmPassword);
  const submitDisabled = busy || !form.userId.trim() || !form.password || passwordMismatch;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitDisabled) return;
    setBusy(true);
    try {
      await auth.register({ userId: form.userId.trim(), password: form.password });
      toast.toastSuccess(UI_COPY.auth.registerSuccess);
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
            <div className="font-content text-2xl text-ink">{UI_COPY.auth.registerTitle}</div>
            <div className="mt-1 grid gap-1 text-sm text-subtext">
              <div>{UI_COPY.auth.registerSubtitle}</div>
              <div className="text-xs">{UI_COPY.auth.passwordHint}</div>
              {nextPath !== "/" ? (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span>注册后将进入：</span>
                  <span className="max-w-full truncate rounded border border-border bg-surface px-2 py-0.5 font-mono text-[11px] text-ink">
                    {nextPath}
                  </span>
                </div>
              ) : null}
            </div>

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
                    autoComplete="new-password"
                    placeholder={UI_COPY.auth.passwordPlaceholder}
                  />
                </label>
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">{UI_COPY.auth.confirmPasswordLabel}</span>
                  <input
                    className={`input ${passwordMismatch ? "border-danger" : ""}`}
                    name="confirm_password"
                    type="password"
                    value={form.confirmPassword}
                    onChange={(e) => setForm((v) => ({ ...v, confirmPassword: e.target.value }))}
                    autoComplete="new-password"
                    placeholder={UI_COPY.auth.confirmPasswordPlaceholder}
                  />
                </label>
                {passwordMismatch ? <div className="text-xs text-danger">两次输入的密码不一致</div> : null}
              </div>

              <div className="flex items-center justify-end gap-2">
                <button
                  className="btn btn-secondary"
                  onClick={() => {
                    setForm({ userId: "", password: "", confirmPassword: "" });
                  }}
                  type="button"
                >
                  {UI_COPY.auth.reset}
                </button>
                <button className="btn btn-primary" disabled={submitDisabled} type="submit">
                  {busy ? UI_COPY.auth.registering : UI_COPY.auth.register}
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
              <span>{UI_COPY.auth.haveAccountHint}</span>
              <Link
                className="text-ink underline decoration-border hover:decoration-ink"
                to={`/login?next=${encodeURIComponent(nextPath)}`}
              >
                {UI_COPY.auth.goLogin}
              </Link>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
