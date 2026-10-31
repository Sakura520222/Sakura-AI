import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./app/auth";
import { ErrorBoundary } from "./app/ErrorBoundary";
import AppShell from "./components/layout/AppShell";
import { EmptyState, LoadingState } from "./components/common/states";
import { ConfirmProvider } from "./components/ui/ConfirmProvider";
import "./styles.css";

const DashboardPage = lazy(() => import("./features/dashboard/DashboardPage"));
const RepositoriesPage = lazy(
  () => import("./features/repositories/RepositoriesPage"),
);
const ReviewsPage = lazy(() => import("./features/reviews/ReviewsPage"));
const IssuesPage = lazy(() => import("./features/issues/IssuesPage"));
const ScansPage = lazy(() => import("./features/scans/ScansPage"));
const QueuePage = lazy(() => import("./features/queue/QueuePage"));
const SettingsPage = lazy(() => import("./features/settings/SettingsPage"));
const AgentPage = lazy(() => import("./features/agent/AgentPage"));
const CandidatesPage = lazy(() => import("./features/agent/CandidatesPage"));
const SkillsPage = lazy(() => import("./features/agent-management/SkillsPage"));
const WorkspacesPage = lazy(
  () => import("./features/agent-management/WorkspacesPage"),
);
const ActivityPage = lazy(() => import("./features/activity/ActivityPage"));
const GitHubAppPage = lazy(() => import("./features/github/GitHubAppPage"));
const BillingPage = lazy(() => import("./features/billing/BillingPage"));
const AdminPage = lazy(() => import("./features/admin/AdminPage"));
const SetupPage = lazy(() => import("./features/setup/SetupPage"));
const LegalPage = lazy(() =>
  import("./features/public/PublicPages").then((module) => ({
    default: module.LegalPage,
  })),
);
const ErrorPage = lazy(() =>
  import("./features/public/PublicPages").then((module) => ({
    default: module.ErrorPage,
  })),
);
const AnnouncementRedirect = lazy(() =>
  import("./features/announcements/AnnouncementsPage").then((module) => ({
    default: module.AnnouncementRedirect,
  })),
);
const CommunityPage = lazy(() => import("./features/community/CommunityPage"));
const MemoryPage = lazy(() => import("./features/memory/MemoryPage"));
const NotificationsPage = lazy(
  () => import("./features/notifications/NotificationsPage"),
);
const PublicLogin = lazy(() =>
  import("./features/auth/AuthPages").then((module) => ({
    default: module.PublicLogin,
  })),
);
const PublicSecurity = lazy(() =>
  import("./features/auth/AuthPages").then((module) => ({
    default: module.PublicSecurity,
  })),
);
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});
const router = createBrowserRouter(
  [
    {
      path: "setup",
      element: (
        <Suspense fallback={<LoadingState />}>
          <SetupPage />
        </Suspense>
      ),
    },
    {
      path: "legal/:kind",
      element: (
        <Suspense fallback={<LoadingState />}>
          <LegalPage />
        </Suspense>
      ),
    },
    {
      path: "error",
      element: (
        <Suspense fallback={<LoadingState />}>
          <ErrorPage />
        </Suspense>
      ),
    },
    {
      path: "login",
      element: (
        <Suspense fallback={<LoadingState />}>
          <PublicLogin />
        </Suspense>
      ),
    },
    {
      path: "security",
      element: (
        <Suspense fallback={<LoadingState />}>
          <PublicSecurity />
        </Suspense>
      ),
    },
    {
      element: (
        <AuthProvider>
          <ConfirmProvider>
            <AppShell />
          </ConfirmProvider>
        </AuthProvider>
      ),
      errorElement: (
        <EmptyState title="页面无法打开 / Unable to open this page" />
      ),
      children: [
        { index: true, element: <DashboardPage /> },
        { path: "repositories", element: <RepositoriesPage /> },
        { path: "repositories/:owner/:name", element: <RepositoriesPage /> },
        { path: "reviews", element: <ReviewsPage /> },
        { path: "reviews/:id", element: <ReviewsPage /> },
        { path: "reviews/:id/files", element: <ReviewsPage /> },
        { path: "issues", element: <IssuesPage /> },
        { path: "issues/:id", element: <IssuesPage /> },
        { path: "scans", element: <ScansPage /> },
        { path: "scans/:id", element: <ScansPage /> },
        { path: "queue", element: <QueuePage /> },
        { path: "queue/:id", element: <QueuePage /> },
        { path: "settings/:category?", element: <SettingsPage /> },
        { path: "agent", element: <AgentPage /> },
        { path: "agent/skills", element: <SkillsPage /> },
        { path: "agent/candidates", element: <CandidatesPage /> },
        { path: "agent/workspaces", element: <WorkspacesPage /> },
        {
          path: "agent/plugins",
          element: <Navigate to="/settings/agent#plugins" replace />,
        },
        { path: "agent/:id", element: <AgentPage /> },
        { path: "activity", element: <ActivityPage /> },
        { path: "activity/:id", element: <ActivityPage /> },
        { path: "github-app", element: <GitHubAppPage /> },
        { path: "billing/*", element: <BillingPage /> },
        { path: "admin/*", element: <AdminPage /> },
        { path: "notifications", element: <NotificationsPage /> },
        { path: "notifications/announcements", element: <NotificationsPage /> },
        {
          path: "notifications/announcements/:id",
          element: <NotificationsPage />,
        },
        { path: "notifications/preferences", element: <NotificationsPage /> },
        { path: "announcements", element: <AnnouncementRedirect /> },
        { path: "announcements/:id", element: <AnnouncementRedirect /> },
        { path: "community", element: <CommunityPage /> },
        { path: "memory", element: <MemoryPage /> },
        { path: "about", element: <Navigate to="/settings/about" replace /> },
        {
          path: "*",
          element: <EmptyState title="页面不存在 / Page not found" />,
        },
      ],
    },
  ],
  { basename: "/app" },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
