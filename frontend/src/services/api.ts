/**
 * Typed API client wrapping fetch with auto-auth headers.
 */

import { useAuthStore } from "@/stores/authStore";

const BASE = import.meta.env.VITE_API_URL || "";

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = useAuthStore.getState().token;
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers as Record<string, string> || {}),
  };

  const res = await fetch(`${BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    useAuthStore.getState().logout();
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

export const apiClient = {
  // Auth
  login: (email: string, password: string) =>
    request<{ access_token: string; refresh_token: string }>(
      "/api/v1/auth/login",
      {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }
    ),

  register: (data: {
    email: string;
    username: string;
    password: string;
    full_name?: string;
  }) =>
    request("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getMe: (token?: string) =>
    request<any>("/api/v1/auth/me", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    }),

  // Datasets
  getDatasets: (params?: {
    page?: number;
    page_size?: number;
    status?: string;
  }) => {
    const q = new URLSearchParams();
    if (params?.page) q.set("page", String(params.page));
    if (params?.page_size) q.set("page_size", String(params.page_size));
    if (params?.status) q.set("status", params.status);
    return request<any>(`/api/v1/datasets?${q}`);
  },

  getDataset: (id: string) => request<any>(`/api/v1/datasets/${id}`),

  updateDataset: (id: string, data: Partial<{ name: string; description: string; is_visible: boolean }>) =>
    request<any>(`/api/v1/datasets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  deleteDataset: (id: string) =>
    request<void>(`/api/v1/datasets/${id}`, { method: "DELETE" }),

  getJobStatus: (datasetId: string) =>
    request<any>(`/api/v1/datasets/${datasetId}/job-status`),
};
