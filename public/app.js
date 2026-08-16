// State variables
let html5QrCode = null;
let cameras = [];
let currentCameraId = null;
let isScanning = false;
let hasHotSwapped = false;

let lastScannedBarcode = "";
let lastScanTime = 0;
const SCAN_THROTTLE_MS = 6000; // Wait 6s before allowing another scan of the same barcode

// DOM Elements
const cameraSelect = document.getElementById("camera-select");
const toggleCameraBtn = document.getElementById("toggle-camera-btn");
const viewfinderOverlay = document.getElementById("custom-viewfinder");
const statusOverlay = document.getElementById("status-overlay");
const statusOverlayText = document.getElementById("status-overlay-text");

const manualBarcodeInp = document.getElementById("manual-barcode");
const manualLookupBtn = document.getElementById("manual-lookup-btn");

const productDetailCard = document.getElementById("product-detail-card");
const historyCard = document.getElementById("history-card");
const historyList = document.getElementById("history-list");
const refreshHistoryBtn = document.getElementById("refresh-history-btn");
const backToHistoryBtn = document.getElementById("back-to-history-btn");

// Init on load
document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
  loadScanHistory();
  initializeCameraList();
  setupVideoAutoplayObserver();
  setupThemeToggle();
});

// Event Listeners setup
function setupEventListeners() {
  toggleCameraBtn.addEventListener("click", toggleCamera);
  manualLookupBtn.addEventListener("click", handleManualLookup);
  manualBarcodeInp.addEventListener("keypress", (e) => {
    if (e.key === "Enter") handleManualLookup();
  });
  
  refreshHistoryBtn.addEventListener("click", loadScanHistory);
  backToHistoryBtn.addEventListener("click", showHistoryPanel);

  // Switch cameras dynamically on selection change
  cameraSelect.addEventListener("change", handleCameraChange);
}

async function handleCameraChange() {
  currentCameraId = cameraSelect.value;
  if (isScanning) {
    console.log("Switching camera to:", currentCameraId);
    await stopScanning();
    await startScanning();
  }
}

// Camera initialization
async function initializeCameraList() {
  toggleCameraBtn.disabled = false; // Always keep the start button active

  // If the library hasn't loaded yet, check again in a bit
  if (typeof Html5Qrcode === "undefined") {
    console.warn("Html5Qrcode library not ready, retrying...");
    cameraSelect.innerHTML = '<option value="environment">Loading scanner...</option>';
    setTimeout(initializeCameraList, 500);
    return;
  }

  try {
    // Check if camera permission is available or already granted
    cameras = await Html5Qrcode.getCameras();
    cameraSelect.innerHTML = "";
    
    if (cameras && cameras.length > 0) {
      // Add default constraints options linked directly to generic constraints for Safari/iOS compatibility
      const defaultBackOpt = document.createElement("option");
      defaultBackOpt.value = "environment";
      defaultBackOpt.text = "Default Back Camera (Recommended)";
      cameraSelect.appendChild(defaultBackOpt);

      const defaultFrontOpt = document.createElement("option");
      defaultFrontOpt.value = "user";
      defaultFrontOpt.text = "Default Front Camera";
      cameraSelect.appendChild(defaultFrontOpt);

      cameras.forEach((camera, index) => {
        const option = document.createElement("option");
        option.value = camera.id;
        option.text = camera.label || `Camera ${index + 1}`;
        cameraSelect.appendChild(option);
      });
      
      cameraSelect.value = "environment";
      currentCameraId = "environment";
    } else {
      addDefaultOptions();
    }
  } catch (err) {
    console.warn("Could not retrieve camera list initially (this is normal before permission is granted):", err);
    addDefaultOptions();
  }
}

function addDefaultOptions() {
  cameraSelect.innerHTML = "";
  
  const optionBack = document.createElement("option");
  optionBack.value = "environment";
  optionBack.text = "Default Back Camera (Recommended)";
  cameraSelect.appendChild(optionBack);

  const optionFront = document.createElement("option");
  optionFront.value = "user";
  optionFront.text = "Default Front Camera";
  cameraSelect.appendChild(optionFront);
  
  cameraSelect.value = "environment";
  currentCameraId = "environment";
}

