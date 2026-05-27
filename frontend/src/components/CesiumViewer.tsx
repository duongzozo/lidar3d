/**
 * LiDAR3D Viewer - CesiumJS + Potree integration
 * Production-ready 3D point cloud viewer with:
 *  - Satellite imagery base layer (Bing Maps)
 *  - Terrain (Cesium World Terrain)
 *  - Potree point cloud rendering via 3D Tiles
 *  - Camera fly-to animation
 *  - Measurement tools (distance, area)
 *  - Layer toggle panel
 *  - FPS counter + memory optimization
 *  - Lazy loading of point clouds
 */

import {
  Viewer,
  Ion,
  Cartesian3,
  Math as CesiumMath,
  Color,
  HeadingPitchRange,
  BoundingSphere,
  Cesium3DTileset,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  UrlTemplateImageryProvider,
  defined,
  Entity,
  Cartographic,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  memo,
  forwardRef,
  useImperativeHandle,
} from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Layers,
  Ruler,
  Eye,
  EyeOff,
  Crosshair,
  BarChart3,
  Trash2,
  Maximize2,
  ChevronDown,
  ChevronUp,
  Activity,
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────

export interface DatasetLayer {
  id: string;
  name: string;
  potreeUrl: string; // URL to Potree metadata.json or 3D Tiles tileset.json
  center: { lon: number; lat: number; elevation: number };
  visible: boolean;
  elevationMin: number;
  elevationMax: number;
  hasRgb: boolean;
  pointCount: number;
  status: string;
}

interface ViewerHandle {
  flyTo: (layer: DatasetLayer) => void;
  resetCamera: () => void;
}

interface CesiumViewerProps {
  datasets: DatasetLayer[];
  onLayerToggle: (id: string, visible: boolean) => void;
  cesiumToken: string;
}

// ── Color ramp for elevation visualization ─────────────────────────────────

const ELEVATION_GRADIENT_VERT = `
  uniform float u_elevMin;
  uniform float u_elevMax;
  czm_material czm_getMaterial(czm_materialInput materialInput) {
    czm_material material = czm_getDefaultMaterial(materialInput);
    float t = clamp((materialInput.height - u_elevMin) / (u_elevMax - u_elevMin), 0.0, 1.0);
    // Terrain color ramp: deep blue → cyan → green → yellow → red
    vec3 col;
    if (t < 0.25) col = mix(vec3(0.0, 0.0, 0.5), vec3(0.0, 0.8, 0.8), t * 4.0);
    else if (t < 0.5) col = mix(vec3(0.0, 0.8, 0.8), vec3(0.1, 0.9, 0.1), (t - 0.25) * 4.0);
    else if (t < 0.75) col = mix(vec3(0.1, 0.9, 0.1), vec3(1.0, 0.9, 0.0), (t - 0.5) * 4.0);
    else col = mix(vec3(1.0, 0.9, 0.0), vec3(1.0, 0.0, 0.0), (t - 0.75) * 4.0);
    material.diffuse = col;
    material.alpha = 1.0;
    return material;
  }
`;

// ── FPS Monitor ────────────────────────────────────────────────────────────

function useFpsMonitor(viewer: Viewer | null) {
  const [fps, setFps] = useState(60);
  const frameRef = useRef(0);
  const lastTimeRef = useRef(performance.now());

  useEffect(() => {
    if (!viewer) return;
    const id = viewer.scene.postRender.addEventListener(() => {
      frameRef.current++;
      const now = performance.now();
      if (now - lastTimeRef.current >= 1000) {
        setFps(frameRef.current);
        frameRef.current = 0;
        lastTimeRef.current = now;
      }
    });
    return () => { viewer.scene.postRender.removeEventListener(id); };
  }, [viewer]);

  return fps;
}

// ── Active 3D Tile tilesets ────────────────────────────────────────────────

class TilesetManager {
  private tilesets = new Map<string, Cesium3DTileset>();
  private viewer: Viewer;

  constructor(viewer: Viewer) {
    this.viewer = viewer;
  }

