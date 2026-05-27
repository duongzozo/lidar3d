/**
 * Main application layout.
 * Sidebar (dataset list + uploader) + full-screen 3D viewer.
 */

import React, { lazy, Suspense, useRef, useState, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  Map,
  Database,
  Upload,
  LogOut,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  Settings,
  Trash2,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import ChunkedUploader from "@/components/ChunkedUploader";
import type { ViewerHandle } from "@/components/CesiumViewer";
import type { DatasetLayer } from "@/components/CesiumViewer";
import { apiClient } from "@/services/api";

const CesiumViewer = lazy(() => import("@/components/CesiumViewer"));

type Sidebar = "datasets" | "upload" | null;

// ── Dataset list item ─────────────────────────────────────────────────────

function DatasetListItem({
  ds,
  visible,
  onToggle,
  onFlyTo,
  onReprocess,
  onDelete,
}: {
  ds: any;
  visible: boolean;
  onToggle: () => void;
  onFlyTo: () => void;
  onReprocess: (crs: string) => void;
  onDelete: () => void;
}) {
  const [showCrsInput, setShowCrsInput] = React.useState(false);
  const [crsInput, setCrsInput] = React.useState("EPSG:3405");

  const statusColors: Record<string, string> = {
    completed: "bg-green-500",
    processing: "bg-yellow-500 animate-pulse",
    failed: "bg-red-500",
    pending: "bg-gray-500",
    uploading: "bg-blue-500 animate-pulse",
  };

  return (
    <div
      className={`group relative px-3 py-3 rounded-xl border transition-all cursor-pointer ${
        visible
          ? "bg-gray-800/80 border-cyan-500/30"
          : "bg-gray-900/60 border-gray-800 hover:border-gray-700"
      }`}
    >
      <div className="flex items-start gap-3">
        {/* Status dot */}
        <div className="mt-1 flex-shrink-0">
          <div
            className={`w-2 h-2 rounded-full ${
              statusColors[ds.status] || "bg-gray-600"
            }`}
          />
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0" onClick={onFlyTo}>
          <p className="text-sm text-gray-200 font-medium truncate">{ds.name}</p>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-xs text-gray-500">
              {ds.status === "completed"
                ? ds.point_count
                  ? `${(ds.point_count / 1e6).toFixed(1)}M pts`
                  : "Ready"
                : ds.status}
            </span>
            {ds.source_crs && (
              <span className="text-xs text-gray-600 font-mono truncate">
                {ds.source_crs}
              </span>
            )}
          </div>
        </div>

        {/* Toggle visibility */}
        {ds.status === "completed" && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            className={`w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold transition-all ${
              visible
                ? "bg-cyan-600/20 text-cyan-400 hover:bg-cyan-600/30"
                : "bg-gray-700 text-gray-500 hover:text-gray-300"
            }`}
          >
            {visible ? "ON" : "OFF"}
          </button>
        )}

        {/* Reprocess button */}
        {(ds.status === "completed" || ds.status === "failed") && (
          <button
            onClick={(e) => { e.stopPropagation(); setShowCrsInput((v) => !v); }}
            title="Reprocess with CRS override"
            className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-500 hover:text-yellow-400 hover:bg-yellow-400/10 transition-all"
          >
            ↺
          </button>
        )}

        {/* Delete button */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            if (window.confirm(`Delete "${ds.name}"?`)) onDelete();
          }}
          title="Delete dataset"
          className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-600 hover:text-red-400 hover:bg-red-400/10 transition-all"
        >
          <Trash2 size={13} />
        </button>
      </div>

      {/* CRS override input */}
      {showCrsInput && (
        <div className="mt-2 flex gap-2" onClick={(e) => e.stopPropagation()}>
          <input
            type="text"
            value={crsInput}
            onChange={(e) => setCrsInput(e.target.value)}
            placeholder="EPSG:3405"
            className="flex-1 bg-gray-900 border border-yellow-600/40 rounded px-2 py-1 text-xs text-gray-300 focus:outline-none focus:border-yellow-500"
          />
          <button
            onClick={() => { onReprocess(crsInput); setShowCrsInput(false); }}
            className="px-2 py-1 bg-yellow-600 hover:bg-yellow-500 text-xs text-white rounded"
          >
            Run
          </button>
        </div>
      )}
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────

export default function App() {
  const token = useAuthStore((s) => s.token);
  const logout = useAuthStore((s) => s.logout);
  const queryClient = useQueryClient();
  const viewerRef = useRef<ViewerHandle>(null);

  const [sidebar, setSidebar] = useState<Sidebar>("datasets");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [layerVisibility, setLayerVisibility] = useState<
    Record<string, boolean>
  >({});

  // ── Fetch datasets ────────────────────────────────────────────────

  const { data: datasetsData, refetch } = useQuery({
    queryKey: ["datasets"],
    queryFn: () => apiClient.getDatasets(),
    refetchInterval: (query) => {
      const items: any[] = query.state.data?.items ?? [];
      const hasProcessing = items.some(
        (d) => d.status === "processing" || d.status === "uploading"
      );
      return hasProcessing ? 3000 : 15000;
    },
  });

  const datasets: any[] = datasetsData?.items || [];

  // Build layer objects for viewer
  const layers: DatasetLayer[] = datasets
    .filter((d) => d.status === "completed" && d.potree_url)
    .map((d) => ({
      id: d.id,
      name: d.name,
      potreeUrl: d.potree_url
        ? `${import.meta.env.VITE_API_URL || ""}${d.potree_url}`
        : "",
      center: {
        lon: d.center_lon ?? 108,
        lat: d.center_lat ?? 16,
        elevation: d.center_elevation ?? 100,
      },
      visible: layerVisibility[d.id] ?? true,
      elevationMin: d.elevation_min ?? 0,
      elevationMax: d.elevation_max ?? 100,
      hasRgb: d.has_rgb,
      pointCount: d.point_count ?? 0,
      status: d.status,
    }));

  // Add processing datasets (for status display in viewer)
  const allViewerDatasets: DatasetLayer[] = [
    ...layers,
    ...datasets
      .filter((d) => d.status !== "completed")
      .map((d) => ({
        id: d.id,
        name: d.name,
        potreeUrl: "",
        center: { lon: 0, lat: 0, elevation: 0 },
        visible: false,
        elevationMin: 0,
        elevationMax: 0,
        hasRgb: false,
        pointCount: 0,
        status: d.status,
      })),
  ];

  const handleLayerToggle = useCallback((id: string, visible: boolean) => {
    setLayerVisibility((prev) => ({ ...prev, [id]: visible }));
  }, []);

  const handleDatasetReady = useCallback(
    (datasetId: string) => {
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      setLayerVisibility((prev) => ({ ...prev, [datasetId]: true }));
    },
    [queryClient]
  );

  const handleFlyTo = useCallback(
    (ds: any) => {
      const layer = layers.find((l) => l.id === ds.id);
      if (layer) viewerRef.current?.flyTo(layer);
    },
    [layers]
  );

  const handleReprocess = useCallback(
    async (dsId: string, sourceCrs: string) => {
      try {
        await fetch(`/api/v1/datasets/${dsId}/reprocess`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ source_crs: sourceCrs }),
        });
        queryClient.invalidateQueries({ queryKey: ["datasets"] });
      } catch (e) {
        console.error("Reprocess failed", e);
      }
    },
    [token, queryClient]
  );

  const handleDelete = useCallback(
    async (dsId: string) => {
      try {
        await apiClient.deleteDataset(dsId);
        queryClient.invalidateQueries({ queryKey: ["datasets"] });
      } catch (e) {
        console.error("Delete failed", e);
      }
    },
    [queryClient]
  );

  return (
    <div className="flex h-screen w-screen bg-gray-950 overflow-hidden font-sans">
      {/* Sidebar toggle */}
      <button
        onClick={() => setSidebarOpen((o) => !o)}
        className="absolute left-0 top-1/2 -translate-y-1/2 z-30 w-6 h-12 bg-gray-800 border border-gray-700 border-l-0 rounded-r-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-700 transition-all"
        style={{ left: sidebarOpen ? "19rem" : 0, transition: "left 0.3s" }}
      >
        {sidebarOpen ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
      </button>

      {/* ── Sidebar ────────────────────────────────────────────────── */}
      <AnimatePresence>
        {sidebarOpen && (
          <motion.aside
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 304, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ type: "spring", stiffness: 300, damping: 35 }}
            className="flex-shrink-0 flex flex-col bg-gray-950 border-r border-gray-800 overflow-hidden"
            style={{ width: 304 }}
          >
            {/* Logo */}
            <div className="px-4 py-4 border-b border-gray-800">
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 bg-cyan-600 rounded-lg flex items-center justify-center">
                  <Map size={16} className="text-white" />
                </div>
                <div>
                  <p className="text-sm font-bold text-gray-100 tracking-wide">
                    LiDAR3D
                  </p>
                  <p className="text-xs text-gray-500">Web GIS Platform</p>
                </div>
                <button
                  onClick={logout}
                  className="ml-auto text-gray-600 hover:text-gray-300 transition-colors"
                  title="Logout"
                >
                  <LogOut size={15} />
                </button>
              </div>
            </div>

            {/* Tab bar */}
            <div className="flex border-b border-gray-800">
              {[
                { id: "datasets", icon: Database, label: "Datasets" },
                { id: "upload", icon: Upload, label: "Upload" },
              ].map(({ id, icon: Icon, label }) => (
                <button
                  key={id}
                  onClick={() =>
                    setSidebar((s) => (s === id ? null : (id as Sidebar)))
                  }
                  className={`flex-1 py-3 flex flex-col items-center gap-1 text-xs transition-colors ${
                    sidebar === id
                      ? "text-cyan-400 border-b-2 border-cyan-500"
                      : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  <Icon size={16} />
                  {label}
                </button>
              ))}
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto">
              {sidebar === "datasets" && (
                <div className="p-3 space-y-2">
                  <div className="flex items-center justify-between px-1 mb-3">
                    <span className="text-xs text-gray-500">
                      {datasets.length} dataset{datasets.length !== 1 ? "s" : ""}
                    </span>
                    <button
                      onClick={() => refetch()}
                      className="text-gray-600 hover:text-gray-300 transition-colors"
                    >
                      <RefreshCw size={13} />
                    </button>
                  </div>

                  {datasets.length === 0 ? (
                    <div className="text-center py-12 text-gray-600">
                      <Database
                        size={32}
                        className="mx-auto mb-3 opacity-30"
                      />
                      <p className="text-sm">No datasets yet</p>
                      <p className="text-xs mt-1">Upload a LAS/LAZ file</p>
                    </div>
                  ) : (
                    datasets.map((ds) => (
                      <DatasetListItem
                        key={ds.id}
                        ds={ds}
                        visible={layerVisibility[ds.id] ?? true}
                        onToggle={() =>
                          handleLayerToggle(
                            ds.id,
                            !(layerVisibility[ds.id] ?? true)
                          )
                        }
                        onFlyTo={() => handleFlyTo(ds)}
                        onReprocess={(crs) => handleReprocess(ds.id, crs)}
                        onDelete={() => handleDelete(ds.id)}
                      />
                    ))
                  )}
                </div>
              )}

              {sidebar === "upload" && (
                <div className="p-3">
                  <ChunkedUploader onDatasetReady={handleDatasetReady} />
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-4 py-3 border-t border-gray-800">
              <p className="text-xs text-gray-600 text-center">
                LiDAR3D v1.0 · FastAPI + CesiumJS
              </p>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      {/* ── 3D Viewer ──────────────────────────────────────────────── */}
      <main className="flex-1 relative">
        <Suspense
          fallback={
            <div className="absolute inset-0 flex items-center justify-center bg-gray-950">
              <div className="flex flex-col items-center gap-4">
                <div className="w-12 h-12 border-3 border-cyan-500 border-t-transparent rounded-full animate-spin" />
                <p className="text-cyan-400 text-sm font-mono">
                  Loading CesiumJS...
                </p>
              </div>
            </div>
          }
        >
          <CesiumViewer
            ref={viewerRef}
            datasets={allViewerDatasets}
            onLayerToggle={handleLayerToggle}
            cesiumToken={import.meta.env.VITE_CESIUM_TOKEN || ""}
          />
        </Suspense>
      </main>
    </div>
  );
}
