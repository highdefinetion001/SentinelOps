
import { Navigate, Outlet } from "react-router-dom";

export default function ProtectedRoute() {
  const isAuthenticated =
    sessionStorage.getItem("sentinelops-demo-session") === "true";

  return isAuthenticated
    ? <Outlet />
    : <Navigate to="/login" replace />;
}
