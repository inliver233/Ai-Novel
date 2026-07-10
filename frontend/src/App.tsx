import { Suspense, lazy } from "react";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";

import { AuthGuard } from "./components/layout/AuthGuard";
import { AdminGuard } from "./components/layout/AdminGuard";
import { ProjectProviderGuard } from "./components/layout/ProjectProviderGuard";
import { AppShell } from "./components/layout/AppShell";
import { ConfirmProvider } from "./components/ui/ConfirmProvider";
import { ToastProvider } from "./components/ui/ToastProvider";
import { AuthProvider } from "./contexts/AuthContext";
import { ProjectsProvider } from "./contexts/ProjectsContext";
import { importWithChunkRetry } from "./lib/lazyImportRetry";
import { RouteErrorPage } from "./pages/RouteErrorPage";

const LoginPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/LoginPage"));
  return { default: mod.LoginPage };
});

const RegisterPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/RegisterPage"));
  return { default: mod.RegisterPage };
});

const DashboardPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/DashboardPage"));
  return { default: mod.DashboardPage };
});

const AdminUsersPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/AdminUsersPage"));
  return { default: mod.AdminUsersPage };
});

const ProjectWizardPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/ProjectWizardPage"));
  return { default: mod.ProjectWizardPage };
});

const SettingsPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/SettingsPage"));
  return { default: mod.SettingsPage };
});

const CharactersPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/CharactersPage"));
  return { default: mod.CharactersPage };
});

const EntriesPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/EntriesPage"));
  return { default: mod.EntriesPage };
});

const OutlinePage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/OutlinePage"));
  return { default: mod.OutlinePage };
});

const WritingPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/WritingPage"));
  return { default: mod.WritingPage };
});

const PreviewPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/PreviewPage"));
  return { default: mod.PreviewPage };
});

const PromptsPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/PromptsPage"));
  return { default: mod.PromptsPage };
});

const PromptStudioPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/PromptStudioPage"));
  return { default: mod.PromptStudioPage };
});

const PromptTemplatesPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/PromptTemplatesPage"));
  return { default: mod.PromptTemplatesPage };
});

const ExportPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/ExportPage"));
  return { default: mod.ExportPage };
});

const ImportPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/ImportPage"));
  return { default: mod.ImportPage };
});

const SearchPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/SearchPage"));
  return { default: mod.SearchPage };
});

const NotFoundPage = lazy(async () => {
  const mod = await importWithChunkRetry(() => import("./pages/NotFoundPage"));
  return { default: mod.NotFoundPage };
});

const router = createBrowserRouter([
  {
    path: "/login",
    element: <LoginPage />,
    errorElement: <RouteErrorPage />,
  },
  {
    path: "/register",
    element: <RegisterPage />,
    errorElement: <RouteErrorPage />,
  },
  {
    element: <AuthGuard />,
    errorElement: <RouteErrorPage />,
    children: [
      {
        path: "/",
        element: (
          <ProjectsProvider>
            <AppShell />
          </ProjectsProvider>
        ),
        errorElement: <RouteErrorPage />,
        children: [
          {
            index: true,
            element: <DashboardPage />,
          },
          {
            element: <AdminGuard />,
            children: [
              {
                path: "admin/users",
                element: <AdminUsersPage />,
              },
            ],
          },
          {
            path: "projects/:projectId",
            element: <ProjectProviderGuard />,
            children: [
              { index: true, element: <Navigate to="writing" replace /> },
              {
                path: "wizard",
                element: <ProjectWizardPage />,
              },
              {
                path: "settings",
                element: <SettingsPage />,
              },
              {
                path: "characters",
                element: <CharactersPage />,
              },
              {
                path: "entries",
                element: <EntriesPage />,
              },
              {
                path: "outline",
                element: <OutlinePage />,
              },
              {
                path: "writing",
                element: <WritingPage />,
              },
              {
                path: "preview",
                element: <PreviewPage />,
              },
              {
                path: "prompts",
                element: <PromptsPage />,
              },
              {
                path: "prompt-studio",
                element: <PromptStudioPage />,
              },
              {
                path: "styles",
                element: <Navigate to="../prompt-studio" replace />,
              },
              {
                path: "prompt-templates",
                element: <PromptTemplatesPage />,
              },
              {
                path: "export",
                element: <ExportPage />,
              },
              {
                path: "import",
                element: <ImportPage />,
              },
              {
                path: "search",
                element: <SearchPage />,
              },
            ],
          },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
]);

export default function App() {
  return (
    <ToastProvider>
      <ConfirmProvider>
        <AuthProvider>
          <Suspense fallback={<div className="p-4 text-sm text-subtext">加载中…</div>}>
            <RouterProvider router={router} />
          </Suspense>
        </AuthProvider>
      </ConfirmProvider>
    </ToastProvider>
  );
}
