// State variables
let html5QrCode = null;
let cameras = [];
let currentCameraId = null;
let isScanning = false;

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
}

// Camera initialization
async function initializeCameraList() {
  try {
    // Check if camera permission is available
    cameras = await Html5Qrcode.getCameras();
    cameraSelect.innerHTML = "";
    
    if (cameras && cameras.length > 0) {
      cameras.forEach((camera, index) => {
        const option = document.createElement("option");
        option.value = camera.id;
        // Clean up names if they contain duplicate terms
        option.text = camera.label || `Camera ${index + 1}`;
        cameraSelect.appendChild(option);
      });
      currentCameraId = cameras[0].id;
      toggleCameraBtn.disabled = false;
    } else {
      cameraSelect.innerHTML = '<option value="">No cameras found</option>';
      toggleCameraBtn.disabled = true;
    }
  } catch (err) {
    console.error("Error getting cameras", err);
    cameraSelect.innerHTML = '<option value="">Camera access denied</option>';
    toggleCameraBtn.disabled = true;
  }
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

async function startScanning() {
  if (!html5QrCode) {
    html5QrCode = new Html5Qrcode("reader");
  }

  toggleCameraBtn.textContent = "Stop Camera";
  toggleCameraBtn.classList.remove("btn-primary");
  toggleCameraBtn.classList.add("btn-secondary");
  viewfinderOverlay.style.display = "block";
  isScanning = true;

  try {
    const config = {
      fps: 20,
      aspectRatio: 1.6,
      qrbox: (width, height) => {
        // Wide scan window tailored for 1D barcodes (EAN-13, UPC)
        const qrWidth = Math.floor(width * 0.75);
        const qrHeight = Math.floor(height * 0.35);
        return { width: qrWidth, height: qrHeight };
      },
      experimentalFeatures: {
        useBarCodeDetectorIfSupported: true
      }
    };

    await html5QrCode.start(
      currentCameraId,
      config,
      onBarcodeDetected,
      (errorMessage) => {
        // Verbose scan error is normal for frame-by-frame check, ignore
      }
    );
  } catch (err) {
    console.error("Failed to start scanning", err);
    alert(`Error starting camera: ${err.message || err}`);
    await stopScanning();
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
    } else {
      displayNotFound(barcode);
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

// Fetch and load scan history
async function loadScanHistory() {
  try {
    const response = await fetch("/api/history?limit=25");
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const data = await response.json();
    renderHistoryList(data);
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