  async load(layer: DatasetLayer): Promise<Cesium3DTileset | null> {
    if (this.tilesets.has(layer.id)) {
      return this.tilesets.get(layer.id)!;
    }
    if (!layer.potreeUrl) {
      console.warn(`No tileset URL for ${layer.name} — skipping load`);
      return null;
    }

    try {
      // Convert Potree URL → 3D Tiles tileset.json
      // PotreeConverter 2.x outputs Cesium-compatible 3D Tiles
      const tilesetUrl = layer.potreeUrl.replace(
        "metadata.json",
        "tileset.json"
      );
      const tileset = await Cesium3DTileset.fromUrl(tilesetUrl, {
        // Octree LOD from backend writer - lower SSE loads finer tiles sooner.
        maximumScreenSpaceError: 4,
        cacheBytes: 1024 * 1024 * 1024,       // 1 GB tile cache for smooth pan
        maximumCacheOverflowBytes: 512 * 1024 * 1024,
        skipLevelOfDetail: false,
        skipScreenSpaceErrorFactor: 8,
        baseScreenSpaceError: 1024,
        skipLevels: 0,
        immediatelyLoadDesiredLevelOfDetail: false,
        loadSiblings: true,                   // smoother pan/zoom
        cullWithChildrenBounds: true,
        dynamicScreenSpaceError: false,
        dynamicScreenSpaceErrorDensity: 0.00278,
        dynamicScreenSpaceErrorFactor: 4.0,
        preloadWhenHidden: true,
      });

      // Point cloud shading — make individual points visible when zoomed in
      tileset.pointCloudShading.attenuation = true;
      tileset.pointCloudShading.geometricErrorScale = 1.0;
      tileset.pointCloudShading.maximumAttenuation = 10;  // bigger points when close
      tileset.pointCloudShading.baseResolution = 0.03;
      tileset.pointCloudShading.eyeDomeLighting = true;
      tileset.pointCloudShading.eyeDomeLightingStrength = 1.0;
      tileset.pointCloudShading.eyeDomeLightingRadius = 1.4;

      tileset.show = layer.visible;
      this.viewer.scene.primitives.add(tileset);
      this.tilesets.set(layer.id, tileset);
      return tileset;
    } catch (err) {
      console.error(`Failed to load tileset for ${layer.name}:`, err);
      return null;
    }
  }

  setVisible(id: string, visible: boolean) {
    const ts = this.tilesets.get(id);
    if (ts) ts.show = visible;
  }

  getBoundingSphere(id: string): BoundingSphere | null {
    const ts = this.tilesets.get(id);
    if (!ts || !ts.boundingSphere) return null;
    return ts.boundingSphere;
  }

  remove(id: string) {
    const ts = this.tilesets.get(id);
    if (ts) {
      this.viewer.scene.primitives.remove(ts);
      this.tilesets.delete(id);
    }
  }

  dispose() {
    for (const [id] of this.tilesets) {
      this.remove(id);
    }
  }
}

// ── Measurement Tool ───────────────────────────────────────────────────────

type MeasureMode = "none" | "distance" | "area";

class MeasurementTool {
  private viewer: Viewer;
  private handler: ScreenSpaceEventHandler | null = null;
  private points: Cartesian3[] = [];
  private entities: Entity[] = [];
  private mode: MeasureMode = "none";
  private onUpdate?: (result: string) => void;

  constructor(viewer: Viewer) {
    this.viewer = viewer;
  }

  start(mode: MeasureMode, onUpdate?: (result: string) => void) {
    this.stop();
    this.mode = mode;
    this.onUpdate = onUpdate;
    this.points = [];

    if (mode === "none") return;

    this.handler = new ScreenSpaceEventHandler(this.viewer.canvas);
    this.handler.setInputAction((event: any) => {
      const cartesian = this.viewer.scene.pickPosition(event.position);
      if (!defined(cartesian)) return;
      this.points.push(cartesian);
      this._addPointEntity(cartesian);
      if (this.points.length >= 2) this._updateMeasurement();
    }, ScreenSpaceEventType.LEFT_CLICK);

    this.handler.setInputAction(() => {
      this.stop();
    }, ScreenSpaceEventType.RIGHT_CLICK);
  }

  private _addPointEntity(pos: Cartesian3) {
    const e = this.viewer.entities.add({
      position: pos,
      point: { pixelSize: 8, color: Color.YELLOW, outlineColor: Color.BLACK, outlineWidth: 2 },
    });
    this.entities.push(e);
  }

