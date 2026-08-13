(function () {
  "use strict";

  const form = document.getElementById("dubForm");
  const formError = document.getElementById("formError");
  const submitBtn = document.getElementById("submitBtn");
  const monitor = document.getElementById("monitor");
  const pctEl = document.getElementById("pct");
  const statusMsg = document.getElementById("statusMsg");
  const resultArea = document.getElementById("resultArea");

  // ---------- Dropzone ----------
  const dropzone = document.getElementById("dropzone");
  const videoInput = document.getElementById("videoInput");
  const dzText = document.getElementById("dzText");

  function humanSize(bytes) {
    if (bytes > 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  videoInput.addEventListener("change", () => {
    const f = videoInput.files[0];
    if (f) {
      dzText.textContent = f.name + " (" + humanSize(f.size) + ")";
      dropzone.classList.add("filled");
    }
  });
  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); })
  );
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) {
      videoInput.files = e.dataTransfer.files;
      dzText.textContent = f.name + " (" + humanSize(f.size) + ")";
      dropzone.classList.add("filled");
    }
  });

  // ---------- Show/hide dependent sections ----------
  function bindToggle(toggleId, sectionId) {
    const toggle = document.getElementById(toggleId);
    const section = document.getElementById(sectionId);
    function sync() { section.style.display = toggle.checked ? "" : "none"; }
    toggle.addEventListener("change", sync);
    sync();
  }
  bindToggle("subtitlesToggle", "subtitleOptions");
  bindToggle("titleToggle", "titleOptions");

  // ---------- Test keys ----------
  const testKeysBtn = document.getElementById("testKeysBtn");
  const keyCheckResult = document.getElementById("keyCheckResult");

  testKeysBtn.addEventListener("click", async () => {
    const gemini = document.getElementById("geminiKeys").value;
    const groq = document.getElementById("groqKeys").value;
    if (!gemini.trim() && !groq.trim()) {
      keyCheckResult.innerHTML = '<div class="key-chip"><span class="dot bad"></span>Key ထည့်ပါ</div>';
      return;
    }
    testKeysBtn.disabled = true;
    testKeysBtn.textContent = "⏳ စစ်ဆေးနေသည်...";
    keyCheckResult.innerHTML = "";
    try {
      const res = await fetch("/api/check-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gemini, groq }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Error");
      let html = "";
      (data.gemini || []).forEach((k) => {
        html += `<div class="key-chip"><span class="dot ${k.ok ? "ok" : "bad"}"></span>Gemini ${k.key} ${k.ok ? "OK" : "(" + (k.err || "error") + ")"}</div>`;
      });
      (data.groq || []).forEach((k) => {
        html += `<div class="key-chip"><span class="dot ${k.ok ? "ok" : "bad"}"></span>Groq ${k.key} ${k.ok ? "OK" : "(" + (k.err || "error") + ")"}</div>`;
      });
      keyCheckResult.innerHTML = html || '<div class="key-chip"><span class="dot bad"></span>Key မတွေ့ပါ</div>';
    } catch (e) {
      keyCheckResult.innerHTML = `<div class="key-chip"><span class="dot bad"></span>${e.message}</div>`;
    } finally {
      testKeysBtn.disabled = false;
      testKeysBtn.textContent = "🔑 Key စစ်ဆေးမည်";
    }
  });

  // ---------- Submit / job polling ----------
  let pollTimer = null;

  function setMonitor(state, pct, msg) {
    pctEl.textContent = (pct === null || pct === undefined) ? "--%" : pct + "%";
    statusMsg.textContent = msg || "";
    monitor.classList.toggle("active", state === "processing" || state === "queued");
  }

  function showResult(kind, payload) {
    if (kind === "done") {
      resultArea.innerHTML = `<a class="download-btn" href="${payload.download_url}" download>⬇ Video ဒေါင်းလုတ်လုပ်မည်</a><button type="button" class="reset-link" id="resetBtn">နောက်တစ်ခု ဒပ်ဘ်လုပ်မည်</button>`;
    } else if (kind === "error") {
      resultArea.innerHTML = `<div class="error-box">${payload}</div><button type="button" class="reset-link" id="resetBtn">ပြန်ကြိုးစားမည်</button>`;
    } else {
      resultArea.innerHTML = "";
    }
    const resetBtn = document.getElementById("resetBtn");
    if (resetBtn) resetBtn.addEventListener("click", resetForm);
  }

  function resetForm() {
    setMonitor("idle", null, "Video တင်ပြီး ဒပ်ဘ်စတင်ရန် ဘေးက form ကို ဖြည့်ပါ။");
    showResult("idle");
    submitBtn.disabled = false;
    submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
    if (pollTimer) clearInterval(pollTimer);
  }

  function pollJob(jobId) {
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        if (!res.ok) throw new Error("Job status unavailable");
        const data = await res.json();
        setMonitor(data.status, data.progress, data.message);
        if (data.status === "done") {
          clearInterval(pollTimer);
          submitBtn.disabled = false;
          submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
          showResult("done", data);
        } else if (data.status === "error") {
          clearInterval(pollTimer);
          submitBtn.disabled = false;
          submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
          setMonitor("error", data.progress, "မအောင်မြင်ပါ");
          showResult("error", data.message);
        }
      } catch (e) {
        clearInterval(pollTimer);
        submitBtn.disabled = false;
        submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
        setMonitor("error", null, "ချိတ်ဆက်မှု အမှား");
        showResult("error", e.message);
      }
    }, 2500);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.classList.remove("show");
    if (!videoInput.files[0]) {
      formError.textContent = "Video file ရွေးပါ။";
      formError.classList.add("show");
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = "⏳ တင်နေသည်...";
    showResult("idle");
    setMonitor("queued", 0, "Upload လုပ်နေသည်...");

    const fd = new FormData(form);
    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "တင်ခြင်း မအောင်မြင်ပါ");
      setMonitor("queued", 0, "စီစဉ်နေသည်...");
      pollJob(data.job_id);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "▶ ဒပ်ဘ် စတင်မည်";
      formError.textContent = err.message;
      formError.classList.add("show");
      setMonitor("idle", null, "Video တင်ပြီး ဒပ်ဘ်စတင်ရန် ဘေးက form ကို ဖြည့်ပါ။");
    }
  });
})();