// Toggle Start/Stop scanning
async function toggleCamera() {
  if (isScanning) {
    await stopScanning();
  } else {
    currentCameraId = cameraSelect.value;
    if (!currentCameraId) {
      alert("Please select a camera first.");
      return;
    }
    await startScanning();
  }
}

// Watcher to force video elements to play inline and muted (fixes iOS/Safari black screen)
function setupVideoAutoplayObserver() {
  if (typeof MutationObserver === "undefined") return;
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (node.tagName === "VIDEO") {
          node.setAttribute("playsinline", "true");
          node.setAttribute("webkit-playsinline", "true");
          node.setAttribute("muted", "true");
          node.muted = true;
          node.play().catch(e => console.warn("[Video Observer] play() failed or auto-played:", e));
        } else if (node.querySelectorAll) {
          const videos = node.querySelectorAll("video");
          videos.forEach(v => {
            v.setAttribute("playsinline", "true");
            v.setAttribute("webkit-playsinline", "true");
            v.setAttribute("muted", "true");
            v.muted = true;
            v.play().catch(e => console.warn("[Video Observer] nested play() failed or auto-played:", e));
          });
        }
      });
    });
  });

  const readerEl = document.getElementById("reader");
  if (readerEl) {
    observer.observe(readerEl, { childList: true, subtree: true });
    console.log("[Video Observer] Successfully attached to #reader");
  }
}

