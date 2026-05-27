import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthState {
  token: string | null;
  refreshToken: string | null;
  user: {
    id: string;
    email: string;
    username: string;
    role: string;
  } | null;
  login: (token: string, refreshToken: string, user: any) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      refreshToken: null,
      user: null,
      login: (token, refreshToken, user) =>
        set({ token, refreshToken, user }),
      logout: () => {
        set({ token: null, refreshToken: null, user: null });
        window.location.href = "/login";
      },
    }),
    {
      name: "lidar3d-auth",
      partialize: (state) => ({
        token: state.token,
        refreshToken: state.refreshToken,
        user: state.user,
      }),
    }
  )
);
