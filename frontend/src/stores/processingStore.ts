import { create } from "zustand";

interface ProcessingState {
  processingIds: Set<string>;
  addProcessing: (id: string) => void;
  removeProcessing: (id: string) => void;
  isProcessing: (id: string) => boolean;
}

export const useProcessingStore = create<ProcessingState>((set, get) => ({
  processingIds: new Set(),
  addProcessing: (id) =>
    set((s) => ({ processingIds: new Set([...s.processingIds, id]) })),
  removeProcessing: (id) =>
    set((s) => {
      const next = new Set(s.processingIds);
      next.delete(id);
      return { processingIds: next };
    }),
  isProcessing: (id) => get().processingIds.has(id),
}));