  private _updateMeasurement() {
    if (this.mode === "distance" && this.points.length >= 2) {
      const p1 = this.points[this.points.length - 2];
      const p2 = this.points[this.points.length - 1];
      const dist = Cartesian3.distance(p1, p2);
      const label = dist > 1000 ? `${(dist / 1000).toFixed(3)} km` : `${dist.toFixed(2)} m`;
      this.onUpdate?.(label);
    }
    if (this.mode === "area" && this.points.length >= 3) {
      // Approximate area using shoelace formula in lat/lon
      const carto = this.points.map((p) => Cartographic.fromCartesian(p));
      let area = 0;
      for (let i = 0; i < carto.length; i++) {
        const j = (i + 1) % carto.length;
        area +=
          CesiumMath.toDegrees(carto[i].longitude) *
          CesiumMath.toDegrees(carto[j].latitude);
        area -=
          CesiumMath.toDegrees(carto[j].longitude) *
          CesiumMath.toDegrees(carto[i].latitude);
      }
      const areaM2 = Math.abs(area / 2) * 111_000 * 111_000;
      const label =
        areaM2 > 1_000_000
          ? `${(areaM2 / 1_000_000).toFixed(3)} km²`
          : `${areaM2.toFixed(1)} m²`;
      this.onUpdate?.(label);
    }
  }

  stop() {
    this.handler?.destroy();
    this.handler = null;
    this.entities.forEach((e) => this.viewer.entities.remove(e));
    this.entities = [];
    this.points = [];
    this.mode = "none";
  }
}

// ── Main Viewer Component ──────────────────────────────────────────────────

