
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import OwnerLogin from "./pages/OwnerLogin";
import LandingPage from "./pages/LandingPage";
import ProtectedRoute from "./components/ProtectedRoute";
import Dashboard from "./pages/Dashboard";

import "./App.css";
import "./styles/auth.css";
import "./styles/landing.css";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Owner login */}
        <Route path="/login" element={<OwnerLogin />} />

        {/* Protected pages */}
        <Route element={<ProtectedRoute />}>
          <Route path="/home" element={<LandingPage />} />
          <Route path="/dashboard" element={<Dashboard />} />
        </Route>

        {/* Default page */}
        <Route
          path="/"
          element={<Navigate to="/login" replace />}
        />

        {/* Unknown URLs */}
        <Route
          path="*"
          element={<Navigate to="/login" replace />}
        />
      </Routes>
    </BrowserRouter>
  );
}
