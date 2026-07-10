import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "../../contexts/auth";

export function AdminGuard() {
  const auth = useAuth();

  if (auth.status !== "authenticated" || !auth.user?.isAdmin) {
    return <Navigate to="/" replace />;
  }

  return <Outlet />;
}
