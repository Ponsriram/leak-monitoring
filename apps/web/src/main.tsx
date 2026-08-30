import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { ApiError } from "./lib/api";
import { AlertsPage } from "./features/alerts/AlertsPage";
import { LoginPage } from "./features/auth/LoginPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { LeaksPage } from "./features/leaks/LeaksPage";
import { IncidentsPage } from "./features/incidents/IncidentsPage";
import { IocPage } from "./features/iocs/IocPage";
import { LiveMapPage } from "./features/map/LiveMapPage";
import { SearchPage } from "./features/search/SearchPage";
import { SourcesPage } from "./features/sources/SourcesPage";
import "./styles/app.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // Retrying a 401 or a 400 just delays the error the user needs to see.
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status < 500) return false;
        return failureCount < 2;
      },
    },
  },
});

/**
 * Routes.
 *
 * The dashboard is a layout route: AppLayout renders the sidebar once and the pages render
 * into its <Outlet/>. Guarding the layout means every child is protected by construction —
 * you cannot add a new page and forget to protect it.
 */
const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/dashboard",
    element: (
      <ProtectedRoute>
        <AppLayout />
      </ProtectedRoute>
    ),
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "leaks", element: <LeaksPage /> },
      { path: "search", element: <SearchPage /> },
      { path: "general", element: <IncidentsPage section="general" /> },
      { path: "ransomware", element: <IncidentsPage section="ransomware" /> },
      { path: "darkweb", element: <IncidentsPage section="darkweb" /> },
      { path: "iocs", element: <IocPage /> },
      { path: "map", element: <LiveMapPage /> },
      { path: "sources", element: <SourcesPage /> },
      { path: "alerts", element: <AlertsPage /> },
    ],
  },
  { path: "/", element: <Navigate to="/dashboard" replace /> },
  { path: "*", element: <Navigate to="/dashboard" replace /> },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