const CesiumViewer = forwardRef<ViewerHandle, CesiumViewerProps>(
  ({ datasets, onLayerToggle, cesiumToken }, ref) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const viewerRef = useRef<Viewer | null>(null);
    const tilesetMgrRef = useRef<TilesetManager | null>(null);
    const measureToolRef = useRef<MeasurementTool | null>(null);
    const loadedLayersRef = useRef<Set<string>>(new Set());

    const [layerPanelOpen, setLayerPanelOpen] = useState(true);
    const [measureMode, setMeasureMode] = useState<MeasureMode>("none");
    const [measurement, setMeasurement] = useState<string>("");
    const [isReady, setIsReady] = useState(false);

    const fps = useFpsMonitor(viewerRef.current);

    // ── Init Cesium Viewer ────────────────────────────────────────────

    useEffect(() => {
      if (!containerRef.current || viewerRef.current) return;

      Ion.defaultAccessToken = cesiumToken || import.meta.env.VITE_CESIUM_TOKEN || "";

      const viewer = new Viewer(containerRef.current, {
        terrainProvider: undefined, // set async below
        baseLayerPicker: false,
        geocoder: false,
        homeButton: false,
        sceneModePicker: false,
        navigationHelpButton: false,
        animation: false,
        timeline: false,
        fullscreenButton: false,
        infoBox: false,
        selectionIndicator: false,
        shadows: false,
        requestRenderMode: false, // continuous for smooth point cloud
        maximumRenderTimeChange: 1000 / 30, // cap at 30fps when idle
        msaaSamples: 2,
      });

      // ── Base imagery ──────────────────────────────────────────────
      // OSM as default (no token needed, reliable)
      // If Ion token present, switch to Esri World Imagery (better quality)
      viewer.imageryLayers.removeAll();
      const hasToken = !!(cesiumToken || import.meta.env.VITE_CESIUM_TOKEN);
      viewer.imageryLayers.addImageryProvider(
        new UrlTemplateImageryProvider({
          url: hasToken
            ? "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            : "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
          credit: hasToken ? "Esri World Imagery" : "© OpenStreetMap contributors",
          minimumLevel: 0,
          maximumLevel: hasToken ? 19 : 18,
        })
      );

      // ── World Terrain ─────────────────────────────────────────────
      (async () => {
        const hasIonToken = !!(cesiumToken || import.meta.env.VITE_CESIUM_TOKEN);
        if (hasIonToken) {
          try {
            const { CesiumTerrainProvider } = await import("cesium");
            const terrain = await CesiumTerrainProvider.fromIonAssetId(1, {
              requestWaterMask: false,
              requestVertexNormals: true,
            });
            viewer.terrainProvider = terrain;
          } catch {
            console.warn("Ion terrain unavailable");
          }
        }
        // Without Ion: ellipsoid terrain (flat) is already Cesium default
        // Globe tiles (OSM) still display correctly on flat ellipsoid
      })();

      // ── Performance settings ─────────────────────────────────────
      viewer.scene.fog.enabled = true;
      viewer.scene.fog.density = 0.0002;
      viewer.scene.globe.maximumScreenSpaceError = 2;
      viewer.scene.postProcessStages.fxaa.enabled = true;
      viewer.scene.debugShowFramesPerSecond = false;

      // Limit camera tilt to avoid going underground
      viewer.scene.screenSpaceCameraController.minimumZoomDistance = 1;
      viewer.scene.screenSpaceCameraController.maximumZoomDistance = 50_000_000;

      // Default camera: global view
      viewer.camera.setView({
        destination: Cartesian3.fromDegrees(108.0, 16.0, 5_000_000),
        orientation: { heading: 0, pitch: -CesiumMath.PI_OVER_TWO, roll: 0 },
      });

      viewerRef.current = viewer;
      tilesetMgrRef.current = new TilesetManager(viewer);
      measureToolRef.current = new MeasurementTool(viewer);
      setIsReady(true);

      return () => {
        tilesetMgrRef.current?.dispose();
        measureToolRef.current?.stop();
        viewer.destroy();
        viewerRef.current = null;
      };
    }, []);

    // ── Load / show / hide layers ─────────────────────────────────────

    useEffect(() => {
      if (!isReady || !tilesetMgrRef.current) return;

      const activeDatasetIds = new Set(datasets.map((layer) => layer.id));
      for (const id of Array.from(loadedLayersRef.current)) {
        if (!activeDatasetIds.has(id)) {
          tilesetMgrRef.current.remove(id);
          loadedLayersRef.current.delete(id);
        }
      }

      for (const layer of datasets) {
        if (layer.status !== "completed") continue;

        if (!loadedLayersRef.current.has(layer.id) && layer.visible) {
          loadedLayersRef.current.add(layer.id);
          tilesetMgrRef.current.load(layer);
        } else if (loadedLayersRef.current.has(layer.id)) {
          tilesetMgrRef.current.setVisible(layer.id, layer.visible);
        }
      }
    }, [datasets, isReady]);

    // ── Fly-to helper ─────────────────────────────────────────────────

    const flyTo = useCallback((layer: DatasetLayer) => {
      const viewer = viewerRef.current;
      const mgr = tilesetMgrRef.current;
      if (!viewer || !mgr) return;

      const bs = mgr.getBoundingSphere(layer.id);
      if (bs) {
        // Fly far enough to see the full point cloud with context
        // Use max(radius * 5, 800m) so corridor/elongated data is fully visible
        const flyDist = Math.max(bs.radius * 5, 800);
        viewer.camera.flyToBoundingSphere(bs, {
          duration: 2.0,
          offset: new HeadingPitchRange(0, CesiumMath.toRadians(-55), flyDist),
        });
      } else {
        // Fly to center coordinates
        viewer.camera.flyTo({
          destination: Cartesian3.fromDegrees(
            layer.center.lon,
            layer.center.lat,
            layer.center.elevation + 500
          ),
          orientation: {
            heading: 0,
            pitch: CesiumMath.toRadians(-40),
            roll: 0,
          },
          duration: 2.5,
        });
      }
    }, []);

    const resetCamera = useCallback(() => {
      viewerRef.current?.camera.flyTo({
        destination: Cartesian3.fromDegrees(108.0, 16.0, 5_000_000),
        orientation: { heading: 0, pitch: -CesiumMath.PI_OVER_TWO, roll: 0 },
        duration: 2,
      });
    }, []);

    useImperativeHandle(ref, () => ({ flyTo, resetCamera }));

    // ── Measurement mode ──────────────────────────────────────────────

    const toggleMeasure = (mode: MeasureMode) => {
      const tool = measureToolRef.current;
      if (!tool) return;
      if (measureMode === mode) {
        tool.stop();
        setMeasureMode("none");
        setMeasurement("");
      } else {
        setMeasurement("");
        tool.start(mode, setMeasurement);
        setMeasureMode(mode);
      }
    };

    // ── FPS color ─────────────────────────────────────────────────────

    const fpsColor =
      fps >= 45 ? "#4ade80" : fps >= 25 ? "#facc15" : "#f87171";

    const completedDatasets = datasets.filter((d) => d.status === "completed");

    return (
      <div className="relative w-full h-full bg-gray-950 overflow-hidden">
        {/* Cesium container */}
        <div ref={containerRef} className="absolute inset-0" />

        {/* Loading overlay */}
        <AnimatePresence>
          {!isReady && (
            <motion.div
              className="absolute inset-0 bg-gray-950 flex items-center justify-center z-50"
              exit={{ opacity: 0 }}
              transition={{ duration: 0.5 }}
            >
              <div className="flex flex-col items-center gap-4">
                <div className="w-16 h-16 border-4 border-cyan-500 border-t-transparent rounded-full animate-spin" />
                <p className="text-cyan-400 font-mono text-sm tracking-wider">
                  INITIALIZING VIEWER
                </p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* HUD: FPS + Memory */}
        <div className="absolute top-3 left-3 z-20 flex items-center gap-2">
          <div
            className="flex items-center gap-1.5 bg-gray-900/90 backdrop-blur px-3 py-1.5 rounded-lg border border-gray-700"
            style={{ fontFamily: "monospace" }}
          >
            <Activity size={12} style={{ color: fpsColor }} />
            <span style={{ color: fpsColor, fontSize: 12 }}>{fps} FPS</span>
          </div>
        </div>

        {/* Measurement result */}
        <AnimatePresence>
          {measurement && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="absolute top-12 left-3 z-20 bg-yellow-500/20 border border-yellow-500/50 backdrop-blur px-3 py-2 rounded-lg"
            >
              <span className="text-yellow-300 font-mono text-sm">
                📏 {measurement}
              </span>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Toolbar */}
        <div className="absolute top-3 right-3 z-20 flex flex-col gap-2">
          {/* Reset camera */}
          <button
            onClick={resetCamera}
            className="w-10 h-10 bg-gray-900/90 backdrop-blur border border-gray-700 rounded-lg flex items-center justify-center text-gray-300 hover:text-white hover:border-cyan-500 transition-all"
            title="Reset camera"
          >
            <Maximize2 size={16} />
          </button>

          {/* Distance measure */}
          <button
            onClick={() => toggleMeasure("distance")}
            className={`w-10 h-10 backdrop-blur border rounded-lg flex items-center justify-center transition-all ${
              measureMode === "distance"
                ? "bg-cyan-600 border-cyan-400 text-white"
                : "bg-gray-900/90 border-gray-700 text-gray-300 hover:text-white hover:border-cyan-500"
            }`}
            title="Measure distance (click points, right-click to stop)"
          >
            <Ruler size={16} />
          </button>

          {/* Area measure */}
          <button
            onClick={() => toggleMeasure("area")}
            className={`w-10 h-10 backdrop-blur border rounded-lg flex items-center justify-center transition-all ${
              measureMode === "area"
                ? "bg-purple-600 border-purple-400 text-white"
                : "bg-gray-900/90 border-gray-700 text-gray-300 hover:text-white hover:border-purple-500"
            }`}
            title="Measure area (3+ points, right-click to finish)"
          >
            <Crosshair size={16} />
          </button>

          {/* Layer panel toggle */}
          <button
            onClick={() => setLayerPanelOpen((p) => !p)}
            className={`w-10 h-10 backdrop-blur border rounded-lg flex items-center justify-center transition-all ${
              layerPanelOpen
                ? "bg-gray-800 border-gray-500 text-white"
                : "bg-gray-900/90 border-gray-700 text-gray-300 hover:text-white"
            }`}
            title="Toggle layer panel"
          >
            <Layers size={16} />
          </button>
        </div>

        {/* Layer Panel */}
        <AnimatePresence>
          {layerPanelOpen && (
            <motion.div
              initial={{ x: 300, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: 300, opacity: 0 }}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
              className="absolute top-16 right-3 z-20 w-72 bg-gray-900/95 backdrop-blur-sm border border-gray-700 rounded-xl overflow-hidden shadow-2xl"
            >
              <div className="px-4 py-3 border-b border-gray-700 flex items-center gap-2">
                <Layers size={14} className="text-cyan-400" />
                <span className="text-sm font-semibold text-gray-200">
                  Point Cloud Layers
                </span>
                <span className="ml-auto text-xs text-gray-500">
                  {completedDatasets.length} datasets
                </span>
              </div>

              <div className="max-h-96 overflow-y-auto">
                {completedDatasets.length === 0 ? (
                  <div className="px-4 py-8 text-center text-gray-500 text-sm">
                    No processed datasets
                  </div>
                ) : (
                  completedDatasets.map((layer) => (
                    <LayerItem
                      key={layer.id}
                      layer={layer}
                      onToggle={onLayerToggle}
                      onFlyTo={flyTo}
                    />
                  ))
                )}
              </div>

              {/* Processing datasets */}
              {datasets.filter((d) => d.status === "processing").length > 0 && (
                <div className="border-t border-gray-700 px-4 py-2">
                  <p className="text-xs text-gray-500 mb-2">Processing...</p>
                  {datasets
                    .filter((d) => d.status === "processing")
                    .map((d) => (
                      <div
                        key={d.id}
                        className="flex items-center gap-2 py-1.5"
                      >
                        <div className="w-3 h-3 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin" />
                        <span className="text-xs text-gray-400 truncate">
                          {d.name}
                        </span>
                      </div>
                    ))}
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }
);

// ── Layer Item ─────────────────────────────────────────────────────────────

const LayerItem = memo(function LayerItem({
  layer,
  onToggle,
  onFlyTo,
}: {
  layer: DatasetLayer;
  onToggle: (id: string, v: boolean) => void;
  onFlyTo: (layer: DatasetLayer) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const pts =
    layer.pointCount > 1_000_000
      ? `${(layer.pointCount / 1_000_000).toFixed(1)}M pts`
      : layer.pointCount > 1_000
      ? `${(layer.pointCount / 1_000).toFixed(0)}k pts`
      : `${layer.pointCount} pts`;

  return (
    <div className="border-b border-gray-800 last:border-0">
      <div className="flex items-center gap-2 px-4 py-3 hover:bg-gray-800/50 transition-colors">
        {/* Visibility toggle */}
        <button
          onClick={() => onToggle(layer.id, !layer.visible)}
          className={`w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 transition-colors ${
            layer.visible
              ? "bg-cyan-600/20 text-cyan-400"
              : "bg-gray-800 text-gray-600"
          }`}
        >
          {layer.visible ? <Eye size={13} /> : <EyeOff size={13} />}
        </button>

        {/* Info */}
        <div className="flex-1 min-w-0" onClick={() => setExpanded((e) => !e)}>
          <p className="text-sm text-gray-200 truncate font-medium">
            {layer.name}
          </p>
          <p className="text-xs text-gray-500">{pts}</p>
        </div>

        {/* Fly-to */}
        <button
          onClick={() => onFlyTo(layer)}
          className="w-7 h-7 rounded-md bg-gray-800 text-gray-400 flex items-center justify-center hover:bg-cyan-800/30 hover:text-cyan-300 transition-colors flex-shrink-0"
          title="Fly to dataset"
        >
          <Crosshair size={12} />
        </button>

        {/* Expand */}
        <button
          onClick={() => setExpanded((e) => !e)}
          className="text-gray-600 hover:text-gray-400 transition-colors"
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-3 space-y-2">
              <MetaRow label="Elevation" value={`${layer.elevationMin.toFixed(1)} – ${layer.elevationMax.toFixed(1)} m`} />
              <MetaRow label="Colors" value={layer.hasRgb ? "RGB" : "Elevation"} />
              <MetaRow label="Center" value={`${layer.center.lat.toFixed(4)}°, ${layer.center.lon.toFixed(4)}°`} />

              {/* Elevation gradient preview */}
              <div>
                <p className="text-xs text-gray-500 mb-1">Elevation gradient</p>
                <div
                  className="h-3 rounded w-full"
                  style={{
                    background:
                      "linear-gradient(to right, #00007f, #00cfcf, #1ae41a, #e6e600, #ff0000)",
                  }}
                />
                <div className="flex justify-between text-xs text-gray-600 mt-0.5">
                  <span>{layer.elevationMin.toFixed(0)}m</span>
                  <span>{layer.elevationMax.toFixed(0)}m</span>
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
});

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-300 font-mono">{value}</span>
    </div>
  );
}

CesiumViewer.displayName = "CesiumViewer";
export default CesiumViewer;
export type { ViewerHandle };