async function startScanning() {
  if (typeof Html5Qrcode === "undefined") {
    alert("The scanner library is still loading. Please try again in a moment.");
    return;
  }

  if (!html5QrCode) {
    // Resolve supported formats namespace dynamically
    const formats = (typeof Html5QrcodeSupportedFormats !== "undefined")
      ? Html5QrcodeSupportedFormats
      : (Html5Qrcode.SupportedFormats || {});

    // Restrict scan formats to EAN/UPC barcodes, Code 128, and QR to optimize decoder CPU load
    html5QrCode = new Html5Qrcode("reader", {
      formatsToSupport: [
        formats.EAN_13,
        formats.EAN_8,
        formats.UPC_A,
        formats.UPC_E,
        formats.CODE_128,
        formats.QR_CODE
      ].filter(Boolean)
    });
  }

  toggleCameraBtn.textContent = "Stop Camera";
  toggleCameraBtn.classList.remove("btn-primary");
  toggleCameraBtn.classList.add("btn-secondary");
  viewfinderOverlay.style.display = "block";
  isScanning = true;

  // Dynamically resolve target camera constraints and configuration
  let cameraIdOrConstraints;
  let videoConstraints = {};

  if (currentCameraId === "environment") {
    cameraIdOrConstraints = { facingMode: "environment" };
    videoConstraints = {
      facingMode: "environment",
      width: { ideal: 1280 },
      height: { ideal: 720 }
    };
  } else if (currentCameraId === "user") {
    cameraIdOrConstraints = { facingMode: "user" };
    videoConstraints = {
      facingMode: "user",
      width: { ideal: 1280 },
      height: { ideal: 720 }
    };
  } else {
    // Specific camera device ID
    cameraIdOrConstraints = currentCameraId; // Pass the camera ID directly as string
    videoConstraints = {
      deviceId: { exact: currentCameraId },
      width: { ideal: 1280 },
      height: { ideal: 720 }
    };
  }

  const config = {
    fps: 25, // Higher frame rate for snappier feedback
    qrbox: (width, height) => {
      // Wide scan window (80% width, 50% height) aligned with CSS viewfinder brackets
      const qrWidth = Math.floor(width * 0.80);
      const qrHeight = Math.floor(height * 0.50);
      return { width: qrWidth, height: qrHeight };
    },
    experimentalFeatures: {
      useBarCodeDetectorIfSupported: true
    },
    videoConstraints: videoConstraints
  };

  try {
    console.log("Starting camera with target:", cameraIdOrConstraints, "and config:", config);
    await html5QrCode.start(
      cameraIdOrConstraints,
      config,
      onBarcodeDetected,
      (errorMessage) => {
        // Verbose scan error is normal for frame-by-frame check, ignore
      }
    );
    
    // Refresh camera device names once permission is granted and camera is active
    setTimeout(refreshCameraNamesAfterPermission, 500);

  } catch (startErr) {
    console.warn("Failed to start with primary constraints. Initiating fallback 1 (no custom resolution):", startErr);
    
    try {
      // Fallback 1: Try without custom width/height constraints
      let fallbackVideoConstraints = {};
      if (currentCameraId === "environment" || currentCameraId === "user") {
        fallbackVideoConstraints = { facingMode: currentCameraId };
      } else {
        fallbackVideoConstraints = { deviceId: { exact: currentCameraId } };
      }

      const fallbackConfig = {
        fps: 25,
        qrbox: (width, height) => {
          const qrWidth = Math.floor(width * 0.80);
          const qrHeight = Math.floor(height * 0.50);
          return { width: qrWidth, height: qrHeight };
        },
        experimentalFeatures: {
          useBarCodeDetectorIfSupported: true
        },
        videoConstraints: fallbackVideoConstraints
      };

      await html5QrCode.start(
        cameraIdOrConstraints,
        fallbackConfig,
        onBarcodeDetected,
        (errorMessage) => {}
      );
      
      setTimeout(refreshCameraNamesAfterPermission, 500);

    } catch (fallbackErr) {
      console.warn("Failed fallback 1. Initiating ultimate fallback (no videoConstraints override in config):", fallbackErr);
      
      try {
        const ultimateConfig = {
          fps: 25,
          qrbox: (width, height) => {
            const qrWidth = Math.floor(width * 0.80);
            const qrHeight = Math.floor(height * 0.50);
            return { width: qrWidth, height: qrHeight };
          },
          experimentalFeatures: {
            useBarCodeDetectorIfSupported: true
          }
        };

        await html5QrCode.start(
          cameraIdOrConstraints,
          ultimateConfig,
          onBarcodeDetected,
          (errorMessage) => {}
        );
        
        setTimeout(refreshCameraNamesAfterPermission, 500);

      } catch (ultimateErr) {
        console.error("Failed all methods to start scanning", ultimateErr);
        alert(`Error starting camera: ${ultimateErr.message || ultimateErr}`);
        await stopScanning();
      }
    }
  }
}

async function refreshCameraNamesAfterPermission() {
  try {
    if (typeof Html5Qrcode === "undefined") return;
    const freshCameras = await Html5Qrcode.getCameras();
    if (freshCameras && freshCameras.length > 0) {
      const prevVal = cameraSelect.value;
      cameraSelect.innerHTML = "";
      
      const defaultBackOpt = document.createElement("option");
      defaultBackOpt.value = "environment";
      defaultBackOpt.text = "Default Back Camera (Recommended)";
      cameraSelect.appendChild(defaultBackOpt);

      const defaultFrontOpt = document.createElement("option");
      defaultFrontOpt.value = "user";
      defaultFrontOpt.text = "Default Front Camera";
      cameraSelect.appendChild(defaultFrontOpt);

      freshCameras.forEach((camera, index) => {
        const option = document.createElement("option");
        option.value = camera.id;
        option.text = camera.label || `Camera ${index + 1}`;
        cameraSelect.appendChild(option);
      });
      
      // Restore the active camera selection
      cameraSelect.value = prevVal;
      currentCameraId = cameraSelect.value;
    }
  } catch (e) {
    console.warn("Could not refresh camera names:", e);
  }
}

