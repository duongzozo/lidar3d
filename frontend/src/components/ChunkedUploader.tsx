/**
 * ChunkedUploader - handles large file uploads (4GB+) in chunks.
 * Shows real-time progress and processing status via WebSocket.
 */

import { useCallback, useRef, useState } from "react";
import { useDropzone } from "react-dropzone";
import { motion, AnimatePresence } from "framer-motion";
import {
  Upload,
  CloudUpload,
  CheckCircle,
  XCircle,
  FileText,
  Loader2,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { useProcessingStore } from "@/stores/processingStore";

const API = import.meta.env.VITE_API_URL || "";
// Derive WebSocket base from current page origin so it works behind nginx
const WS_BASE = import.meta.env.VITE_WS_URL ||
  (typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`
    : "");
const CHUNK_SIZE = 64 * 1024 * 1024; // 64 MB

interface UploadState {
  phase:
    | "idle"
    | "initializing"
    | "uploading"
    | "processing"
    | "done"
    | "error";
  uploadProgress: number;
  processingProgress: number;
  processingStep: string;
  error: string | null;
  datasetId: string | null;
  sessionId: string | null;
  totalChunks: number;
  uploadedChunks: number;
}

const INITIAL: UploadState = {
  phase: "idle",
  uploadProgress: 0,
  processingProgress: 0,
  processingStep: "",
  error: null,
  datasetId: null,
  sessionId: null,
  totalChunks: 0,
  uploadedChunks: 0,
};

interface ChunkedUploaderProps {
  onDatasetReady?: (datasetId: string) => void;
}

export default function ChunkedUploader({
  onDatasetReady,
}: ChunkedUploaderProps) {
  const token = useAuthStore((s) => s.token);
  const addProcessing = useProcessingStore((s) => s.addProcessing);
  const [state, setState] = useState<UploadState>(INITIAL);
  const [datasetName, setDatasetName] = useState("");
  const [sourceCrs, setSourceCrs] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const abortRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);

  const authHeader = { Authorization: `Bearer ${token}` };

  // ── Upload flow ────────────────────────────────────────────────────

  const startUpload = async (f: File, name: string) => {
    abortRef.current = false;
    setState({ ...INITIAL, phase: "initializing" });

    try {
      // 1. Init session
      const initRes = await fetch(`${API}/api/v1/upload/init`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeader },
        body: JSON.stringify({
          filename: f.name,
          total_size_bytes: f.size,
          chunk_size_bytes: CHUNK_SIZE,
          dataset_name: name || f.name,
          dataset_description: "",
          source_crs: sourceCrs.trim() || null,
        }),
      });

      if (!initRes.ok) throw new Error(`Init failed: ${initRes.statusText}`);
      const { session_id, total_chunks } = await initRes.json();

      setState((s) => ({
        ...s,
        phase: "uploading",
        sessionId: session_id,
        totalChunks: total_chunks,
      }));

      // 2. Upload chunks with concurrency=3
      await uploadChunks(f, session_id, total_chunks);

      // 3. Complete session → trigger processing
      const completeRes = await fetch(
        `${API}/api/v1/upload/${session_id}/complete`,
        {
          method: "POST",
          headers: authHeader,
        }
      );
      if (!completeRes.ok) throw new Error("Failed to complete upload");
      const { dataset_id } = await completeRes.json();

      setState((s) => ({
        ...s,
        phase: "processing",
        datasetId: dataset_id,
        processingProgress: 0,
        processingStep: "Queued",
      }));
      addProcessing(dataset_id);

      // 4. Subscribe to WebSocket progress
      watchProgress(dataset_id);
    } catch (err: any) {
      setState((s) => ({ ...s, phase: "error", error: err.message }));
    }
  };

  const uploadChunks = async (
    f: File,
    sessionId: string,
    totalChunks: number
  ) => {
    const CONCURRENCY = 3;
    let uploaded = 0;

    const queue: number[] = Array.from({ length: totalChunks }, (_, i) => i);

    async function uploadOne(idx: number) {
      if (abortRef.current) return;
      const start = idx * CHUNK_SIZE;
      const blob = f.slice(start, start + CHUNK_SIZE);
      const form = new FormData();
      form.append("file", blob, `chunk_${idx}`);

      let retries = 3;
      while (retries-- > 0) {
        const res = await fetch(
          `${API}/api/v1/upload/${sessionId}/chunk?index=${idx}`,
          {
            method: "PUT",
            headers: authHeader,
            body: form,
          }
        );
        if (res.ok) {
          uploaded++;
          setState((s) => ({
            ...s,
            uploadedChunks: uploaded,
            uploadProgress: Math.round((uploaded / totalChunks) * 100),
          }));
          return;
        }
        if (retries === 0) throw new Error(`Chunk ${idx} failed`);
        await new Promise((r) => setTimeout(r, 1000));
      }
    }

    // Pool-based concurrency
    const running: Promise<void>[] = [];
    for (const idx of queue) {
      const p = uploadOne(idx).then(() => {
        running.splice(running.indexOf(p), 1);
      });
      running.push(p);
      if (running.length >= CONCURRENCY) {
        await Promise.race(running);
      }
    }
    await Promise.all(running);
  };

  const watchProgress = (datasetId: string) => {
    const wsUrl = `${WS_BASE}/ws/progress/${datasetId}?token=${token}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "ping") return;
        setState((s) => ({
          ...s,
          processingProgress: msg.progress ?? s.processingProgress,
          processingStep: msg.step ?? s.processingStep,
          phase:
            msg.status === "completed"
              ? "done"
              : msg.status === "failed"
              ? "error"
              : "processing",
          error: msg.status === "failed" ? msg.step : null,
        }));
        if (msg.status === "completed") {
          ws.close();
          onDatasetReady?.(datasetId);
        }
        if (msg.status === "failed") ws.close();
      } catch {}
    };

    ws.onerror = () => {
      setState((s) => ({
        ...s,
        phase: "error",
        error: "WebSocket connection failed",
      }));
    };
  };

  const cancel = () => {
    abortRef.current = true;
    wsRef.current?.close();
    setState(INITIAL);
    setFile(null);
  };

  // ── Dropzone ───────────────────────────────────────────────────────

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    accept: { "application/octet-stream": [".las", ".laz"] },
    maxFiles: 1,
    disabled: state.phase !== "idle",
    onDropAccepted: ([f]) => {
      setFile(f);
      if (!datasetName) setDatasetName(f.name.replace(/\.(las|laz)$/i, ""));
    },
  });

  const handleSubmit = () => {
    if (file && datasetName && state.phase === "idle") {
      startUpload(file, datasetName);
    }
  };

  const pct =
    state.phase === "uploading"
      ? state.uploadProgress
      : state.phase === "processing" || state.phase === "done"
      ? state.processingProgress
      : 0;

  const statusColor = {
    idle: "text-gray-400",
    initializing: "text-cyan-400",
    uploading: "text-blue-400",
    processing: "text-purple-400",
    done: "text-green-400",
    error: "text-red-400",
  }[state.phase];

  const statusLabel = {
    idle: file ? "Ready to upload" : "Drop a LAS/LAZ file",
    initializing: "Initializing...",
    uploading: `Uploading ${state.uploadedChunks}/${state.totalChunks} chunks`,
    processing: state.processingStep || "Processing...",
    done: "Processing complete!",
    error: state.error || "Error occurred",
  }[state.phase];

  return (
    <div className="flex flex-col gap-4 p-4 bg-gray-900 rounded-xl border border-gray-700">
      <h3 className="text-sm font-semibold text-gray-200 flex items-center gap-2">
        <Upload size={14} className="text-cyan-400" />
        Upload LAS/LAZ File
      </h3>

      {/* Dropzone */}
      <div
        {...getRootProps()}
        className={`relative border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
          isDragActive
            ? "border-cyan-500 bg-cyan-500/10"
            : file
            ? "border-green-600 bg-green-900/10"
            : "border-gray-600 hover:border-gray-500 hover:bg-gray-800/50"
        } ${state.phase !== "idle" ? "pointer-events-none opacity-60" : ""}`}
      >
        <input {...getInputProps()} />
        <div className="flex flex-col items-center gap-3">
          {file ? (
            <FileText size={32} className="text-green-400" />
          ) : (
            <CloudUpload
              size={32}
              className={isDragActive ? "text-cyan-400" : "text-gray-500"}
            />
          )}
          <div>
            {file ? (
              <>
                <p className="text-sm text-gray-200 font-medium">{file.name}</p>
                <p className="text-xs text-gray-500">
                  {(file.size / 1024 / 1024 / 1024).toFixed(2)} GB
                </p>
              </>
            ) : (
              <>
                <p className="text-sm text-gray-400">
                  {isDragActive ? "Drop here" : "Drag & drop .las/.laz file"}
                </p>
                <p className="text-xs text-gray-600 mt-1">
                  Supports files up to 10 GB
                </p>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Dataset name */}
      {file && state.phase === "idle" && (
        <input
          type="text"
          value={datasetName}
          onChange={(e) => setDatasetName(e.target.value)}
          placeholder="Dataset name..."
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-cyan-500"
        />
      )}

      {/* CRS override */}
      {file && state.phase === "idle" && (
        <div>
          <input
            type="text"
            value={sourceCrs}
            onChange={(e) => setSourceCrs(e.target.value)}
            placeholder="CRS (optional) — e.g. EPSG:3405 for VN-2000 TM-3 105°"
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-cyan-500"
          />
          <p className="mt-1 text-xs text-gray-600">
            Để trống nếu file đã có thông tin CRS. VN-2000: EPSG:3405 (CM 105°) hoặc EPSG:3406 (CM 108°). Cũng hỗ trợ chuỗi PROJ4 hoặc WKT từ file .prj.
          </p>
        </div>
      )}

      {/* Progress bar */}
      <AnimatePresence>
        {state.phase !== "idle" && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="space-y-2"
          >
            <div className="flex items-center justify-between text-xs">
              <span className={statusColor}>{statusLabel}</span>
              <span className="text-gray-500 font-mono">
                {pct.toFixed(0)}%
              </span>
            </div>
            <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
              <motion.div
                className={`h-full rounded-full ${
                  state.phase === "done"
                    ? "bg-green-500"
                    : state.phase === "error"
                    ? "bg-red-500"
                    : state.phase === "processing"
                    ? "bg-purple-500"
                    : "bg-cyan-500"
                }`}
                initial={{ width: 0 }}
                animate={{ width: `${pct}%` }}
                transition={{ duration: 0.3 }}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Action buttons */}
      <div className="flex gap-2">
        {state.phase === "idle" && file && (
          <button
            onClick={handleSubmit}
            disabled={!datasetName}
            className="flex-1 py-2.5 bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm font-semibold rounded-lg transition-colors flex items-center justify-center gap-2"
          >
            <Upload size={14} />
            Upload & Process
          </button>
        )}

        {["uploading", "processing", "initializing"].includes(state.phase) && (
          <button
            onClick={cancel}
            className="flex-1 py-2.5 bg-red-900/40 hover:bg-red-800/50 border border-red-800 text-red-400 text-sm rounded-lg transition-colors flex items-center justify-center gap-2"
          >
            <XCircle size={14} />
            Cancel
          </button>
        )}

        {(state.phase === "done" || state.phase === "error") && (
          <button
            onClick={() => {
              setState(INITIAL);
              setFile(null);
              setDatasetName("");
            }}
            className="flex-1 py-2.5 bg-gray-800 hover:bg-gray-700 text-gray-300 text-sm rounded-lg transition-colors"
          >
            Upload another
          </button>
        )}
      </div>

      {/* Status icon */}
      {state.phase === "done" && (
        <div className="flex items-center gap-2 text-green-400 text-sm">
          <CheckCircle size={16} />
          <span>Dataset ready! Switch to viewer to explore.</span>
        </div>
      )}
    </div>
  );
}