async function stopScanning() {
  if (html5QrCode && isScanning) {
    try {
      await html5QrCode.stop();
    } catch (err) {
      console.error("Failed to stop scanning", err);
    }
  }
  
  toggleCameraBtn.textContent = "Start Camera";
  toggleCameraBtn.classList.remove("btn-secondary");
  toggleCameraBtn.classList.add("btn-primary");
  viewfinderOverlay.style.display = "none";
  isScanning = false;
}

// Callback when barcode is detected by browser library
function onBarcodeDetected(decodedText, decodedResult) {
  const now = Date.now();
  const format = decodedResult?.result?.format?.formatName || "BARCODE";
  
  // Throttle scanning the same item repeatedly
  if (decodedText === lastScannedBarcode && (now - lastScanTime) < SCAN_THROTTLE_MS) {
    return;
  }

  lastScannedBarcode = decodedText;
  lastScanTime = now;

  console.log(`Scanned ${format}: ${decodedText}`);
  
  // Play subtle scan beep sound (synthesized client side)
  playBeep();

  // Perform backend lookup
  performLookup(decodedText);
}

function playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime); // A5 note
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.15);
    
    osc.connect(gain);
    gain.connect(ctx.destination);
    
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
  } catch (e) {
    // AudioContext blocked or not supported, ignore
  }
}

// Trigger product lookup
async function performLookup(barcode) {
  showStatusOverlay("Retrieving details from 7 API sources...");
  
  try {
    const response = await fetch(`/api/lookup?barcode=${encodeURIComponent(barcode)}`);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    
    const data = await response.json();
    hideStatusOverlay();
    
    if (data.status === "found" && data.record) {
      displayProduct(data.record);
      saveToLocalHistory(
        data.barcode, 
        "found", 
        data.record.source || "Open Food Facts", 
        data.record.display || data.record.name
      );
    } else {
      displayNotFound(barcode);
      saveToLocalHistory(barcode, "not_found", "None", "Product Not Found");
    }
    
    // Refresh history panel
    loadScanHistory();
  } catch (err) {
    console.error("Lookup request failed", err);
    hideStatusOverlay();
    alert(`Lookup failed: ${err.message}`);
  }
}

// Manual form lookup
function handleManualLookup() {
  const code = manualBarcodeInp.value.trim();
  if (!code) {
    alert("Please enter a valid barcode");
    return;
  }
  performLookup(code);
}

// Status overlay helpers
function showStatusOverlay(text) {
  statusOverlayText.textContent = text;
  statusOverlay.classList.add("active");
}

function hideStatusOverlay() {
  statusOverlay.classList.remove("active");
}

// Render product details
function displayProduct(record) {
  // Toggle views
  productDetailCard.classList.remove("hidden");
  historyCard.classList.add("hidden");

  // Basic Details
  document.getElementById("product-name").textContent = record.name || "Unknown Product";
  document.getElementById("product-brand").textContent = record.brand || "";
  document.getElementById("product-barcode").textContent = `Barcode: ${record.barcode}`;
  document.getElementById("product-source-badge").textContent = record.source || "External DB";
  
  // Image Setup
  const imgEl = document.getElementById("product-image");
  const fallbackEl = document.getElementById("product-image-fallback");
  if (record.image_url) {
    imgEl.src = record.image_url;
    imgEl.style.display = "block";
    fallbackEl.style.display = "none";
  } else {
    imgEl.src = "";
    imgEl.style.display = "none";
    fallbackEl.style.display = "flex";
  }

  // Nutri-Score badge
  const nsBadge = document.getElementById("nutriscore-badge");
  const nsLetter = document.getElementById("nutriscore-letter");
  nsBadge.className = "badge-nutriscore"; // Reset classes
  if (record.nutriscore) {
    const score = record.nutriscore.toLowerCase();
    nsLetter.textContent = score;
    nsBadge.classList.add(score);
    nsBadge.classList.remove("hidden");
  } else {
    nsBadge.classList.add("hidden");
  }

  // NOVA Badge
  const novaBadge = document.getElementById("nova-badge");
  const novaNum = document.getElementById("nova-number");
  novaBadge.className = "badge-nova"; // Reset classes
  if (record.nova_group) {
    const group = parseInt(record.nova_group);
    if (group >= 1 && group <= 4) {
      novaNum.textContent = group;
      novaBadge.classList.add(`n${group}`);
      novaBadge.classList.remove("hidden");
    } else {
      novaBadge.classList.add("hidden");
    }
  } else {
    novaBadge.classList.add("hidden");
  }

  // Metadata Panel
  document.getElementById("product-quantity").textContent = record.quantity_label || "N/A";
  document.getElementById("product-serving").textContent = record.serving_size || "N/A";
  document.getElementById("product-categories").textContent = record.categories || "General Retail / Other";

  // Nutrition Metrics Panel
  const nutritionPanel = document.getElementById("nutrition-panel");
  const noNutrMsg = document.getElementById("no-nutrition-message");

  if (record.has_nutrition) {
    nutritionPanel.classList.remove("hidden");
    noNutrMsg.classList.add("hidden");

    // Populate bars
    setNutrientRow("kcal", record.kcal_100g, " kcal", 800); // Scale relative to 800kcal
    setNutrientRow("fat", record.fat_100g, "g", 100);
    setNutrientRow("sat_fat", record.sat_fat_100g, "g", 100);
    setNutrientRow("carbs", record.carbs_100g, "g", 100);
    setNutrientRow("sugars", record.sugars_100g, "g", 100);
    setNutrientRow("fiber", record.fiber_100g, "g", 100);
    setNutrientRow("proteins", record.proteins_100g, "g", 100);
    setNutrientRow("salt", record.salt_100g, "g", 5); // Scale relative to 5g maximum salt
  } else {
    nutritionPanel.classList.add("hidden");
    noNutrMsg.classList.remove("hidden");
  }

  // Allergens
  const allergensPanel = document.getElementById("allergens-container");
  const allergensTagsList = document.getElementById("allergens-tags-list");
  allergensTagsList.innerHTML = "";

  if (record.allergens) {
    allergensPanel.classList.remove("hidden");
    const items = record.allergens.split(",");
    items.forEach(item => {
      const trimmed = item.trim();
      if (trimmed) {
        const tag = document.createElement("span");
        tag.className = "allergen-tag";
        tag.textContent = trimmed;
        allergensTagsList.appendChild(tag);
      }
    });
  } else {
    allergensPanel.classList.add("hidden");
  }
}

function setNutrientRow(id, value, unit, maxVal) {
  const valEl = document.getElementById(`val-${id}`);
  const barEl = document.getElementById(`bar-${id}`);
  const rowEl = document.getElementById(`nutr-${id}`);

  if (value !== null && value !== undefined) {
    rowEl.classList.remove("hidden");
    valEl.textContent = value.toFixed(1) + unit;
    const percentage = Math.min(100, (value / maxVal) * 100);
    // Request animation frame to ensure progress bar animate smoothly on load
    requestAnimationFrame(() => {
      barEl.style.width = `${percentage}%`;
    });
  } else {
    rowEl.classList.add("hidden");
    barEl.style.width = "0%";
  }
}

// Display product not found details
function displayNotFound(barcode) {
  productDetailCard.classList.remove("hidden");
  historyCard.classList.add("hidden");

  // Show standard placeholder card
  document.getElementById("product-name").textContent = "Product Not Found";
  document.getElementById("product-brand").textContent = "Not recognized in databases";
  document.getElementById("product-barcode").textContent = `Barcode: ${barcode}`;
  document.getElementById("product-source-badge").textContent = "None";
  
  // Hide image, badges
  document.getElementById("product-image").style.display = "none";
  document.getElementById("product-image-fallback").style.display = "flex";
  document.getElementById("nutriscore-badge").classList.add("hidden");
  document.getElementById("nova-badge").classList.add("hidden");

  document.getElementById("product-quantity").textContent = "-";
  document.getElementById("product-serving").textContent = "-";
  document.getElementById("product-categories").textContent = "-";

  document.getElementById("nutrition-panel").classList.add("hidden");
  document.getElementById("no-nutrition-message").classList.remove("hidden");
  document.getElementById("allergens-container").classList.add("hidden");
}

// UI Panel toggling
function showHistoryPanel() {
  productDetailCard.classList.add("hidden");
  historyCard.classList.remove("hidden");
  
  // Reset scan state on back click to allow immediate re-scan
  lastScannedBarcode = "";
}

// Save scan result to browser local storage
function saveToLocalHistory(barcode, status, source, product) {
  try {
    let history = JSON.parse(localStorage.getItem("scan_history") || "[]");
    
    // Remove duplicates to push this barcode to top of the list
    history = history.filter(item => item.barcode !== barcode);
    
    history.unshift({
      timestamp: new Date().toISOString().replace('T', ' ').split('.')[0],
      barcode: barcode,
      status: status,
      source: source,
      product: product
    });
    
    // Cap history list size at 50 items
    if (history.length > 50) {
      history = history.slice(0, 50);
    }
    
    localStorage.setItem("scan_history", JSON.stringify(history));
  } catch (e) {
    console.error("Failed to save to localStorage history", e);
  }
}

// Fetch and load scan history (now reading client-side from localStorage)
function loadScanHistory() {
  try {
    const history = JSON.parse(localStorage.getItem("scan_history") || "[]");
    renderHistoryList(history);
  } catch (err) {
    console.error("Error loading scan history", err);
    historyList.innerHTML = '<p class="empty-history-text">Failed to load history.</p>';
  }
}

// Render history records
function renderHistoryList(records) {
  historyList.innerHTML = "";
  
  if (!records || records.length === 0) {
    historyList.innerHTML = '<p class="empty-history-text">No recent scans found.</p>';
    return;
  }

  records.forEach(item => {
    const card = document.createElement("div");
    card.className = "history-item";
    card.addEventListener("click", () => {
      performLookup(item.barcode);
    });

    const left = document.createElement("div");
    left.className = "history-item-left";

    const name = document.createElement("div");
    name.className = "history-item-name";
    name.textContent = item.product;
    left.appendChild(name);

    const meta = document.createElement("div");
    meta.className = "history-item-meta";

    const code = document.createElement("span");
    code.className = "history-item-barcode";
    code.textContent = item.barcode;
    meta.appendChild(code);

    const divider = document.createTextNode(" • ");
    meta.appendChild(divider);

    const statusBadge = document.createElement("span");
    statusBadge.className = `history-item-status ${item.status}`;
    statusBadge.textContent = item.status === "found" ? item.source : "Not Found";
    meta.appendChild(statusBadge);

    left.appendChild(meta);
    card.appendChild(left);

    const right = document.createElement("div");
    right.className = "history-item-time";
    // Format timestamp roughly (show only HH:MM:SS)
    if (item.timestamp && item.timestamp.includes(" ")) {
      right.textContent = item.timestamp.split(" ")[1];
    } else {
      right.textContent = item.timestamp || "";
    }
    card.appendChild(right);

    historyList.appendChild(card);
  });
}

// Theme Toggle logic
function setupThemeToggle() {
  const themeToggleBtn = document.getElementById("theme-toggle-btn");
  if (!themeToggleBtn) return;

  // Set initial accessibility state
  const currentTheme = document.documentElement.getAttribute("data-theme") || "light";
  themeToggleBtn.setAttribute("aria-checked", currentTheme === "dark" ? "true" : "false");

  themeToggleBtn.addEventListener("click", () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const newTheme = isDark ? "light" : "dark";
    
    document.documentElement.setAttribute("data-theme", newTheme);
    localStorage.setItem("app_theme", newTheme);
    themeToggleBtn.setAttribute("aria-checked", !isDark ? "true" : "false");
    console.log(`Theme toggled to: ${newTheme}`);
  });
}
